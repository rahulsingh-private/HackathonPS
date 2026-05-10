#!/usr/bin/env python3
"""
AI-Powered Self-Healing System
Monitors the HackathonPS chaos backend, detects anomalies via Claude,
and triggers corrective REST actions automatically.
Falls back to rule-based detection when the Claude API is unavailable.
"""

import re
import time
import json
import sys
from datetime import datetime, timezone
from collections import deque

import requests
import anthropic

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_URL        = "http://localhost:8080"
POLL_INTERVAL   = 15          # seconds between log polls
MAX_LOGS_CYCLE  = 150         # max log entries sent to Claude per cycle
LOG_LEVELS      = {"ERROR", "WARN"}
CLAUDE_MODEL    = "claude-opus-4-7"

# ── Claude client ───────────────────────────────────────────────────────────────
client = anthropic.Anthropic()

# ── State ──────────────────────────────────────────────────────────────────────
seen_log_keys: set[str]    = set()
action_history: deque      = deque(maxlen=30)
healed_keys: set[str]      = set()   # idempotency: action:orderId
cycle_count                = 0
claude_available           = True    # toggled on rate-limit / auth errors

# ── Stable system prompt (cached at Anthropic) ─────────────────────────────────
SYSTEM_PROMPT = """You are an AI self-healing agent for a Java Spring Boot e-commerce backend.
The system has intentional chaos engineering bugs. Your job is to:
1. Analyse the provided log entries (ERROR/WARN level) from the last polling cycle.
2. Identify anomalies mapped to known failure patterns.
3. Recommend and specify corrective REST API actions.

KNOWN FAILURE PATTERNS:
- GHOST_PAYMENT         : payment processed for CANCELLED or FAILED order
- DUPLICATE_PAYMENT     : multiple SUCCESS payments for the same orderId
- NEGATIVE_STOCK        : inventory level below zero for a product
- INVENTORY_LEAK        : reserved stock not released on order cancel
- STUCK_ORDER           : order remains in CREATED state after creation window
- PAID_CANCELLED_ORDER  : order simultaneously PAID and CANCELLED
- DUPLICATE_REFUND      : refund attempted on already-refunded payment
- ORDER_SYNC_FAILURE    : payment committed but order state not updated to PAID

AVAILABLE HEALING ACTIONS:
- cancel_order          : POST /order/cancel  {"orderId": "..."}
- refund_payment        : POST /refund        {"orderId": "..."}
- check_inventory       : GET  /inventory     (no params needed)
- alert_only            : no REST call

EXTRACTION RULES:
- Extract orderId values from log message text (UUID pattern).
- Do NOT repeat actions for orderIds already in recent_actions.
- GHOST_PAYMENT -> cancel_order then refund_payment.
- DUPLICATE_PAYMENT -> refund_payment.
- NEGATIVE_STOCK -> check_inventory + alert_only.
- STUCK_ORDER -> cancel_order.
- INVENTORY_LEAK -> alert_only.
- ORDER_SYNC_FAILURE -> alert_only (cannot re-trigger payment sync via API).

OUTPUT FORMAT — respond with ONLY valid JSON, no markdown fences:
{
  "anomalies": [
    {
      "pattern": "PATTERN_NAME",
      "severity": "critical|high|medium|low",
      "affected_entity": "orderId or productId or unknown",
      "evidence": "1-2 key log lines",
      "rca": "root cause in 1-2 sentences"
    }
  ],
  "actions": [
    {
      "action": "action_name",
      "params": {"orderId": "..."},
      "reason": "why this fixes the anomaly"
    }
  ],
  "summary": "1-2 sentence plain-English summary",
  "health_score": 0
}

If no anomalies: {"anomalies":[],"actions":[],"summary":"No anomalies detected.","health_score":100}
"""

# ── ANSI colours ───────────────────────────────────────────────────────────────
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"
MAGENTA= "\033[95m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

# ── HTTP helpers ───────────────────────────────────────────────────────────────
def fetch_logs() -> list[dict]:
    try:
        r = requests.get(f"{BASE_URL}/logs", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"{RED}[healer] Cannot reach /logs: {exc}{RESET}")
        return []

def fetch_inventory() -> dict:
    try:
        r = requests.get(f"{BASE_URL}/inventory", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"{RED}[healer] Cannot reach /inventory: {exc}{RESET}")
        return {}

def post_cancel(order_id: str) -> dict:
    try:
        r = requests.post(f"{BASE_URL}/order/cancel", json={"orderId": order_id}, timeout=5)
        return {"status": r.status_code, "body": r.text[:200]}
    except Exception as exc:
        return {"status": -1, "error": str(exc)}

