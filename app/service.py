"""Business logic shared by the web UI, the MCP server and the buyer agent.

Every entry point (human or agent) goes through these functions, so the same
rules and the same PayPal flow apply no matter who is ordering.
"""

from __future__ import annotations

import math
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import rules
from .config import settings
from .db import dumps, log_event, now_iso, query, query_one, tx
from zoneinfo import ZoneInfo

from .paypal import PayPalError, capture_from_order, issue_of, paypal
from .rules import RuleViolation

# Statuses that consume capacity (money captured, being captured, or under PayPal review).
ACTIVE_STATUSES = ("paid", "ready", "picked_up", "shipped", "capturing", "payment_review")
# Final statuses: never capture again.
TERMINAL_STATUSES = ("refunded", "sold_out", "cancelled", "needs_attention")
MAX_PENDING_PER_DAY = 15  # unpaid holds per stall per pickup date (anti hold-squatting)
STATUS_LABELS = {
    "pending_payment": "Waiting for PayPal approval",
    "capturing": "Confirming payment with PayPal",
    "payment_review": "Payment pending at PayPal (do not hand over goods yet)",
    "sold_out": "Sold out before payment. You were not charged",
    "needs_attention": "Payment issue: seller has been alerted",
    "paid": "Paid",
    "ready": "Ready for pickup",
    "picked_up": "Picked up",
    "shipped": "Shipped",
    "refunded": "Refunded",
    "cancelled": "Cancelled",
    "expired": "Expired (not paid in time)",
    "payment_failed": "Payment failed",
}


# --------------------------------------------------------------------- helpers
def today_local(tz_name: str | None = None) -> date:
    """'Today' in the seller's timezone, not the server's (Render runs in UTC)."""
    return datetime.now(ZoneInfo(tz_name or settings.default_timezone)).date()


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "stall"
    slug, n = base, 2
    while query_one("SELECT 1 FROM stalls WHERE id=?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def fmt_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------- stalls
def create_stall(data: dict[str, Any]) -> dict[str, Any]:
    """Create a stall + items from a confirmed catalog. Returns stall incl. owner_key."""
    name = (data.get("name") or "").strip()
    if not name:
        raise RuleViolation("missing_name", "A stall needs a name.")
    items = data.get("items") or []
    if not items:
        raise RuleViolation("missing_items", "Add at least one item.")
    windows = [
        {"day": rules.normalize_day(w.get("day", "")), "start": w.get("start", "09:00"), "end": w.get("end", "12:00")}
        for w in data.get("pickup_windows") or []
        if w.get("day")
    ]
    for w in windows:
        for key in ("start", "end"):
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", w[key]):
                raise RuleViolation("invalid_window", f"Pickup times must be HH:MM (24h), got {w[key]!r}")
        if w["end"] <= w["start"]:
            raise RuleViolation("invalid_window", f"Pickup window {w['start']}-{w['end']} ends before it starts.")
        if w["day"] not in rules.DAYS + ["any"]:
            raise RuleViolation("invalid_window", f"Unknown pickup day {w['day']!r}.")
    stall_id = slugify(name)
    owner_key = data.get("owner_key") or secrets.token_urlsafe(16)
    with tx() as c:
        c.execute(
            """INSERT INTO stalls (id,name,description,category,city,lat,lng,pickup_address,paypal_email,
               timezone,lead_time_hours,pickup_windows,ships,owner_key,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stall_id, name, data.get("description", ""), data.get("category", ""), data.get("city", ""),
                data.get("lat"), data.get("lng"), data.get("pickup_address", ""),
                (data.get("paypal_email") or "").strip(), data.get("timezone") or settings.default_timezone,
                int(data.get("lead_time_hours") or 24), dumps(windows), 1 if data.get("ships") else 0,
                owner_key, now_iso(),
            ),
        )
        for it in items:
            price_cents = to_cents(it.get("price"))
            if price_cents is None or price_cents <= 0:
                raise RuleViolation("invalid_price", f"Item {it.get('name')!r} needs a positive price.")
            cap = it.get("daily_capacity")
            c.execute(
                """INSERT INTO items (stall_id,name,description,price_cents,unit,allergens,tags,daily_capacity)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    stall_id, (it.get("name") or "Item").strip(), it.get("description", ""), price_cents,
                    it.get("unit") or "each", dumps(it.get("allergens") or []), dumps(it.get("tags") or []),
                    int(cap) if cap not in (None, "") else None,
                ),
            )
    log_event("stall_created", stall_id=stall_id)
    stall = get_stall(stall_id, include_secret=True)
    assert stall
    return stall


