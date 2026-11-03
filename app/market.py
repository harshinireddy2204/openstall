"""Market runs: one basket across several stalls for the same pickup day.

In "single" mode the buyer approves ONE PayPal payment that pays every seller directly:
one Orders v2 order with one purchase unit per stall (payee = that seller, reference_id =
our order id). Each stall keeps its own OpenStall order, capacity hold, refunds and tracking.

Holds are all-or-nothing: if any stall can't take its part, nothing is held and nothing is charged.
If PayPal refuses a multi-seller order, we fall back to "split" mode (one checkout per stall).
"""

from __future__ import annotations

import uuid
from typing import Any

from . import rules
from .config import settings
from .db import log_event, now_iso, query, query_one, tx
from .paypal import PayPalError, captures_by_reference, issue_of, paypal
from .rules import RuleViolation
from . import service

MAX_STALLS = 5
PAID_LIKE = ("paid", "ready", "picked_up", "shipped")


def get_basket(basket_id: str) -> dict[str, Any] | None:
    return query_one("SELECT * FROM baskets WHERE id=?", (basket_id,))


def is_single(basket_id: str) -> bool:
    b = get_basket(basket_id)
    return bool(b and b["mode"] == "single")


def _orders(basket_id: str) -> list[dict[str, Any]]:
    return query("SELECT * FROM orders WHERE basket_id=? ORDER BY rowid", (basket_id,))


# ------------------------------------------------------------------ placing
def place_basket(
    stalls: list[dict[str, Any]],
    pickup_date: str,
    *,
    buyer_name: str = "",
    note: str = "",
    channel: str = "web",
) -> dict[str, Any]:
    """stalls: [{stall_id, items: [{item_id, quantity}], pickup_window?}]."""
    if not isinstance(stalls, list) or not stalls:
        raise RuleViolation("empty_market_order", "Add items from at least one stall.")
    merged: dict[str, dict[str, Any]] = {}
    for part in stalls:
        if not isinstance(part, dict) or not part.get("stall_id"):
            raise RuleViolation("invalid_line", "Each stall entry needs a stall_id and items.")
        entry = merged.setdefault(str(part["stall_id"]), {"items": [], "pickup_window": part.get("pickup_window")})
        entry["items"].extend(part.get("items") or [])
    if len(merged) > MAX_STALLS:
        raise RuleViolation("too_many_stalls", f"A market run can include up to {MAX_STALLS} stalls.")

    # Validate every stall before holding anything. Errors name the stall so agents can fix just that part.
    prepared = []
    for stall_id, part in merged.items():
        try:
            prepared.append(service.prepare_order(stall_id, part["items"], pickup_date, part["pickup_window"]))
        except RuleViolation as e:
            raise _for_stall(e, stall_id)
    grand_total = sum(p[4] for p in prepared)
    if grand_total > settings.max_order_cents:
        raise RuleViolation("over_limit", f"Market runs are limited to {service.fmt_money(settings.max_order_cents)} in total.")

    service._expire_stale_holds()
    basket_id = "MK-" + uuid.uuid4().hex[:10].upper()
    order_ids = [service.new_order_id() for _ in prepared]
    with tx() as c:  # all holds or none
        c.execute("INSERT INTO baskets (id, pickup_date, mode, channel, created_at) VALUES (?,?,?,?,?)",
                  (basket_id, prepared[0][1].isoformat(), "single", channel, now_iso()))
        for oid, (stall, d, window, priced, total) in zip(order_ids, prepared):
            try:
                service.hold_order(c, oid, stall, d, window, priced, total, buyer_name=buyer_name, note=note,
                                   channel=channel, basket_id=basket_id)
            except RuleViolation as e:
                raise _for_stall(e, stall["id"])

    mode = "single" if settings.market_checkout != "split" and len(prepared) > 1 else "split"
    if mode == "single":
        mode = _attach_single_checkout(basket_id, order_ids, prepared)
    if mode == "split":
        with tx() as c:
            c.execute("UPDATE baskets SET mode='split' WHERE id=?", (basket_id,))
        try:
            for oid, (stall, d, window, priced, total) in zip(order_ids, prepared):
                service.attach_checkout(oid, stall, d, window, priced, total)
        except RuleViolation:
            cancel_basket(basket_id, status="payment_failed")  # release every hold; no money has moved
            raise
    for oid, p in zip(order_ids, prepared):
        log_event("order_created", stall_id=p[0]["id"], order_id=oid, channel=channel, total_cents=p[4], market=basket_id)
    return basket_view(basket_id)


def _for_stall(e: RuleViolation, stall_id: str) -> RuleViolation:
    stall = service.get_stall(stall_id)
    name = stall["name"] if stall else stall_id
    err = RuleViolation(e.code, f"{name}: {e.message}")
    err.stall_id = stall_id  # type: ignore[attr-defined]
    return err