def post_refund(order_id: str) -> dict:
    try:
        r = requests.post(f"{BASE_URL}/refund", json={"orderId": order_id}, timeout=5)
        return {"status": r.status_code, "body": r.text[:200]}
    except Exception as exc:
        return {"status": -1, "error": str(exc)}

# ── Action executor ────────────────────────────────────────────────────────────
def execute_action(action: dict) -> dict | None:
    name     = action.get("action", "")
    params   = action.get("params") or {}
    order_id = params.get("orderId", "")

    key = f"{name}:{order_id}"
    if key in healed_keys:
        return None
    healed_keys.add(key)

    if name == "cancel_order" and order_id:
        return {"action": name, "orderId": order_id, "result": post_cancel(order_id)}
    if name == "refund_payment" and order_id:
        return {"action": name, "orderId": order_id, "result": post_refund(order_id)}
    if name == "check_inventory":
        inv = fetch_inventory()
        neg = {k: v.get("stock", 0) for k, v in inv.items() if v.get("stock", 0) < 0}
        return {"action": name, "negative_stock_items": neg}
    if name == "alert_only":
        return {"action": name, "reason": action.get("reason", "")}
    return None

# ── Rule-based fallback analyser ───────────────────────────────────────────────
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_RULES = [
    # (error_type keywords, message keywords, pattern, severity, action, rca)
    (
        {"PAID_AFTER_CANCEL", "STATE_MACHINE_VIOLATION", "ORDER_STATE_CONFLICT"},
        {"cancelled", "terminal state"},
        "PAID_CANCELLED_ORDER", "critical",
        "cancel_order",
        "PaymentService falls through on CANCELLED orders — OrderService state machine allows PAID on terminal state",
    ),
    (
        {"PAY_PARTIAL_WRITE", "ORDER_SYNC_FAILURE", "PARTIAL_COMMIT", "ORDER_UPDATE_FAILURE"},
        {"payment", "order", "not updated", "sync"},
        "ORDER_SYNC_FAILURE", "high",
        "alert_only",
        "Payment committed but order status write failed — partial write leaves order out of sync with payment record",
    ),
    (
        {"CONFIRM_NOTIFICATION_FAILED", "PAY_CONFIRM_ERR", "ASYNC_CONFIRM_TIMEOUT"},
        {"confirm", "payment"},
        "DUPLICATE_PAYMENT", "high",
        "refund_payment",
        "ChaosScheduler paymentRetryWorker submits duplicate payments; PaymentService has no idempotency guard",
    ),
    (
        {"NEGATIVE_STOCK", "STOCK_BELOW_ZERO", "INV_COUNTER_UNDERFLOW", "STOCK_LEVEL_ANOMALY",
         "AUDIT_NEGATIVE_STOCK", "AUDIT_STOCK_UNDERFLOW"},
        {"negative", "below zero", "underflow"},
        "NEGATIVE_STOCK", "high",
        "check_inventory",
        "nightlyStockSyncWorker deducts 999 units every 50s with no floor check; deductStock has no sufficiency guard",
    ),
    (
        {"STOCK_NOT_RELEASED", "INVENTORY_RELEASE_DEFERRED", "INV_HOLD_OUTSTANDING", "RELEASE_DEFERRED"},
        {"release", "stock", "cancel"},
        "INVENTORY_LEAK", "medium",
        "alert_only",
        "cancelOrder only releases inventory 40% of the time (Math.random() > 0.6); reserved stock leaks on most cancels",
    ),
    (
        {"ORDER_STUCK_CREATED", "ORDER_PIPELINE_STALL", "CREATED_STATE_TIMEOUT"},
        {"stuck", "created", "stall"},
        "STUCK_ORDER", "medium",
        "cancel_order",
        "shouldFail() in OrderService aborts reservation phase and returns early without setting order to FAILED",
    ),
    (
        {"DUPLICATE_REFUND", "REFUND_ALREADY_PROCESSED", "WARN_DUP_REFUND"},
        {"refund", "already", "duplicate"},
        "DUPLICATE_REFUND", "medium",
        "alert_only",
        "refundPayment has no guard against re-refunding; ChaosScheduler retries trigger duplicate refund attempts",
    ),
    (
        {"UNEXPECTED_ORDER_STATUS", "ORDER_STATE_MISMATCH", "AUTH_ON_TERMINAL_ORDER"},
        {"non-standard", "status=FAILED", "status=CANCELLED"},
        "GHOST_PAYMENT", "critical",
        "cancel_order",
        "processPayment only warns on non-payable order states, never returns — payment proceeds regardless of order status",
    ),
]