def to_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
    try:
        return int((Decimal(str(value).replace("$", "").replace(",", "").strip()) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def get_stall(stall_id: str, include_secret: bool = False) -> dict[str, Any] | None:
    stall = query_one("SELECT * FROM stalls WHERE id=?", (stall_id,))
    if not stall:
        return None
    if not include_secret:
        stall.pop("owner_key", None)
        stall.pop("paypal_email", None)
    stall["items"] = query("SELECT * FROM items WHERE stall_id=? AND active=1 ORDER BY id", (stall_id,))
    return stall


def check_owner(stall_id: str, key: str | None) -> dict[str, Any]:
    stall = get_stall(stall_id, include_secret=True)
    if not stall or not key or not secrets.compare_digest(stall["owner_key"], key):
        raise PermissionError("Invalid stall or owner key")
    return stall


def public_item(it: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": it["id"],
        "name": it["name"],
        "description": it["description"],
        "price": fmt_money(it["price_cents"]),
        "price_cents": it["price_cents"],
        "unit": it["unit"],
        "allergens": it["allergens"],
        "tags": it["tags"],
        "daily_capacity": it["daily_capacity"],
    }


def stall_summary(stall: dict[str, Any], distance_km: float | None = None) -> dict[str, Any]:
    out = {
        "stall_id": stall["id"],
        "name": stall["name"],
        "category": stall["category"],
        "description": stall["description"],
        "city": stall["city"],
        "pickup_days": sorted({w["day"] for w in stall["pickup_windows"]}),
        "lead_time_hours": stall["lead_time_hours"],
        "ships": bool(stall["ships"]),
        "url": f"{settings.base_url}/s/{stall['id']}",
    }
    if distance_km is not None:
        out["distance_km"] = round(distance_km, 1)
    return out


def find_stalls(
    query_text: str = "", lat: float | None = None, lng: float | None = None, radius_km: float = 40, limit: int = 10
) -> list[dict[str, Any]]:
    """Keyword + distance search across stalls and their items."""
    words = [w for w in re.findall(r"[a-z0-9]+", (query_text or "").lower()) if len(w) > 2]
    results = []
    for stall in query("SELECT * FROM stalls ORDER BY created_at DESC"):
        items = query("SELECT name, description, tags, allergens FROM items WHERE stall_id=? AND active=1", (stall["id"],))
        haystack = " ".join(
            [stall["name"], stall["description"], stall["category"], stall["city"]]
            + [f"{i['name']} {i['description']} {' '.join(i['tags'])}" for i in items]
        ).lower()
        score = sum(1 for w in words if w in haystack) if words else 1
        if words and score == 0:
            continue
        dist = None
        if lat is not None and lng is not None and stall["lat"] is not None:
            dist = haversine_km(lat, lng, stall["lat"], stall["lng"])
            if dist > radius_km and not stall["ships"]:
                continue
        results.append((score, -(dist or 0), stall_summary(stall, dist)))
    results.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [r[2] for r in results[:limit]]


# ---------------------------------------------------------------- availability
def reconcile_stuck_captures(older_than_minutes: int = 5) -> int:
    """Orders left in 'capturing' (process crashed mid-capture) are reconciled with PayPal's truth."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat(timespec="seconds")
    fixed = 0
    for o in query("SELECT * FROM orders WHERE status='capturing' AND updated_at < ?", (cutoff,)):
        try:
            cap = capture_from_order(paypal.get_order(o["paypal_order_id"]))
        except PayPalError:
            continue  # try again next time
        if cap:
            _finalize_capture(o, cap)
        else:
            _set_status(o["id"], "pending_payment")
        fixed += 1
    return fixed


def _expire_stale_holds() -> None:
    reconcile_stuck_captures()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=settings.hold_minutes)).isoformat(timespec="seconds")
    stale = query("SELECT id, stall_id FROM orders WHERE status='pending_payment' AND created_at < ?", (cutoff,))
    if not stale:
        return
    with tx() as c:
        for o in stale:
            c.execute("UPDATE orders SET status='expired', updated_at=? WHERE id=?", (now_iso(), o["id"]))
    for o in stale:
        log_event("order_expired", stall_id=o["stall_id"], order_id=o["id"])


def reserved_quantities(stall_id: str, pickup_date: str, *, include_pending: bool = True, exclude_order: str | None = None) -> dict[int, int]:
    statuses = list(ACTIVE_STATUSES) + (["pending_payment"] if include_pending else [])
    marks = ",".join("?" * len(statuses))
    rows = query(
        f"SELECT id, lines FROM orders WHERE stall_id=? AND pickup_date=? AND status IN ({marks})",
        (stall_id, pickup_date, *statuses),
    )
    out: dict[int, int] = {}
    for r in rows:
        if r["id"] == exclude_order:
            continue
        for line in r["lines"]:
            out[line["item_id"]] = out.get(line["item_id"], 0) + line["quantity"]
    return out


def check_availability(stall_id: str, pickup_date: str) -> dict[str, Any]:
    stall = get_stall(stall_id)
    if not stall:
        raise RuleViolation("unknown_stall", f"No stall with id {stall_id!r}.")
    d = rules.parse_date(pickup_date)
    _expire_stale_holds()
    reserved = reserved_quantities(stall_id, d.isoformat())
    windows = rules.windows_for_date(stall["pickup_windows"], d)
    bookable_window = None
    reason = None
    try:
        bookable_window = rules.resolve_window(stall, d, None)
    except RuleViolation as e:
        reason = e.message
    return {
        "stall_id": stall_id,
        "pickup_date": d.isoformat(),
        "weekday": d.strftime("%A"),
        "open": bookable_window is not None,
        "reason_closed": reason,
        "pickup_windows": [rules.window_label(w) for w in windows],
        "pickup_address": stall["pickup_address"],
        "items": [
            {
                "item_id": it["id"],
                "name": it["name"],
                "price": fmt_money(it["price_cents"]),
                "remaining": None if it["daily_capacity"] is None else max(it["daily_capacity"] - reserved.get(it["id"], 0), 0),
            }
            for it in stall["items"]
        ],
    }


# --------------------------------------------------------------------- orders
def place_order(
    stall_id: str,
    lines: list[dict[str, Any]],
    pickup_date: str,
    *,
    buyer_name: str = "",
    note: str = "",
    pickup_window: str | None = None,
    channel: str = "web",
) -> dict[str, Any]:
    """Validate against seller rules, hold capacity, create the PayPal order.

    Returns the order plus a PayPal approval URL the HUMAN must open. Agents
    never complete payment themselves.
    """
    stall = get_stall(stall_id, include_secret=True)
    if not stall:
        raise RuleViolation("unknown_stall", f"No stall with id {stall_id!r}.")
    d = rules.parse_date(pickup_date)
    if d < today_local(stall["timezone"]):
        raise RuleViolation("past_date", "Pickup date is in the past.")
    window = rules.resolve_window(stall, d, pickup_window)
    items_by_id = {it["id"]: it for it in stall["items"]}
    priced = rules.price_lines(items_by_id, lines, max_items=settings.max_items_per_order)
    total = rules.check_total(priced, settings.max_order_cents)

    _expire_stale_holds()
    order_id = "OS-" + uuid.uuid4().hex[:10].upper()
    # Capacity check and hold insert happen in one serialized transaction.
    with tx() as c:
        pending = c.execute(
            "SELECT COUNT(*) FROM orders WHERE stall_id=? AND pickup_date=? AND status='pending_payment'",
            (stall_id, d.isoformat()),
        ).fetchone()[0]
        if pending >= MAX_PENDING_PER_DAY:
            raise RuleViolation(
                "too_many_unpaid_orders",
                "This stall has too many unpaid orders for that day. Please try again in a few minutes.",
            )
        rules.check_capacity(priced, items_by_id, reserved_quantities(stall_id, d.isoformat()))
        ts = now_iso()
        c.execute(
            """INSERT INTO orders (id,stall_id,buyer_name,buyer_note,pickup_date,pickup_window,lines,subtotal_cents,
               status,channel,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, stall_id, buyer_name[:80], note[:500], d.isoformat(), window,
             dumps([l.as_dict() for l in priced]), total, "pending_payment", channel, ts, ts),
        )

    try:
        created = paypal.create_order(
            reference_id=order_id,
            lines=[{"name": l.name, "quantity": l.quantity, "unit_cents": l.unit_cents} for l in priced],
            total_cents=total,
            description=f"{stall['name']} pickup {d.isoformat()} {window}",
            payee_email=stall["paypal_email"] or None,
            brand_name=stall["name"],
            return_url=f"{settings.base_url}/paypal/return?order={order_id}",
            cancel_url=f"{settings.base_url}/paypal/cancel?order={order_id}",
        )
    except PayPalError as e:
        # Release the hold so a PayPal problem never blocks capacity. No money has moved.
        _set_status(order_id, "payment_failed")
        issue = issue_of(e)
        log_event("paypal_create_failed", stall_id=stall_id, order_id=order_id, error=str(e), issue=issue, debug_id=e.debug_id)
        if issue.startswith("PAYEE_ACCOUNT"):
            raise RuleViolation("seller_cannot_receive", f"{stall['name']}'s PayPal account can't receive payments yet. No payment was taken.")
        raise RuleViolation("payment_unavailable", "PayPal is unavailable right now. No payment was taken; please retry.")

    with tx() as c:
        c.execute(
            "UPDATE orders SET paypal_order_id=?, approve_url=?, updated_at=? WHERE id=?",
            (created.id, created.approve_url, now_iso(), order_id),
        )
    log_event("order_created", stall_id=stall_id, order_id=order_id, channel=channel, total_cents=total)
    return order_view(order_id)


def _set_status(order_id: str, status: str, **fields: Any) -> None:
    sets = ", ".join([f"{k}=?" for k in fields] + ["status=?", "updated_at=?"])
    with tx() as c:
        c.execute(f"UPDATE orders SET {sets} WHERE id=?", (*fields.values(), status, now_iso(), order_id))


def get_order(order_id: str) -> dict[str, Any] | None:
    return query_one("SELECT * FROM orders WHERE id=?", (order_id,))


def order_view(order_id: str) -> dict[str, Any]:
    o = get_order(order_id)
    if not o:
        raise RuleViolation("unknown_order", f"No order {order_id!r}.")
    stall = get_stall(o["stall_id"]) or {"name": o["stall_id"], "pickup_address": ""}
    view = {
        "order_id": o["id"],
        "stall_id": o["stall_id"],
        "stall_name": stall["name"],
        "status": o["status"],
        "status_label": STATUS_LABELS.get(o["status"], o["status"]),
        "pickup_date": o["pickup_date"],
        "pickup_window": o["pickup_window"],
        "pickup_address": stall.get("pickup_address", ""),
        "lines": o["lines"],
        "total": fmt_money(o["subtotal_cents"]),
        "total_cents": o["subtotal_cents"],
        "channel": o["channel"],
        "tracking": o["tracking"],
    }
    view["steps"] = order_steps(o)
    if o["status"] == "pending_payment" and o["approve_url"]:
        view["paypal_approval_url"] = o["approve_url"]
        view["next_step"] = (
            "Ask the human buyer to open paypal_approval_url and approve the payment in PayPal. "
            "The order is held for %d minutes." % settings.hold_minutes
        )
    return view


# Which events mark each buyer-visible milestone (first occurrence wins).
_MILESTONE_EVENTS = {
    "placed": ("order_created",),
    "paid": ("order_paid", "payment_review"),
    "ready": ("seller_ready",),
    "picked_up": ("seller_picked_up",),
    "shipped": ("seller_ship",),
    "refunded": ("seller_refund", "refunded_by_paypal", "amount_mismatch"),
    "cancelled": ("order_cancelled",),
    "expired": ("order_expired",),
    "sold_out": ("sold_out_not_captured",),
}


def order_steps(o: dict[str, Any]) -> list[dict[str, Any]]:
    """Buyer-facing progress tracker: [{key, label, state: done|current|todo|stopped, at}]."""
    times: dict[str, str] = {}
    for e in query("SELECT kind, created_at FROM events WHERE order_id=? ORDER BY id", (o["id"],)):
        for key, kinds in _MILESTONE_EVENTS.items():
            if e["kind"] in kinds and key not in times:
                times[key] = e["created_at"]
    times.setdefault("placed", o["created_at"])

    status = o["status"]
    if status in ("cancelled", "expired", "sold_out", "payment_failed"):
        label = {"cancelled": "Cancelled", "expired": "Expired, not paid in time",
                 "sold_out": "Sold out, not charged", "payment_failed": "Payment failed, not charged"}[status]
        flow = [("placed", "Order placed"), (status, label)]
        stop = status
    elif status in ("refunded", "needs_attention"):
        flow = [("placed", "Order placed"), ("paid", "Paid with PayPal"),
                ("refunded", "Refunded to PayPal" if status == "refunded" else "Refund being fixed by the seller")]
        stop = "refunded"
    elif status == "shipped":
        flow = [("placed", "Order placed"), ("paid", "Paid with PayPal"), ("shipped", "Shipped")]
        stop = None
    else:
        flow = [("placed", "Order placed"), ("paid", "Paid with PayPal"),
                ("ready", "Ready for pickup"), ("picked_up", "Picked up")]
        stop = None

    # Number of milestones already completed (including "placed").
    reached = {
        "pending_payment": 1, "capturing": 1, "payment_review": 1, "paid": 2, "ready": 3, "picked_up": 4,
        "shipped": 3, "refunded": 3, "needs_attention": 3,
        "cancelled": 2, "expired": 2, "sold_out": 2, "payment_failed": 2,
    }.get(status, 1)
    steps = []
    for i, (key, label) in enumerate(flow):
        if i < reached:
            state = "stopped" if key == stop else "done"
        elif i == reached:
            state = "current"
        else:
            state = "todo"
        steps.append({"key": key, "label": label, "state": state, "at": times.get(key)})
    return steps


def complete_payment(order_id: str) -> dict[str, Any]:
    """Buyer returned from PayPal approval (or CHECKOUT.ORDER.APPROVED webhook). Idempotent.

    Discipline: check stock BEFORE moving money. Capacity is reserved by flipping
    the order to 'capturing' inside the same lock as the check, then we capture.
    """
    o = get_order(order_id)
    if not o:
        raise RuleViolation("unknown_order", f"No order {order_id!r}.")
    if o["status"] in ACTIVE_STATUSES or o["status"] in TERMINAL_STATUSES:
        msg = {"cancelled": "This order was cancelled, so no payment was taken."}.get(o["status"])
        return {**order_view(order_id), **({"error": msg} if msg else {})}
    if not o["paypal_order_id"]:
        raise RuleViolation("no_payment", "This order has no PayPal payment attached.")

    # Phase 1: reserve stock against money already captured / being captured.
    stall = get_stall(o["stall_id"])
    items_by_id = {it["id"]: it for it in stall["items"]} if stall else {}
    priced = [rules.PricedLine(l["item_id"], l["name"], l["quantity"], l["unit_cents"]) for l in o["lines"]]
    with tx() as c:
        current = c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0]
        if current not in ("pending_payment", "expired", "payment_failed"):
            return order_view(order_id)  # someone else (webhook / refresh) is handling it
        committed = reserved_quantities(o["stall_id"], o["pickup_date"], include_pending=False, exclude_order=order_id)
        try:
            rules.check_capacity(priced, items_by_id, committed)
        except RuleViolation as e:
            c.execute("UPDATE orders SET status='sold_out', updated_at=? WHERE id=?", (now_iso(), order_id))
            sold_out_reason = e.message
        else:
            c.execute("UPDATE orders SET status='capturing', updated_at=? WHERE id=?", (now_iso(), order_id))
            sold_out_reason = None
    if sold_out_reason:
        # Approval alone moves no money, so nothing to refund.
        log_event("sold_out_not_captured", stall_id=o["stall_id"], order_id=order_id, reason=sold_out_reason)
        return {**order_view(order_id), "error": "Sorry, it sold out before you paid. You were not charged."}

    # Phase 2: capture (idempotent via PayPal-Request-Id).
    try:
        cap = paypal.capture_order(o["paypal_order_id"])
    except PayPalError as e:
        return _handle_capture_error(o, e)
    return _finalize_capture(o, cap)