def _attach_single_checkout(basket_id: str, order_ids: list[str], prepared: list) -> str:
    units = [{
        "reference_id": oid,
        "lines": [{"name": l.name, "quantity": l.quantity, "unit_cents": l.unit_cents} for l in priced],
        "total_cents": total,
        "description": f"{stall['name']} pickup {d.isoformat()} {window}",
        "payee_email": stall["paypal_email"] or None,
    } for oid, (stall, d, window, priced, total) in zip(order_ids, prepared)]
    first = order_ids[0]
    try:
        created = paypal.create_multi_order(
            request_key=basket_id, units=units, brand_name="OpenStall market run",
            return_url=f"{settings.base_url}/paypal/return?order={first}",
            cancel_url=f"{settings.base_url}/paypal/cancel?order={first}",
        )
    except PayPalError as e:
        # Multi-seller checkout can be unavailable for an account. One checkout per stall still works.
        log_event("market_single_refused", order_id=first, basket=basket_id, error=str(e), issue=issue_of(e), debug_id=e.debug_id)
        return "split"
    with tx() as c:
        c.execute("UPDATE baskets SET paypal_order_id=?, approve_url=? WHERE id=?", (created.id, created.approve_url, basket_id))
        for oid in order_ids:
            c.execute("UPDATE orders SET paypal_order_id=?, approve_url=?, updated_at=? WHERE id=?",
                      (created.id, created.approve_url, now_iso(), oid))
    return "single"


# ------------------------------------------------------------------ paying
def complete_basket(basket_id: str, focus: str | None = None) -> dict[str, Any]:
    """Buyer approved the single market checkout. Same discipline as one order:
    reserve stock first, capture once, then verify each seller's capture against its own order."""
    b = get_basket(basket_id)
    orders = _orders(basket_id)
    if not b or not b["paypal_order_id"]:
        raise RuleViolation("unknown_order", "Market order not found.")

    sold_out: list[str] = []
    to_capture: list[dict[str, Any]] = []
    with tx() as c:
        for o in orders:
            current = c.execute("SELECT status FROM orders WHERE id=?", (o["id"],)).fetchone()[0]
            if current not in ("pending_payment", "expired", "payment_failed"):
                continue
            stall = service.get_stall(o["stall_id"])
            items_by_id = {it["id"]: it for it in stall["items"]} if stall else {}
            priced = [rules.PricedLine(l["item_id"], l["name"], l["quantity"], l["unit_cents"]) for l in o["lines"]]
            committed = service.reserved_quantities(o["stall_id"], o["pickup_date"], include_pending=False, exclude_order=o["id"])
            try:
                rules.check_capacity(priced, items_by_id, committed)
            except RuleViolation as e:
                c.execute("UPDATE orders SET status='sold_out', updated_at=? WHERE id=?", (now_iso(), o["id"]))
                sold_out.append(o["id"])
                o["_reason"] = e.message
            else:
                c.execute("UPDATE orders SET status='capturing', updated_at=? WHERE id=?", (now_iso(), o["id"]))
                to_capture.append(o)
    for o in orders:
        if o["id"] in sold_out:
            log_event("sold_out_not_captured", stall_id=o["stall_id"], order_id=o["id"], reason=o.get("_reason"))
    if not to_capture:
        return _result(basket_id, focus, "Everything sold out before you paid. You were not charged." if sold_out else None)

    # One capture covers every seller. Units we had to mark sold out are refunded by _finalize_capture.
    try:
        caps = paypal.capture_order_all(b["paypal_order_id"])
    except PayPalError as e:
        return _handle_basket_capture_error(basket_id, b, to_capture, e, focus)
    return _finalize_basket(basket_id, caps, focus)


def _finalize_basket(basket_id: str, caps: dict[str, dict[str, Any]], focus: str | None) -> dict[str, Any]:
    notes = []
    for o in _orders(basket_id):
        cap = caps.get(o["id"])
        if cap and o["status"] in ("capturing", "sold_out", "pending_payment", "expired", "payment_failed"):
            service._finalize_capture(o, cap)
        elif o["status"] == "capturing":
            # PayPal captured other sellers but not this one (HTTP 207 partial capture). Nothing was taken for it.
            service._set_status(o["id"], "payment_failed")
            log_event("market_unit_not_captured", stall_id=o["stall_id"], order_id=o["id"])
            notes.append(o["id"])
    after = _orders(basket_id)
    if any(o["status"] == "refunded" for o in after):
        msg = "Part of your market run sold out before payment and was refunded right away. The rest is paid."
    elif notes:
        msg = "PayPal could not pay every stall. Stalls marked 'Payment failed' were not charged."
    else:
        msg = None
    return _result(basket_id, focus, msg)