def rule_based_analyse(logs: list[dict]) -> dict:
    anomalies = []
    actions   = []
    seen_patterns: set[str] = set()

    for entry in logs:
        error_type = (entry.get("error_type") or "").upper()
        message    = (entry.get("message") or "").lower()
        msg_raw    = entry.get("message") or ""

        # Extract first UUID found in message as affected entity
        uuid_match   = _UUID_RE.search(msg_raw)
        entity       = uuid_match.group(0) if uuid_match else "unknown"

        for et_kws, msg_kws, pattern, severity, action_name, rca in _RULES:
            if pattern in seen_patterns:
                continue
            et_hit  = error_type in et_kws
            msg_hit = any(kw in message for kw in msg_kws)
            if not (et_hit or msg_hit):
                continue

            seen_patterns.add(pattern)
            anomalies.append({
                "pattern":          pattern,
                "severity":         severity,
                "affected_entity":  entity,
                "evidence":         msg_raw[:120],
                "rca":              rca,
            })

            if action_name in ("cancel_order", "refund_payment") and entity != "unknown":
                actions.append({
                    "action": action_name,
                    "params": {"orderId": entity},
                    "reason": f"Rule-based: {pattern} detected for orderId={entity}",
                })
                # Ghost payment also needs a refund
                if pattern == "GHOST_PAYMENT":
                    actions.append({
                        "action": "refund_payment",
                        "params": {"orderId": entity},
                        "reason": "Rule-based: reverse charge on cancelled order",
                    })
            elif action_name == "check_inventory":
                actions.append({
                    "action": "check_inventory",
                    "params": {},
                    "reason": f"Rule-based: {pattern} — verify current stock levels",
                })
            elif action_name == "alert_only":
                actions.append({
                    "action": "alert_only",
                    "params": {},
                    "reason": f"Rule-based: {pattern} — requires manual intervention",
                })

    critical_count = sum(1 for a in anomalies if a["severity"] in ("critical", "high"))
    score = max(0, 100 - len(anomalies) * 12 - critical_count * 8)

    summary = (
        f"Rule engine found {len(anomalies)} anomaly(s) across {len(logs)} log entries. "
        f"{critical_count} critical/high severity."
        if anomalies else "No anomalies detected by rule engine."
    )

    return {
        "anomalies":    anomalies,
        "actions":      actions,
        "summary":      summary,
        "health_score": score,
        "_source":      "rules",
    }