def _handle_capture_error(o: dict[str, Any], e: PayPalError) -> dict[str, Any]:
    order_id = o["id"]
    issue = issue_of(e)
    log_event("capture_failed", stall_id=o["stall_id"], order_id=order_id, error=str(e), issue=issue, debug_id=e.debug_id)

    if issue == "ORDER_ALREADY_CAPTURED" or e.status is None or (e.status or 0) >= 500:
        # Ambiguous outcome (timeout / 5xx) or a duplicate: ask PayPal what actually happened.
        try:
            cap = capture_from_order(paypal.get_order(o["paypal_order_id"]))
        except PayPalError:
            cap = None
        if cap:
            return _finalize_capture(o, cap)
        _set_status(order_id, "pending_payment")  # safe: capture retries reuse the same request id
        return {**order_view(order_id), "error": "We're still confirming your payment with PayPal. Refresh in a moment; you won't be charged twice."}
    if issue in ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"):
        _set_status(order_id, "pending_payment")
        return {**order_view(order_id), "error": "This payment hasn't been approved in PayPal yet."}
    if issue in ("INSTRUMENT_DECLINED", "TRANSACTION_REFUSED"):
        _set_status(order_id, "pending_payment")
        return {**order_view(order_id), "error": "Your payment method was declined. Approve again with another funding source."}
    _set_status(order_id, "payment_failed")
    return {**order_view(order_id), "error": "PayPal could not complete the payment. You were not charged."}


