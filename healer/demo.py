#!/usr/bin/env python3
"""
AI Self-Healing Live Demo
Shows real incidents being raised from live logs, RCA performed,
and healing REST calls executed — all in real time.
"""

import re
import sys
import time
import json
import requests
from datetime import datetime

BASE_URL = "http://localhost:8080"

# ── ANSI ───────────────────────────────────────────────────────────────────────
RED     = "\033[91m"
YELLOW  = "\033[93m"
GREEN   = "\033[92m"
CYAN    = "\033[96m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RESET   = "\033[0m"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# ── Incident definitions ───────────────────────────────────────────────────────
INCIDENTS = [
    {
        "id":        "INC-001",
        "pattern":   "GHOST_PAYMENT",
        "severity":  "CRITICAL",
        "triggers":  {"UNEXPECTED_ORDER_STATUS", "ORDER_STATE_MISMATCH", "AUTH_ON_TERMINAL_ORDER"},
        "msg_hints": ["non-standard state", "status=CANCELLED", "status=FAILED", "terminal order state",
                      "payment auth continuing despite", "payment proceeding for order status=CANCELLED",
                      "payment proceeding for order status=FAILED"],
        "title":     "Ghost Payment — charge on cancelled/failed order",
        "rca": [
            "  Root Cause  : PaymentService.processPayment() checks order status but only logs a",
            "               warning — it never returns or throws. Execution falls through and the",
            "               payment is processed regardless of order state.",
            "  Code Path   : PaymentService.java:71-83",
            "               if (order.getStatus() != RESERVED && != CREATED) {",
            "                   log.warn(...);   // <-- warns but continues",
            "               }                    // <-- no return/throw here!",
            "               payment.setStatus(SUCCESS);  // charge happens anyway",
            "  Impact      : Customer charged for an order that was already cancelled.",
            "               Funds debited with no corresponding fulfilment.",
            "  Code Fix    : Add 'throw new IllegalStateException()' inside the status check.",
        ],
        "action": "refund_payment",
        "action_label": "POST /refund",
        "action_reason": "Reverse the erroneous charge against the cancelled order",
    },
    {
        "id":        "INC-002",
        "pattern":   "DUPLICATE_PAYMENT",
        "severity":  "HIGH",
        "triggers":  {"CONFIRM_NOTIFICATION_FAILED", "PAY_CONFIRM_ERR"},
        "msg_hints": ["duplicate payment", "pay record conflict", "payment record created for",
                      "retry", "double charge"],
        "title":     "Duplicate Payment — same order charged twice",
        "rca": [
            "  Root Cause  : ChaosScheduler.paymentRetryWorker() submits payment for the same",
            "               orderId twice without checking if a payment already exists.",
            "               PaymentService has no idempotency key or duplicate guard.",
            "  Code Path   : PaymentService.java:118-119",
            "               paymentsById.put(paymentId, payment);",
            "               paymentsByOrder.computeIfAbsent(...).add(payment);  // always adds",
            "  Impact      : Customer charged twice for a single order.",
            "               paymentsByOrder list grows unbounded on retries.",
            "  Code Fix    : Check paymentsByOrder before creating a new payment record.",
        ],
        "action": "refund_payment",
        "action_label": "POST /refund",
        "action_reason": "Refund the duplicate charge (latest payment in the list)",
    },
    {
        "id":        "INC-003",
        "pattern":   "NEGATIVE_STOCK",
        "severity":  "HIGH",
        "triggers":  {"NEGATIVE_STOCK", "STOCK_BELOW_ZERO", "INV_COUNTER_UNDERFLOW",
                      "STOCK_LEVEL_ANOMALY", "AUDIT_NEGATIVE_STOCK"},
        "msg_hints": ["negative stock", "below zero", "underflow", "-1", "stock level below threshold",
                      "out of expected range"],
        "title":     "Negative Stock — inventory counter below zero",
        "rca": [
            "  Root Cause  : ChaosScheduler.nightlyStockSyncWorker() calls",
            "               inventoryService.deductStock('PROD-005', 999) every 50 seconds.",
            "               InventoryService.deductStock() uses AtomicInteger.addAndGet(-qty)",
            "               with no floor check — stock goes arbitrarily negative.",
            "  Code Path   : InventoryService.java:150",
            "               int newStock = item.getStockRef().addAndGet(-quantity);",
            "               if (newStock < 0) { log.warn(...); }  // warns but doesn't stop",
            "  Impact      : PROD-005 stock is currently -134,000+.",
            "               Orders accepted for items that don't exist.",
            "  Code Fix    : Use compareAndSet loop to ensure stock >= quantity before deducting.",
        ],
        "action": "check_inventory",
        "action_label": "GET /inventory",
        "action_reason": "Inspect current stock levels and flag all negative counters",
    },
    {
        "id":        "INC-004",
        "pattern":   "INVENTORY_LEAK",
        "severity":  "MEDIUM",
        "triggers":  {"STOCK_NOT_RELEASED", "INVENTORY_RELEASE_DEFERRED",
                      "INV_HOLD_OUTSTANDING", "RELEASE_DEFERRED"},
        "msg_hints": ["stock not released", "release deferred", "inv hold outstanding",
                      "reservation may persist", "stock hold not cleared"],
        "title":     "Inventory Leak — reserved stock not released on cancel",
        "rca": [
            "  Root Cause  : OrderService.cancelOrder() only releases inventory",
            "               40% of the time due to a random branch:",
            "  Code Path   : OrderService.java:246",
            "               if (Math.random() > 0.6 && inventoryService != null) {",
            "                   inventoryService.releaseStock(...);  // only 40% chance",
            "               } else {",
            "                   logStore.warn('STOCK_NOT_RELEASED', ...);  // 60% skipped",
            "               }",
            "  Impact      : Reserved stock accumulates and is never returned to available pool.",
            "               New orders rejected for 'insufficient stock' despite cancellations.",
            "  Code Fix    : Remove the Math.random() guard — always release on cancel.",
        ],
        "action": "cancel_order",
        "action_label": "POST /order/cancel",
        "action_reason": "Re-trigger cancel to attempt stock release (40% chance each call)",
    },
    {
        "id":        "INC-005",
        "pattern":   "STUCK_ORDER",
        "severity":  "MEDIUM",
        "triggers":  {"ORDER_STUCK_CREATED", "ORDER_PIPELINE_STALL", "CREATED_STATE_TIMEOUT"},
        "msg_hints": ["stuck in created", "pipeline stall", "no state transition",
                      "remains uncommitted", "inv phase may not have completed"],
        "title":     "Stuck Order — order frozen in CREATED state",
        "rca": [
            "  Root Cause  : OrderService.createOrder() calls shouldFail() which has a 25%",
            "               chance of triggering early return — skipping inventory reservation",
            "               and leaving the order in CREATED instead of RESERVED or FAILED.",
            "  Code Path   : OrderService.java:79-91",
            "               if (shouldFail()) {",
            "                   logStore.warn('RESERVATION_PHASE_FAILURE', ...);",
            "                   schedulePostCreationAudit(orderId, traceId);",
            "                   return order;  // <-- exits without setting status",
            "               }",
            "  Impact      : Order sits in CREATED — cannot proceed to payment.",
            "               Async audit fires but cannot fix state.",
            "  Code Fix    : Set order.setStatus(FAILED) before the early return.",
        ],
        "action": "cancel_order",
        "action_label": "POST /order/cancel",
        "action_reason": "Cancel the stuck order to free any partial reservations",
    },
    {
        "id":        "INC-006",
        "pattern":   "PAID_CANCELLED_ORDER",
        "severity":  "CRITICAL",
        "triggers":  {"PAID_AFTER_CANCEL", "STATE_MACHINE_VIOLATION", "ORDER_STATE_CONFLICT"},
        "msg_hints": ["terminal state", "cancelled state", "paid from cancelled",
                      "state transition conflict", "payment accepted while order"],
        "title":     "State Machine Violation — order PAID after CANCELLED",
        "rca": [
            "  Root Cause  : OrderService.markOrderPaid() checks for CANCELLED status and logs",
            "               a warning but does not return false — it falls through and marks",
            "               the order PAID regardless.",
            "  Code Path   : OrderService.java:191-201",
            "               if (order.getStatus() == CANCELLED) {",
            "                   log.warn('Payment accepted while order in terminal state');",
            "               }  // <-- no return false here!",
            "               order.setStatus(PAID);  // always executes",
            "  Impact      : Order in contradictory state — PAID but was CANCELLED.",
            "               Triggers downstream fulfilment for a cancelled order.",
            "  Code Fix    : Add 'return false' inside the CANCELLED check.",
        ],
        "action": "refund_payment",
        "action_label": "POST /refund",
        "action_reason": "Reverse the payment applied to the cancelled order",
    },
    {
        "id":        "INC-007",
        "pattern":   "ORDER_SYNC_FAILURE",
        "severity":  "HIGH",
        "triggers":  {"PAY_PARTIAL_WRITE", "ORDER_SYNC_FAILURE", "PARTIAL_COMMIT",
                      "ORDER_UPDATE_FAILURE", "DB_WRITE_FAILURE"},
        "msg_hints": ["not updated to paid", "partial write", "order sync failed",
                      "order status update failed", "write did not complete",
                      "persisted but orderId"],
        "title":     "Order Sync Failure — payment committed, order status not updated",
        "rca": [
            "  Root Cause  : OrderService.markOrderPaid() has a 10% random DB write failure:",
            "  Code Path   : OrderService.java:203-208",
            "               if (Math.random() < 0.1) {",
            "                   logStore.error('DB_WRITE_FAILURE', ...);",
            "                   return false;  // payment already committed above!",
            "               }",
            "               order.setStatus(PAID);",
            "  Impact      : Payment record exists but order status stays RESERVED/CREATED.",
            "               Customer charged but order appears unpaid — may be retried.",
            "  Code Fix    : Wrap payment commit + order update in a transaction.",
        ],
        "action": None,
        "action_label": "ALERT ONLY",
        "action_reason": "Cannot re-trigger order status sync via API — requires manual reconciliation",
    },
]


# ── HTTP ───────────────────────────────────────────────────────────────────────
def fetch_logs() -> list[dict]:
    r = requests.get(f"{BASE_URL}/logs", timeout=5)
    r.raise_for_status()
    return r.json()

def fetch_inventory() -> dict:
    r = requests.get(f"{BASE_URL}/inventory", timeout=5)
    r.raise_for_status()
    return r.json()

def api_cancel(order_id: str) -> tuple[int, str]:
    r = requests.post(f"{BASE_URL}/order/cancel", json={"orderId": order_id}, timeout=5)
    return r.status_code, r.text[:100]

def api_refund(order_id: str) -> tuple[int, str]:
    r = requests.post(f"{BASE_URL}/refund", json={"orderId": order_id}, timeout=5)
    return r.status_code, r.text[:100]


# ── Helpers ────────────────────────────────────────────────────────────────────
def sep(char="─", width=72, col=DIM):
    print(f"{col}{char * width}{RESET}")

def slow_print(text: str, delay: float = 0.018):
    for ch in text:
        print(ch, end="", flush=True)
        time.sleep(delay)
    print()

def match_incident(entry: dict, inc: dict) -> bool:
    et  = (entry.get("error_type") or "").upper()
    msg = (entry.get("message") or "").lower()
    return (et in inc["triggers"]) or any(h in msg for h in inc["msg_hints"])

def extract_uuid(text: str) -> str:
    m = _UUID.search(text)
    return m.group(0) if m else ""


# ── Main demo ──────────────────────────────────────────────────────────────────
def main():
    # Header
    print()
    sep("=", col=CYAN)
    print(f"{BOLD}{CYAN}   AI-Powered Self-Healing System  —  Live Incident Demo{RESET}")
    print(f"{CYAN}   Backend: {BASE_URL}   |   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    sep("=", col=CYAN)

    # ── Phase 1: Log ingestion ──────────────────────────────────────────────
    print(f"\n{BOLD}{BLUE}PHASE 1  LOG INGESTION{RESET}")
    sep()
    print(f"  Connecting to {CYAN}{BASE_URL}/logs{RESET} ...", end="", flush=True)
    time.sleep(0.4)
    logs = fetch_logs()
    errors = [e for e in logs if e.get("level") in ("ERROR", "WARN")]
    print(f"  {GREEN}OK{RESET}")
    print(f"  {len(logs)} total entries  |  {BOLD}{RED}{len(errors)}{RESET} ERROR/WARN flagged for analysis\n")

    print(f"  {BOLD}Streaming incoming incidents:{RESET}")
    shown = 0
    for entry in errors[:18]:
        lvl = entry.get("level", "")
        col = RED if lvl == "ERROR" else YELLOW
        svc = entry.get("service", "?")[:16]
        et  = entry.get("error_type") or ""
        msg = entry.get("message", "")[:65]
        ts  = entry.get("timestamp", "")[:19].replace("T", " ")
        print(f"  {DIM}{ts}{RESET}  {col}{BOLD}{lvl:<5}{RESET}  [{svc:<16}]  ", end="")
        if et and et != "NONE":
            print(f"{MAGENTA}[{et}]{RESET}  ", end="")
        print(f"{msg}")
        time.sleep(0.06)
        shown += 1
    if len(errors) > shown:
        print(f"  {DIM}... {len(errors) - shown} more entries queued for analysis{RESET}")

    time.sleep(0.5)

    # ── Phase 2: Inventory snapshot ─────────────────────────────────────────
    print(f"\n{BOLD}{BLUE}PHASE 2  INVENTORY SNAPSHOT{RESET}")
    sep()
    inv = fetch_inventory()
    print(f"  {'Product':<12} {'Name':<22} {'Stock':>8}  {'Reserved':>9}  {'Status'}")
    sep("-", 72, DIM)
    for pid, item in inv.items():
        stock = item.get("stock", 0)
        res   = item.get("reservedStock", 0)
        name  = item.get("name", "")[:20]
        if stock < 0:
            status = f"{RED}{BOLD}CRITICAL — NEGATIVE{RESET}"
            scol   = RED
        elif stock == 0:
            status = f"{YELLOW}{BOLD}OUT OF STOCK{RESET}"
            scol   = YELLOW
        elif stock < 5:
            status = f"{YELLOW}LOW{RESET}"
            scol   = YELLOW
        else:
            status = f"{GREEN}OK{RESET}"
            scol   = GREEN
        print(f"  {pid:<12} {name:<22} {scol}{BOLD}{stock:>8}{RESET}  {res:>9}  {status}")
        time.sleep(0.08)

    time.sleep(0.4)

    # ── Phase 3: Incident detection ─────────────────────────────────────────
    print(f"\n{BOLD}{BLUE}PHASE 3  INCIDENT DETECTION  &  ROOT CAUSE ANALYSIS{RESET}")
    sep()
    print(f"  {DIM}Scanning {len(errors)} log entries against {len(INCIDENTS)} known failure patterns...{RESET}\n")
    time.sleep(0.6)

    detected: list[dict] = []   # {incident, matched_logs, entity}
    used_patterns: set[str] = set()

    for inc in INCIDENTS:
        matched = [e for e in errors if match_incident(e, inc)]
        if not matched:
            continue
        if inc["pattern"] in used_patterns:
            continue
        used_patterns.add(inc["pattern"])

        # Find best entity
        entity = ""
        for e in matched:
            entity = extract_uuid(e.get("message", ""))
            if entity:
                break

        detected.append({"inc": inc, "logs": matched[:3], "entity": entity})

    print(f"  {BOLD}{RED}{len(detected)} INCIDENTS DETECTED{RESET}\n")
    time.sleep(0.3)

    for idx, det in enumerate(detected, 1):
        inc    = det["inc"]
        mlogs  = det["logs"]
        entity = det["entity"]

        sev_col = RED if inc["severity"] in ("CRITICAL", "HIGH") else YELLOW

        # Incident header
        print(f"{sev_col}{BOLD}  ┌─ [{inc['id']}] {inc['severity']} — {inc['pattern']}{RESET}")
        print(f"{sev_col}{BOLD}  │  {inc['title']}{RESET}")
        print(f"{sev_col}  │{RESET}")

        # Evidence
        print(f"{sev_col}  │{RESET}  {BOLD}Evidence from logs:{RESET}")
        for e in mlogs:
            lvl = e.get("level", "")
            col = RED if lvl == "ERROR" else YELLOW
            msg = e.get("message", "")[:70]
            et  = e.get("error_type") or ""
            svc = e.get("service", "")[:14]
            print(f"{sev_col}  │{RESET}    {col}{lvl:<5}{RESET}  [{svc}]  {DIM}{msg}{RESET}")
            if et and et != "NONE":
                print(f"{sev_col}  │{RESET}           error_type={MAGENTA}{et}{RESET}")
            time.sleep(0.04)

        # RCA
        print(f"{sev_col}  │{RESET}")
        print(f"{sev_col}  │{RESET}  {BOLD}Root Cause Analysis:{RESET}")
        for line in inc["rca"]:
            print(f"{sev_col}  │{RESET}  {DIM}{line}{RESET}")
            time.sleep(0.03)

        # Entity
        if entity:
            print(f"{sev_col}  │{RESET}")
            print(f"{sev_col}  │{RESET}  {BOLD}Affected Entity:{RESET}  {CYAN}{entity}{RESET}")

        print(f"{sev_col}  └{'─'*68}{RESET}\n")
        time.sleep(0.2)

    # ── Phase 4: Healing actions ─────────────────────────────────────────────
    print(f"\n{BOLD}{BLUE}PHASE 4  AUTOMATED HEALING ACTIONS{RESET}")
    sep()
    print(f"  Executing corrective API calls for {len(detected)} incidents...\n")
    time.sleep(0.4)

    healed: set[str] = set()
    total_ok = 0
    total_fail = 0

    for det in detected:
        inc    = det["inc"]
        entity = det["entity"]
        action = inc["action"]
        sev_col = RED if inc["severity"] in ("CRITICAL", "HIGH") else YELLOW

        print(f"  {BOLD}[{inc['id']}]{RESET} {sev_col}{inc['pattern']}{RESET}")
        print(f"         Action  : {BOLD}{BLUE}{inc['action_label']}{RESET}")
        print(f"         Reason  : {inc['action_reason']}")

        if action is None:
            print(f"         Result  : {YELLOW}ALERT ONLY — manual intervention required{RESET}\n")
            continue

        if not entity:
            print(f"         Result  : {YELLOW}No orderId extracted from logs — skipping automated fix{RESET}\n")
            continue

        ikey = f"{action}:{entity}"
        if ikey in healed:
            print(f"         Result  : {DIM}Already healed for {entity} — skipping{RESET}\n")
            continue
        healed.add(ikey)

        print(f"         Payload : {{'orderId': '{entity}'}}")
        print(f"         Calling : {CYAN}{BASE_URL}{inc['action_label'].split(' ', 1)[-1]}{RESET} ...", end="", flush=True)
        time.sleep(0.3)

        if action == "refund_payment":
            status, body = api_refund(entity)
        elif action == "cancel_order":
            status, body = api_cancel(entity)
        elif action == "check_inventory":
            inv2 = fetch_inventory()
            neg  = {k: v.get("stock", 0) for k, v in inv2.items() if v.get("stock", 0) < 0}
            print(f"  {GREEN}OK{RESET}")
            if neg:
                print(f"         {RED}NEGATIVE STOCK DETECTED: {neg}{RESET}")
            else:
                print(f"         {GREEN}All stock levels non-negative{RESET}")
            print()
            total_ok += 1
            continue
        else:
            status, body = -1, "unknown action"

        if 200 <= status < 300:
            print(f"  {GREEN}{BOLD}HTTP {status} OK{RESET}")
            print(f"         {GREEN}Fix applied successfully{RESET}")
            total_ok += 1
        elif status == 409:
            print(f"  {YELLOW}HTTP 409 — already refunded{RESET}")
            total_ok += 1
        else:
            print(f"  {RED}HTTP {status} FAILED{RESET}  {DIM}{body}{RESET}")
            total_fail += 1
        print()
        time.sleep(0.15)

    # ── Phase 5: Summary ─────────────────────────────────────────────────────
    sep("=", col=CYAN)
    print(f"\n{BOLD}{CYAN}  DEMO SUMMARY{RESET}\n")
    print(f"  Logs analysed    : {len(errors)}")
    print(f"  Incidents raised : {BOLD}{RED}{len(detected)}{RESET}")

    # severity breakdown
    crit = sum(1 for d in detected if d["inc"]["severity"] == "CRITICAL")
    high = sum(1 for d in detected if d["inc"]["severity"] == "HIGH")
    med  = sum(1 for d in detected if d["inc"]["severity"] == "MEDIUM")
    print(f"  Severity         : {RED}CRITICAL={crit}{RESET}  {RED}HIGH={high}{RESET}  {YELLOW}MEDIUM={med}{RESET}")
    print(f"  Fixes applied    : {GREEN}{BOLD}{total_ok} OK{RESET}  {RED}{total_fail} FAILED{RESET}")

    print(f"\n  {BOLD}Incidents detected:{RESET}")
    for det in detected:
        inc     = det["inc"]
        sev_col = RED if inc["severity"] in ("CRITICAL", "HIGH") else YELLOW
        fix_tag = f"{GREEN}[FIXED]{RESET}" if (inc["action"] and det["entity"]) else f"{YELLOW}[ALERT]{RESET}"
        print(f"    {fix_tag}  {sev_col}{inc['severity']:<8}{RESET}  {BOLD}{inc['pattern']:<25}{RESET}  {inc['title']}")

    print()
    sep("=", col=CYAN)
    print(f"{BOLD}{CYAN}  Self-healing cycle complete.{RESET}")
    sep("=", col=CYAN)
    print()


if __name__ == "__main__":
    main()