# ── Claude analyser ────────────────────────────────────────────────────────────
def analyse_with_claude(logs: list[dict]) -> dict | None:
    global claude_available
    if not claude_available or not logs:
        return None

    payload = json.dumps({
        "cycle":          cycle_count,
        "timestamp_utc":  datetime.now(timezone.utc).isoformat(),
        "log_count":      len(logs),
        "recent_actions": list(action_history)[-10:],
        "logs":           logs,
    }, indent=2)

    text = ""
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            system=[{
                "type":          "text",
                "text":          SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": payload}],
        )
        for block in resp.content:
            if block.type == "text":
                text = block.text
                break
        result = json.loads(text)
        result["_source"] = "claude"
        return result

    except anthropic.RateLimitError as exc:
        print(f"{YELLOW}[healer] Claude rate-limited — switching to rule engine. ({exc}){RESET}")
        claude_available = False
        return None
    except anthropic.AuthenticationError as exc:
        print(f"{RED}[healer] Claude auth error — switching to rule engine. ({exc}){RESET}")
        claude_available = False
        return None
    except json.JSONDecodeError:
        print(f"{RED}[healer] Claude returned non-JSON — falling back to rules.{RESET}")
        return None
    except Exception as exc:
        print(f"{RED}[healer] Claude error: {exc}{RESET}")
        return None

# ── Display ────────────────────────────────────────────────────────────────────
def print_header():
    print(f"\n{BOLD}{CYAN}{'='*70}{RESET}")
    print(f"{BOLD}{CYAN}  AI Self-Healing Monitor  -  HackathonPS Chaos Backend{RESET}")
    print(f"{BOLD}{CYAN}{'='*70}{RESET}")

def print_cycle_banner(new_count: int):
    ts  = datetime.now().strftime("%H:%M:%S")
    eng = f"{GREEN}Claude{RESET}" if claude_available else f"{YELLOW}RuleEngine{RESET}"
    print(f"\n{BOLD}[{ts}] Cycle #{cycle_count}{RESET}  "
          f"{DIM}new logs: {new_count}  seen total: {len(seen_log_keys)}  engine: {RESET}{eng}")
    print(f"{DIM}{'-'*70}{RESET}")

def print_log_sample(logs: list[dict]):
    if not logs:
        print(f"  {DIM}No new ERROR/WARN logs.{RESET}")
        return
    for entry in logs[:8]:
        lvl = entry.get("level", "INFO")
        svc = entry.get("service", "?")[:18]
        msg = entry.get("message", "")[:78]
        col = RED if lvl == "ERROR" else YELLOW
        print(f"  {col}{lvl:<5}{RESET} [{svc}] {msg}")
    if len(logs) > 8:
        print(f"  {DIM}... and {len(logs)-8} more{RESET}")

def print_analysis(a: dict):
    src   = a.get("_source", "?")
    score = a.get("health_score", 100)
    label = f"{GREEN}[Claude]{RESET}" if src == "claude" else f"{MAGENTA}[RuleEngine]{RESET}"

    score_fmt = (
        f"{GREEN}{BOLD}{score}{RESET}" if score >= 80
        else f"{YELLOW}{BOLD}{score}{RESET}" if score >= 50
        else f"{RED}{BOLD}{score}{RESET}"
    )

    print(f"\n  {label} Health: {score_fmt}/100  |  {a.get('summary','')}")

    for ano in a.get("anomalies", []):
        sev = ano.get("severity", "").upper()
        col = RED if sev in ("CRITICAL", "HIGH") else YELLOW
        print(f"  {col}[{sev}]{RESET} {BOLD}{ano.get('pattern')}{RESET}"
              f"  entity={ano.get('affected_entity','?')}")
        print(f"         RCA: {DIM}{ano.get('rca','')}{RESET}")

    acts = a.get("actions", [])
    if acts:
        print(f"  {BOLD}{BLUE}Actions to execute ({len(acts)}):{RESET}")
        for act in acts:
            print(f"    -> {BOLD}{act.get('action')}{RESET}"
                  f"  {DIM}{act.get('params',{})}{RESET}")

def print_action_result(r: dict):
    action = r.get("action", "")
    if action == "check_inventory":
        neg = r.get("negative_stock_items", {})
        if neg:
            print(f"    {RED}[ALERT] Negative stock: {neg}{RESET}")
        else:
            print(f"    {GREEN}[OK] check_inventory: all levels >= 0{RESET}")
    elif action == "alert_only":
        print(f"    {YELLOW}[ALERT] {r.get('reason','')}{RESET}")
    else:
        res    = r.get("result", {})
        status = res.get("status", -1)
        col    = GREEN if 200 <= status < 300 else RED
        tag    = "[OK]" if 200 <= status < 300 else "[FAIL]"
        print(f"    {col}{tag} {action} orderId={r.get('orderId')} -> HTTP {status}{RESET}")

# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    global cycle_count

    print_header()
    print(f"  Backend : {BOLD}{BASE_URL}{RESET}")
    print(f"  Poll    : every {POLL_INTERVAL}s")
    print(f"  Engine  : Claude ({CLAUDE_MODEL}) with rule-based fallback")
    print(f"  Press Ctrl+C to stop.\n")

    # Consume existing logs as baseline so first cycle sees only delta
    baseline = fetch_logs()
    for e in baseline:
        seen_log_keys.add(f"{e.get('timestamp','')}|{e.get('trace_id','')}|{e.get('message','')}")
    print(f"  {DIM}Baseline: {len(baseline)} entries consumed. Watching for new activity...{RESET}\n")

    while True:
        try:
            time.sleep(POLL_INTERVAL)
            cycle_count += 1

            all_logs   = fetch_logs()
            new_errors = []
            for e in all_logs:
                k = f"{e.get('timestamp','')}|{e.get('trace_id','')}|{e.get('message','')}"
                if k in seen_log_keys:
                    continue
                seen_log_keys.add(k)
                if e.get("level") in LOG_LEVELS:
                    new_errors.append(e)

            new_errors = new_errors[-MAX_LOGS_CYCLE:]
            print_cycle_banner(len(new_errors))
            print_log_sample(new_errors)

            if not new_errors:
                print(f"  {GREEN}[OK] System quiet — no new errors/warnings.{RESET}")
                continue

            # Try Claude first, fall back to rules
            analysis = analyse_with_claude(new_errors)
            if analysis is None:
                analysis = rule_based_analyse(new_errors)

            print_analysis(analysis)

            acts = analysis.get("actions", [])
            if acts:
                print(f"\n  {BOLD}Executing healing actions...{RESET}")
            for act in acts:
                result = execute_action(act)
                if result is None:
                    oid = (act.get("params") or {}).get("orderId", "?")
                    print(f"    {DIM}Skipped {act.get('action')} orderId={oid} (already healed){RESET}")
                    continue
                print_action_result(result)
                action_history.append({
                    "cycle":  cycle_count,
                    "action": act.get("action"),
                    "params": act.get("params", {}),
                    "ts":     datetime.now(timezone.utc).isoformat(),
                })

        except KeyboardInterrupt:
            print(f"\n\n{CYAN}Healer stopped after {cycle_count} cycle(s).{RESET}")
            sys.exit(0)
        except Exception as exc:
            print(f"{RED}[healer] Loop error: {exc}{RESET}")
            time.sleep(5)


if __name__ == "__main__":
    main()