def _safe_refund(o: dict[str, Any], capture_id: str, reason: str) -> str:
    """Refund and return the resulting order status. A failed refund is escalated, never swallowed."""
    try:
        paypal.refund_capture(capture_id, note=reason)
        return "refunded"
    except PayPalError as e:
        log_event("refund_failed_needs_attention", stall_id=o["stall_id"], order_id=o["id"], capture_id=capture_id,
                  reason=reason, error=str(e), debug_id=e.debug_id)
        return "needs_attention"


def _finalize_capture(o: dict[str, Any], cap: dict[str, Any]) -> dict[str, Any]:
    """Verify a captured payment against the order, then mark paid / review / refund."""
    order_id = o["id"]
    if cap["amount_cents"] != o["subtotal_cents"] or cap.get("currency", settings.currency) != settings.currency:
        # Never accept an amount or currency that differs from the catalog-priced order.
        status = _safe_refund(o, cap["capture_id"], "Amount mismatch, refunded automatically.")
        _set_status(order_id, status, paypal_capture_id=cap["capture_id"])
        log_event("amount_mismatch", stall_id=o["stall_id"], order_id=order_id, captured=cap["amount_cents"], currency=cap.get("currency"))
        return order_view(order_id)

    current = get_order(order_id)
    if current and current["status"] in ("sold_out", "cancelled"):
        # Money arrived for an order we had already closed (e.g. late webhook): give it back.
        status = _safe_refund(o, cap["capture_id"], "Order was closed before payment completed.")
        _set_status(order_id, status, paypal_capture_id=cap["capture_id"])
        return order_view(order_id)

    if cap["status"] == "COMPLETED":
        _set_status(order_id, "paid", paypal_capture_id=cap["capture_id"])
        log_event("order_paid", stall_id=o["stall_id"], order_id=order_id)
    elif cap["status"] == "PENDING":
        # e.g. eCheck or PayPal review: hold stock but don't release goods until COMPLETED webhook.
        _set_status(order_id, "payment_review", paypal_capture_id=cap["capture_id"])
        log_event("payment_review", stall_id=o["stall_id"], order_id=order_id)
    else:  # DECLINED / FAILED
        _set_status(order_id, "payment_failed", paypal_capture_id=cap["capture_id"])
        log_event("capture_declined", stall_id=o["stall_id"], order_id=order_id, capture_status=cap["status"])
        return {**order_view(order_id), "error": "PayPal declined the payment. You were not charged."}
    return order_view(order_id)