def _handle_basket_capture_error(basket_id: str, b: dict[str, Any], orders: list[dict[str, Any]], e: PayPalError,
                                 focus: str | None) -> dict[str, Any]:
    issue = issue_of(e)
    log_event("capture_failed", order_id=orders[0]["id"], basket=basket_id, error=str(e), issue=issue, debug_id=e.debug_id)
    if issue == "ORDER_ALREADY_CAPTURED" or e.status is None or (e.status or 0) >= 500:
        try:  # ambiguous: ask PayPal what actually happened
            caps = captures_by_reference(paypal.get_order(b["paypal_order_id"]))
        except PayPalError:
            caps = {}
        if caps:
            return _finalize_basket(basket_id, caps, focus)
        _set_all(orders, "pending_payment")
        return _result(basket_id, focus, "We're still confirming your payment with PayPal. Refresh in a moment; you won't be charged twice.")
    if issue in ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"):
        _set_all(orders, "pending_payment")
        return _result(basket_id, focus, "This payment hasn't been approved in PayPal yet.")
    if issue in ("INSTRUMENT_DECLINED", "TRANSACTION_REFUSED"):
        _set_all(orders, "pending_payment")
        return _result(basket_id, focus, "Your payment method was declined. Approve again with another funding source.")
    _set_all(orders, "payment_failed")
    return _result(basket_id, focus, "PayPal could not complete the payment. You were not charged.")


def _set_all(orders: list[dict[str, Any]], status: str) -> None:
    for o in orders:
        service._set_status(o["id"], status)


def _result(basket_id: str, focus: str | None, error: str | None) -> dict[str, Any]:
    out = service.order_view(focus) if focus else basket_view(basket_id)
    return {**out, **({"error": error} if error else {})}


def confirm_basket(basket_id: str) -> dict[str, Any]:
    """Safe to poll: captures only once PayPal reports approval."""
    b = get_basket(basket_id)
    if not b:
        raise RuleViolation("unknown_order", "Market order not found.")
    if b["mode"] == "single":
        orders = _orders(basket_id)
        if any(o["status"] in ("pending_payment", "expired", "payment_failed") for o in orders) and b["paypal_order_id"]:
            try:
                pp_status = paypal.get_order(b["paypal_order_id"]).get("status")
            except PayPalError:
                pp_status = None
            if pp_status in ("APPROVED", "COMPLETED"):
                res = complete_basket(basket_id)
                return {**basket_view(basket_id), **({"error": res["error"]} if res.get("error") else {})}
    else:
        for o in _orders(basket_id):
            if o["status"] == "pending_payment":
                service.confirm_if_approved(o["id"])
    return basket_view(basket_id)


def cancel_basket(basket_id: str, status: str = "cancelled") -> None:
    for o in _orders(basket_id):
        if o["status"] == "pending_payment":
            service._set_status(o["id"], status)
            log_event("order_cancelled" if status == "cancelled" else "paypal_create_failed", stall_id=o["stall_id"], order_id=o["id"])


# ------------------------------------------------------------------ viewing
def basket_view(basket_id: str) -> dict[str, Any]:
    b = get_basket(basket_id)
    if not b:
        raise RuleViolation("unknown_order", f"No market order {basket_id!r}.")
    orders = [service.order_view(o["id"]) for o in _orders(basket_id)]
    statuses = {o["status"] for o in orders}
    total = sum(o["total_cents"] for o in orders)
    if statuses <= {"pending_payment"}:
        status, label = "pending_payment", "Waiting for PayPal approval"
    elif statuses <= set(PAID_LIKE):
        status, label = "paid", "Paid. Every stall has your order"
    elif statuses & {"capturing", "payment_review"}:
        status, label = "confirming", "Confirming payment with PayPal"
    elif statuses & set(PAID_LIKE):
        status, label = "partly_paid", "Partly paid. Check each stall below"
    else:
        status, label = "closed", "Not paid"
    view: dict[str, Any] = {
        "market_order_id": basket_id,
        "mode": b["mode"],
        "status": status,
        "status_label": label,
        "pickup_date": b["pickup_date"],
        "total": service.fmt_money(total),
        "total_cents": total,
        "stall_count": len(orders),
        "orders": orders,
    }
    if b["mode"] == "single" and status == "pending_payment" and b["approve_url"]:
        view["paypal_approval_url"] = b["approve_url"]
        view["next_step"] = (
            "Ask the human buyer to open paypal_approval_url and approve ONE PayPal payment that pays every stall. "
            "Held for %d minutes." % settings.hold_minutes
        )
    elif b["mode"] == "split" and any(o["status"] == "pending_payment" for o in orders):
        view["next_step"] = ("Each stall has its own PayPal approval link (orders[].paypal_approval_url). "
                             "The human approves each one.")
    return view