def confirm_if_approved(order_id: str) -> dict[str, Any]:
    """Safe to poll from the browser: captures only once PayPal reports the buyer approved."""
    o = get_order(order_id)
    if not o:
        raise RuleViolation("unknown_order", f"No order {order_id!r}.")
    if o["status"] not in ("pending_payment", "expired", "payment_failed") or not o["paypal_order_id"]:
        return order_view(order_id)
    try:
        pp_status = paypal.get_order(o["paypal_order_id"]).get("status")
    except PayPalError:
        return order_view(order_id)
    if pp_status in ("APPROVED", "COMPLETED"):
        return complete_payment(order_id)
    return order_view(order_id)


def cancel_order(order_id: str) -> dict[str, Any]:
    o = get_order(order_id)
    if o and o["status"] == "pending_payment":
        _set_status(order_id, "cancelled")
        log_event("order_cancelled", stall_id=o["stall_id"], order_id=order_id)
    return order_view(order_id)


def seller_update_order(stall_id: str, order_id: str, action: str, **kw: Any) -> dict[str, Any]:
    o = get_order(order_id)
    if not o or o["stall_id"] != stall_id:
        raise RuleViolation("unknown_order", "Order not found for this stall.")
    if action == "ready" and o["status"] == "paid":
        _set_status(order_id, "ready")
    elif action == "picked_up" and o["status"] in ("paid", "ready"):
        _set_status(order_id, "picked_up")
    elif action == "refund" and o["status"] in ("paid", "ready", "needs_attention"):
        try:
            paypal.refund_capture(o["paypal_capture_id"], note=kw.get("note") or "Refunded by the seller.")
        except PayPalError as e:
            raise RuleViolation("refund_failed", f"PayPal refused the refund: {e}. Try again or refund from your PayPal account.")
        _set_status(order_id, "refunded")
    elif action == "ship" and o["status"] in ("paid", "ready"):
        number, carrier = (kw.get("tracking_number") or "").strip(), (kw.get("carrier") or "USPS").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{6,40}", number):
            raise RuleViolation("missing_tracking", "A valid tracking number is required.")
        try:
            res = paypal.add_tracking(o["paypal_order_id"], o["paypal_capture_id"], number, carrier)
        except PayPalError as e:
            raise RuleViolation("tracking_failed", f"PayPal rejected the tracking info: {e}")
        _set_status(order_id, "shipped", tracking=dumps({"number": number, "carrier": carrier, "paypal": res}))
    else:
        raise RuleViolation("invalid_transition", f"Cannot {action} an order that is {o['status']}.")
    log_event(f"seller_{action}", stall_id=stall_id, order_id=order_id)
    return order_view(order_id)


def list_orders(stall_id: str) -> list[dict[str, Any]]:
    return [order_view(o["id"]) for o in query(
        "SELECT id FROM orders WHERE stall_id=? AND COALESCE(buyer_note,'') NOT LIKE '[sample]%' "
        "ORDER BY pickup_date, created_at", (stall_id,)
    )]


def set_daily_capacity(stall_id: str, item_id: int, capacity: int | None) -> dict[str, Any]:
    """Seller (or the seller's Insights assistant, after the seller confirms) changes how many of an item they make per day."""
    if capacity is not None and not (0 <= int(capacity) <= 10000):
        raise RuleViolation("invalid_capacity", "Daily capacity must be between 0 and 10000, or empty for unlimited.")
    with tx() as c:
        row = c.execute("SELECT name, daily_capacity FROM items WHERE id=? AND stall_id=?", (int(item_id), stall_id)).fetchone()
        if not row:
            raise RuleViolation("unknown_item", f"Item {item_id} is not on this stall's menu.")
        c.execute("UPDATE items SET daily_capacity=? WHERE id=? AND stall_id=?",
                  (None if capacity is None else int(capacity), int(item_id), stall_id))
    log_event("capacity_changed", stall_id=stall_id, item_id=int(item_id), item=row[0], before=row[1], after=capacity)
    return {"item_id": int(item_id), "name": row[0], "daily_capacity_before": row[1], "daily_capacity": capacity}


# --------------------------------------------------------------- custom orders
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_OPEN_REQUESTS = 20


def request_custom_order(
    stall_id: str, buyer_email: str, description: str, needed_by: str, budget_cents: int | None = None
) -> dict[str, Any]:
    """An agent ASKS for a custom order. The seller reviews it and sets the price.

    Agents never set prices or trigger invoices themselves, so OpenStall can't be
    used to spam strangers with PayPal invoices.
    """
    stall = get_stall(stall_id)
    if not stall:
        raise RuleViolation("unknown_stall", f"No stall with id {stall_id!r}.")
    if not EMAIL_RE.fullmatch(buyer_email or ""):
        raise RuleViolation("invalid_email", "A valid buyer email is required so the seller can send a PayPal invoice.")
    description = (description or "").strip()
    if len(description) < 5:
        raise RuleViolation("missing_description", "Describe the custom order (what, how many, any dietary needs).")
    d = rules.parse_date(needed_by)
    if d < today_local(stall["timezone"]):
        raise RuleViolation("past_date", "needed_by is in the past.")
    if budget_cents is not None and not (0 < budget_cents <= settings.max_order_cents * 5):
        raise RuleViolation("invalid_amount", "Budget is out of range.")
    open_count = query_one("SELECT COUNT(*) AS n FROM invoices WHERE stall_id=? AND status='requested'", (stall_id,))["n"]
    if open_count >= MAX_OPEN_REQUESTS:
        raise RuleViolation("too_many_requests", "This seller has too many open requests. Please try later.")
    dup = query_one("SELECT id FROM invoices WHERE stall_id=? AND buyer_email=? AND description=? AND status='requested'",
                    (stall_id, buyer_email.lower(), description[:500]))
    ref = dup["id"] if dup else "CO-" + uuid.uuid4().hex[:8].upper()
    if not dup:
        with tx() as c:
            c.execute(
                """INSERT INTO invoices (id,stall_id,buyer_email,description,amount_cents,deposit_cents,due_date,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (ref, stall_id, buyer_email.lower(), description[:500], budget_cents or 0, 0, d.isoformat(), "requested", now_iso()),
            )
        log_event("custom_request", stall_id=stall_id, invoice=ref)
    return {
        "custom_order_id": ref,
        "status": "awaiting_seller_quote",
        "message": f"Request sent to {stall['name']}. If they accept, a PayPal invoice with a deposit will be emailed to {buyer_email}.",
    }


def send_custom_quote(stall_id: str, ref: str, amount_cents: int, deposit_percent: int) -> dict[str, Any]:
    """Seller accepts a request: creates and sends a PayPal invoice with a partial-payment deposit."""
    stall = get_stall(stall_id, include_secret=True)
    req = query_one("SELECT * FROM invoices WHERE id=? AND stall_id=?", (ref, stall_id))
    if not stall or not req:
        raise RuleViolation("unknown_request", "Custom order request not found.")
    if req["status"] != "requested":
        raise RuleViolation("invalid_transition", f"This request is already {req['status']}.")
    if not (100 <= amount_cents <= settings.max_order_cents * 5):
        raise RuleViolation("invalid_amount", "Quote must be between $1 and the platform limit.")
    deposit_percent = min(max(int(deposit_percent), 10), 100)
    deposit = max(amount_cents * deposit_percent // 100, 100)
    try:
        inv = paypal.create_and_send_invoice(
            reference=ref, seller_name=stall["name"], seller_email=stall["paypal_email"] or None,
            buyer_email=req["buyer_email"], description=req["description"][:200],
            amount_cents=amount_cents, deposit_cents=deposit, due_date=req["due_date"],
            note=f"A deposit of {fmt_money(deposit)} confirms your order. Balance due by {req['due_date']}.",
        )
    except PayPalError as e:
        log_event("invoice_failed", stall_id=stall_id, invoice=ref, error=str(e), debug_id=e.debug_id)
        raise RuleViolation("payment_unavailable", f"PayPal could not create the invoice: {e}")
    with tx() as c:
        c.execute("UPDATE invoices SET paypal_invoice_id=?, amount_cents=?, deposit_cents=?, status='sent', pay_url=? WHERE id=?",
                  (inv["id"], amount_cents, deposit, inv["pay_url"], ref))
    log_event("invoice_sent", stall_id=stall_id, invoice=ref, amount_cents=amount_cents)
    return {"custom_order_id": ref, "status": "invoice_sent", "total": fmt_money(amount_cents),
            "deposit_due": fmt_money(deposit), "pay_url": inv["pay_url"]}


def custom_order_status(ref: str) -> dict[str, Any]:
    req = query_one("SELECT id, stall_id, status, amount_cents, deposit_cents, due_date FROM invoices WHERE id=?", (ref,))
    if not req:
        raise RuleViolation("unknown_request", "Custom order request not found.")
    return {"custom_order_id": ref, "status": req["status"], "due_date": req["due_date"],
            "total": fmt_money(req["amount_cents"]) if req["status"] != "requested" else None}


# --------------------------------------------------------------------- webhooks
def _order_for_resource(resource: dict[str, Any]) -> dict[str, Any] | None:
    ref = (resource.get("custom_id") or "").strip()
    if ref and (o := get_order(ref)):
        return o
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    if related.get("order_id") and (o := query_one("SELECT * FROM orders WHERE paypal_order_id=?", (related["order_id"],))):
        return o
    # Refund resources point "up" to their capture.
    for link in resource.get("links") or []:
        if link.get("rel") == "up" and "/captures/" in link.get("href", ""):
            cap_id = link["href"].rstrip("/").split("/")[-1]
            if o := query_one("SELECT * FROM orders WHERE paypal_capture_id=?", (cap_id,)):
                return o
    if resource.get("id") and (o := query_one("SELECT * FROM orders WHERE paypal_capture_id=?", (resource["id"],))):
        return o
    return None


def handle_webhook(event: dict[str, Any]) -> str:
    """Process a verified PayPal webhook. Idempotent; returns a short outcome."""
    etype = event.get("event_type", "")
    resource = event.get("resource") or {}

    if etype == "CHECKOUT.ORDER.APPROVED":
        # Buyer approved but may have closed the tab before returning: capture server-side.
        o = query_one("SELECT id FROM orders WHERE paypal_order_id=?", (resource.get("id"),))
        if not o:
            return "ignored_unknown_order"
        return "order_" + complete_payment(o["id"])["status"]

    if etype.startswith("CUSTOMER.DISPUTE."):
        txns = resource.get("disputed_transactions") or [{}]
        cap_id = txns[0].get("seller_transaction_id")
        o = query_one("SELECT * FROM orders WHERE paypal_capture_id=?", (cap_id,)) if cap_id else None
        log_event("dispute_" + etype.split(".")[-1].lower(), stall_id=o and o["stall_id"], order_id=o and o["id"],
                  reason=resource.get("reason"), dispute_id=resource.get("dispute_id"))
        return "dispute_logged"

    if not etype.startswith("PAYMENT.CAPTURE."):
        return "ignored"
    o = _order_for_resource(resource)
    if not o:
        return "ignored_unknown_order"

    if etype == "PAYMENT.CAPTURE.COMPLETED":
        if o["status"] in ("pending_payment", "capturing", "expired", "payment_failed", "payment_review", "sold_out", "cancelled"):
            try:
                amount = paypal_to_cents(resource)
            except (TypeError, ValueError, ArithmeticError):
                return "ignored_bad_amount"
            cap = {"status": "COMPLETED", "capture_id": resource.get("id"), "amount_cents": amount,
                   "currency": (resource.get("amount") or {}).get("currency_code", settings.currency)}
            return "marked_" + _finalize_capture(o, cap)["status"]
        return "no_change"
    if etype == "PAYMENT.CAPTURE.PENDING" and o["status"] in ("pending_payment", "capturing"):
        _set_status(o["id"], "payment_review", paypal_capture_id=resource.get("id"))
        return "marked_payment_review"
    if etype in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED") and o["status"] not in ("refunded",):
        _set_status(o["id"], "payment_failed")
        log_event("capture_denied", stall_id=o["stall_id"], order_id=o["id"])
        return "marked_payment_failed"
    if etype in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
        try:
            refunded = paypal_to_cents(resource)
        except (TypeError, ValueError, ArithmeticError):
            refunded = o["subtotal_cents"]
        if refunded >= o["subtotal_cents"]:
            if o["status"] != "refunded":
                _set_status(o["id"], "refunded")
                log_event("refunded_by_paypal", stall_id=o["stall_id"], order_id=o["id"])
            return "marked_refunded"
        log_event("partial_refund", stall_id=o["stall_id"], order_id=o["id"], refunded_cents=refunded)
        return "partial_refund_logged"
    return "no_change"


def paypal_to_cents(resource: dict[str, Any]) -> int:
    from .paypal import to_cents as _to_cents
    return _to_cents((resource.get("amount") or {}).get("value"))
