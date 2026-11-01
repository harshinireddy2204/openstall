"""End-to-end scenarios, including the failure modes we expect in a live demo."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db, rules, service, tools
from app.paypal import PayPalError, paypal
from app.rules import RuleViolation

from .conftest import approve_and_return, item_id, next_weekday

NOOR = "noor-s-cardamom-kitchen"
TAMALES = "merrimack-tamales"
PICKLES = "pawtucket-pickle-co"


def order_payload(stall, item, qty, day="sat"):
    return {"items": [{"item_id": item, "quantity": qty}], "pickup_date": next_weekday(day), "buyer_name": "Test"}


# ------------------------------------------------------------------ happy paths
def test_web_happy_path_pays_and_consumes_capacity(client):
    bun = item_id(NOOR, "Cardamom bun")
    r = client.post(f"/api/stalls/{NOOR}/orders", json=order_payload(NOOR, bun, 12))
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["status"] == "pending_payment" and order["total_cents"] == 12 * 450
    assert order["paypal_approval_url"]
    paid = approve_and_return(client, order)
    assert paid["status"] == "paid"
    avail = client.get(f"/api/stalls/{NOOR}/availability", params={"date": order["pickup_date"]}).json()
    assert next(i for i in avail["items"] if i["item_id"] == bun)["remaining"] == 48 - 12


def test_agent_tool_flow_end_to_end(client):
    found = tools.find_stalls(query="tamales vegan")
    assert found["count"] >= 1 and found["stalls"][0]["stall_id"] == TAMALES
    menu = tools.get_menu(stall_id=TAMALES)
    vegan = next(i for i in menu["items"] if "Vegan" in i["name"])
    sat = next_weekday("sat")
    assert tools.check_availability(stall_id=TAMALES, pickup_date=sat)["open"] is True
    order = tools.place_order(stall_id=TAMALES, items=[{"item_id": vegan["item_id"], "quantity": 2}], pickup_date=sat)
    assert order["channel"] == "agent" and "next_step" in order
    assert tools.get_order_status(order_id=order["order_id"])["status"] == "pending_payment"
    assert approve_and_return(client, order)["status"] == "paid"


def test_mcp_http_endpoint_lists_and_calls_tools(client):
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    r = client.post("/agent/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"find_stalls", "get_menu", "check_availability", "place_order", "get_order_status", "request_custom_order"} <= names
    r = client.post("/agent/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_menu", "arguments": {"stall_id": NOOR}},
    })
    assert r.json()["result"]["structuredContent"]["name"] == "Noor's Cardamom Kitchen"


# ------------------------------------------------------- agent misbehaviour
def test_agent_cannot_set_its_own_price():
    bun = item_id(NOOR, "Cardamom bun")
    order = tools.place_order(
        stall_id=NOOR, pickup_date=next_weekday("sat"),
        items=[{"item_id": bun, "quantity": 10, "price": 0.01, "unit_cents": 1}],
    )
    assert order["total_cents"] == 10 * 450


@pytest.mark.parametrize("lines,code", [
    ([], "empty_order"),
    ([{"item_id": 99999, "quantity": 1}], "unknown_item"),
    ([{"item_id": "abc", "quantity": 1}], "invalid_line"),
    ([{"quantity": 1}], "invalid_line"),
    ([{"item_id": 1, "quantity": 0}], "invalid_quantity"),
    ([{"item_id": 1, "quantity": -5}], "invalid_quantity"),
    ([{"item_id": 1, "quantity": 5000}], "too_many_items"),
])
def test_bad_lines_rejected(lines, code):
    res = tools.place_order(stall_id=NOOR, items=lines, pickup_date=next_weekday("sat"))
    assert res["error"] == code


def test_item_from_another_stall_rejected():
    tamale = item_id(TAMALES, "Pork")
    res = tools.place_order(stall_id=NOOR, items=[{"item_id": tamale, "quantity": 1}], pickup_date=next_weekday("sat"))
    assert res["error"] == "unknown_item"


def test_unknown_stall_and_order():
    assert tools.get_menu(stall_id="nope")["error"] == "unknown_stall"
    assert tools.get_order_status(order_id="OS-NOPE")["error"] == "unknown_order"


def test_model_sends_wrong_argument_names():
    assert tools.call("place_order", {"stall": NOOR})["error"] == "bad_arguments"
    assert tools.call("delete_everything", {})["error"] == "unknown_tool"


# ------------------------------------------------------------- seller rules
def test_over_capacity_rejected_before_any_paypal_call(monkeypatch):
    calls = []
    monkeypatch.setattr(paypal, "create_order", lambda **kw: calls.append(kw))
    box = item_id(NOOR, "box of 6")  # capacity 8
    res = tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 9}], pickup_date=next_weekday("sat"))
    assert res["error"] == "over_capacity" and calls == []


def test_holds_count_toward_capacity():
    box = item_id(NOOR, "box of 6")
    sat = next_weekday("sat")
    assert "order_id" in tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 6}], pickup_date=sat)
    res = tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 3}], pickup_date=sat)
    assert res["error"] == "over_capacity" and "Only 2" in res["message"]


def test_capacity_is_per_day():
    box = item_id(NOOR, "box of 6")
    tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 8}], pickup_date=next_weekday("sat"))
    assert "order_id" in tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 8}], pickup_date=next_weekday("fri"))


def test_no_pickup_on_that_weekday():
    res = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 1}], pickup_date=next_weekday("tue"))
    assert res["error"] == "no_pickup_that_day" and "Tuesday" in res["message"]


def test_lead_time_enforced():
    stall = service.get_stall(TAMALES)
    tz = ZoneInfo("America/New_York")
    sat = date.fromisoformat(next_weekday("sat"))
    friday_evening = datetime.combine(sat - timedelta(days=1), datetime.min.time(), tz).replace(hour=18)
    with pytest.raises(RuleViolation) as e:
        rules.resolve_window(stall, sat, None, now=friday_evening)  # 16h before a 48h-notice stall
    assert e.value.code == "lead_time"


@pytest.mark.parametrize("bad", ["2020-01-01", "next saturday", "2026-13-45", ""])
def test_bad_or_past_dates(bad):
    res = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 1}], pickup_date=bad)
    assert res["error"] in ("invalid_date", "past_date", "no_pickup_that_day", "lead_time")


def test_order_total_cap(monkeypatch):
    monkeypatch.setattr(service.settings.__class__, "max_order_cents", property(lambda self: 1000), raising=False)
    res = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 5}], pickup_date=next_weekday("sat"))
    assert res["error"] == "over_limit"


# ---------------------------------------------------------- hold lifecycle
def _age_order(order_id, minutes):
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with db.tx() as c:
        c.execute("UPDATE orders SET created_at=? WHERE id=?", (old, order_id))


def test_stale_hold_expires_and_frees_capacity():
    box = item_id(NOOR, "box of 6")
    sat = next_weekday("sat")
    first = tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 8}], pickup_date=sat)
    _age_order(first["order_id"], 45)
    second = tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 8}], pickup_date=sat)
    assert "order_id" in second
    assert service.get_order(first["order_id"])["status"] == "expired"


def test_late_approval_after_resale_is_refunded(client):
    """Buyer A's hold expires, buyer B buys the stock, then A approves in PayPal."""
    box = item_id(NOOR, "box of 6")
    sat = next_weekday("sat")
    a = tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 8}], pickup_date=sat)
    _age_order(a["order_id"], 45)
    b = tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 8}], pickup_date=sat)
    assert approve_and_return(client, b)["status"] == "paid"
    captures = []
    real_capture = paypal.capture_order
    paypal.capture_order = lambda oid: captures.append(oid) or real_capture(oid)
    try:
        assert approve_and_return(client, a)["status"] == "sold_out"
    finally:
        paypal.capture_order = real_capture
    assert captures == []  # stock is checked BEFORE money moves: A is never charged


def test_late_approval_with_stock_left_still_succeeds(client):
    bun = item_id(NOOR, "Cardamom bun")
    a = tools.place_order(stall_id=NOOR, items=[{"item_id": bun, "quantity": 4}], pickup_date=next_weekday("sat"))
    _age_order(a["order_id"], 45)
    tools.check_availability(stall_id=NOOR, pickup_date=a["pickup_date"])  # triggers expiry
    assert approve_and_return(client, a)["status"] == "paid"


# ------------------------------------------------------ PayPal return flow
def test_buyer_cancels_in_paypal(client):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 1}], pickup_date=next_weekday("sat"))
    token = o["paypal_approval_url"].split("token=")[1]
    r = client.post("/mock-paypal/approve", data={"token": token, "decision": "cancel"}, follow_redirects=True)
    assert r.status_code == 200 and "cancelled" in r.text.lower()
    assert service.get_order(o["order_id"])["status"] == "cancelled"
    # Even if a stale approval comes through later, a cancelled order is never captured.
    paypal.mock_approve(token)
    assert service.complete_payment(o["order_id"])["status"] == "cancelled"


def test_return_page_refresh_is_idempotent(client, monkeypatch):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 2}], pickup_date=next_weekday("sat"))
    approve_and_return(client, o)
    calls = []
    monkeypatch.setattr(paypal, "capture_order", lambda oid: calls.append(oid))
    r = client.get(f"/paypal/return?order={o['order_id']}", follow_redirects=False)
    assert r.status_code == 303 and calls == []


def test_return_with_mismatched_token_rejected(client):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 1}], pickup_date=next_weekday("sat"))
    r = client.get(f"/paypal/return?order={o['order_id']}&token=SOMEONE-ELSES", follow_redirects=False)
    assert r.status_code == 400


def test_return_before_approval_does_not_mark_paid(client):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 1}], pickup_date=next_weekday("sat"))
    client.get(f"/paypal/return?order={o['order_id']}", follow_redirects=False)
    # Poking the return URL early must not break the order: it stays payable.
    assert service.get_order(o["order_id"])["status"] == "pending_payment"
    assert approve_and_return(client, o)["status"] == "paid"


def test_paypal_outage_on_create_releases_hold(monkeypatch):
    def boom(**kw):
        raise PayPalError("Service Unavailable", 503, "dbg123")
    monkeypatch.setattr(paypal, "create_order", boom)
    box = item_id(NOOR, "box of 6")
    sat = next_weekday("sat")
    res = tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 8}], pickup_date=sat)
    assert res["error"] == "payment_unavailable"
    monkeypatch.undo()
    assert "order_id" in tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 8}], pickup_date=sat)


def test_card_declined_keeps_order_retryable(client, monkeypatch):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 1}], pickup_date=next_weekday("sat"))

    def declined(oid):
        raise PayPalError("Unprocessable", 422, details={"details": [{"issue": "INSTRUMENT_DECLINED"}]})
    monkeypatch.setattr(paypal, "capture_order", declined)
    res = service.complete_payment(o["order_id"])
    assert res["status"] == "pending_payment" and "declined" in res["error"]


def test_already_captured_is_verified_not_trusted(monkeypatch):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 2}], pickup_date=next_weekday("sat"))

    def already(oid):
        raise PayPalError("Unprocessable", 422, details={"details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
    monkeypatch.setattr(paypal, "capture_order", already)
    monkeypatch.setattr(paypal, "get_order", lambda oid: {"purchase_units": [{"payments": {"captures": [
        {"id": "CAP1", "status": "COMPLETED", "amount": {"value": "9.00"}}]}}]})
    assert service.complete_payment(o["order_id"])["status"] == "paid"


def test_amount_mismatch_auto_refunds(monkeypatch):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 2}], pickup_date=next_weekday("sat"))
    monkeypatch.setattr(paypal, "capture_order", lambda oid: {"status": "COMPLETED", "capture_id": "C", "amount_cents": 1})
    refunds = []
    monkeypatch.setattr(paypal, "refund_capture", lambda cid, **kw: refunds.append(cid) or {"status": "COMPLETED"})
    assert service.complete_payment(o["order_id"])["status"] == "refunded" and refunds == ["C"]


# ------------------------------------------------------------------ webhooks
def test_webhook_capture_completed_marks_paid_once(client):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 2}], pickup_date=next_weekday("sat"))
    event = {"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
             "resource": {"id": "CAP9", "status": "COMPLETED", "custom_id": o["order_id"], "amount": {"value": "9.00"}}}
    assert client.post("/webhooks/paypal", json=event).json()["status"] == "marked_paid"
    assert client.post("/webhooks/paypal", json=event).json()["status"] == "duplicate"


def test_webhook_wrong_amount_is_refunded(client):
    o = tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 2}], pickup_date=next_weekday("sat"))
    event = {"id": "WH-2", "event_type": "PAYMENT.CAPTURE.COMPLETED",
             "resource": {"id": "CAP9", "custom_id": o["order_id"], "amount": {"value": "0.01"}}}
    assert client.post("/webhooks/paypal", json=event).json()["status"] == "marked_refunded"


def test_webhook_unknown_order_and_bad_json(client):
    ev = {"id": "WH-3", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"custom_id": "OS-NOPE"}}
    assert client.post("/webhooks/paypal", json=ev).json()["status"] == "ignored_unknown_order"
    assert client.post("/webhooks/paypal", content=b"not json").status_code == 400


def test_webhook_rejected_when_signature_invalid(client, monkeypatch):
    monkeypatch.setattr(paypal, "verify_webhook", lambda h, e: False)
    assert client.post("/webhooks/paypal", json={"id": "x", "event_type": "PAYMENT.CAPTURE.COMPLETED"}).status_code == 401


# --------------------------------------------------------------- seller side
def test_dashboard_requires_owner_key(client):
    assert client.get(f"/s/{NOOR}/dashboard").status_code == 403
    assert client.get(f"/s/{NOOR}/dashboard?key=guess").status_code == 403
    key = db.query_one("SELECT owner_key FROM stalls WHERE id=?", (NOOR,))["owner_key"]
    assert client.get(f"/s/{NOOR}/dashboard?key={key}").status_code == 200


def test_seller_actions_and_transitions(client):
    key = db.query_one("SELECT owner_key FROM stalls WHERE id=?", (PICKLES,))["owner_key"]
    jar = item_id(PICKLES, "dill")
    o = approve_and_return(client, tools.place_order(stall_id=PICKLES, items=[{"item_id": jar, "quantity": 2}], pickup_date=next_weekday("wed")))
    url = f"/api/stalls/{PICKLES}/orders/{o['order_id']}"
    assert client.post(url, json={"key": "wrong", "action": "ready"}).status_code == 403
    assert client.post(url, json={"key": key, "action": "ship"}).json()["error"] == "missing_tracking"
    r = client.post(url, json={"key": key, "action": "ship", "tracking_number": "9400100000000000000000", "carrier": "USPS"})
    assert r.json()["status"] == "shipped"
    assert client.post(url, json={"key": key, "action": "refund"}).json()["error"] == "invalid_transition"


def test_seller_refund(client):
    key = db.query_one("SELECT owner_key FROM stalls WHERE id=?", (NOOR,))["owner_key"]
    o = approve_and_return(client, tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "bun"), "quantity": 1}], pickup_date=next_weekday("sat")))
    r = client.post(f"/api/stalls/{NOOR}/orders/{o['order_id']}", json={"key": key, "action": "refund"})
    assert r.json()["status"] == "refunded"


def test_custom_order_agent_can_only_ask_seller_sets_price(client, monkeypatch):
    sent = []
    real = paypal.create_and_send_invoice
    monkeypatch.setattr(paypal, "create_and_send_invoice", lambda **kw: sent.append(kw) or real(**kw))
    sat = next_weekday("sat")
    assert tools.request_custom_order(stall_id=NOOR, buyer_email="nope", description="Cake for 20", needed_by=sat)["error"] == "invalid_email"
    assert tools.request_custom_order(stall_id=NOOR, buyer_email="b@x.co", description="x", needed_by=sat)["error"] == "missing_description"
    req = tools.request_custom_order(stall_id=NOOR, buyer_email="buyer@example.com", description="Birthday cake for 20",
                                     needed_by=sat, budget_usd=120)
    assert req["status"] == "awaiting_seller_quote" and sent == []  # no invoice until the seller acts
    again = tools.request_custom_order(stall_id=NOOR, buyer_email="buyer@example.com", description="Birthday cake for 20", needed_by=sat)
    assert again["custom_order_id"] == req["custom_order_id"]  # duplicate requests collapse
    key = db.query_one("SELECT owner_key FROM stalls WHERE id=?", (NOOR,))["owner_key"]
    url = f"/api/stalls/{NOOR}/custom/{req['custom_order_id']}/quote"
    assert client.post(url, json={"key": "bad", "amount": 150}).status_code == 403
    r = client.post(url, json={"key": key, "amount": 150, "deposit_percent": 30})
    assert r.status_code == 200 and r.json()["deposit_due"] == "$45.00" and len(sent) == 1
    assert sent[0]["amount_cents"] == 15000 and sent[0]["deposit_cents"] == 4500
    assert client.post(url, json={"key": key, "amount": 150}).json()["error"] == "invalid_transition"
    assert tools.get_custom_order_status(custom_order_id=req["custom_order_id"])["status"] == "sent"


def test_custom_request_flood_is_capped():
    sat = next_weekday("sat")
    for i in range(service.MAX_OPEN_REQUESTS):
        assert "custom_order_id" in tools.request_custom_order(stall_id=NOOR, buyer_email=f"b{i}@x.co", description=f"Tray number {i}", needed_by=sat)
    assert tools.request_custom_order(stall_id=NOOR, buyer_email="z@x.co", description="One more tray", needed_by=sat)["error"] == "too_many_requests"


# ------------------------------------------------- payment edge cases (judge round)
def _new_order(qty=2):
    return tools.place_order(stall_id=NOOR, items=[{"item_id": item_id(NOOR, "Cardamom bun"), "quantity": qty}], pickup_date=next_weekday("sat"))


def _approve(order):
    paypal.mock_approve(order["paypal_approval_url"].split("token=")[1])


def test_capture_timeout_but_paypal_captured_is_reconciled(monkeypatch):
    o = _new_order()
    _approve(o)
    real = paypal.capture_order

    def capture_then_timeout(oid):
        real(oid)  # PayPal actually captures...
        raise PayPalError("Network error talking to PayPal: ReadTimeout", None)  # ...but we never see the reply
    monkeypatch.setattr(paypal, "capture_order", capture_then_timeout)
    assert service.complete_payment(o["order_id"])["status"] == "paid"


def test_capture_timeout_without_capture_stays_retryable(monkeypatch):
    o = _new_order()
    _approve(o)

    def timeout(oid):
        raise PayPalError("Network error talking to PayPal: ConnectTimeout", None)
    monkeypatch.setattr(paypal, "capture_order", timeout)
    res = service.complete_payment(o["order_id"])
    assert res["status"] == "pending_payment" and "confirming" in res["error"]
    monkeypatch.undo()
    assert service.complete_payment(o["order_id"])["status"] == "paid"


def test_pending_capture_is_not_treated_as_paid(client, monkeypatch):
    o = _new_order()
    _approve(o)
    monkeypatch.setattr(paypal, "capture_order", lambda oid: {"status": "PENDING", "capture_id": "CAPP", "amount_cents": 900, "currency": "USD"})
    assert service.complete_payment(o["order_id"])["status"] == "payment_review"
    # Stock stays reserved while PayPal reviews
    avail = tools.check_availability(stall_id=NOOR, pickup_date=o["pickup_date"])
    assert next(i for i in avail["items"] if "Cardamom bun" == i["name"])["remaining"] == 46
    ev = {"id": "WH-P", "event_type": "PAYMENT.CAPTURE.COMPLETED",
          "resource": {"id": "CAPP", "custom_id": o["order_id"], "amount": {"value": "9.00", "currency_code": "USD"}}}
    assert client.post("/webhooks/paypal", json=ev).json()["status"] == "marked_paid"


def test_failed_refund_escalates_instead_of_crashing(monkeypatch):
    o = _new_order()
    _approve(o)
    monkeypatch.setattr(paypal, "capture_order", lambda oid: {"status": "COMPLETED", "capture_id": "C1", "amount_cents": 1, "currency": "USD"})

    def refund_down(cid, **kw):
        raise PayPalError("Service Unavailable", 503)
    monkeypatch.setattr(paypal, "refund_capture", refund_down)
    assert service.complete_payment(o["order_id"])["status"] == "needs_attention"
    assert db.query_one("SELECT 1 FROM events WHERE kind='refund_failed_needs_attention'")


def test_currency_mismatch_is_refunded(monkeypatch):
    o = _new_order()
    _approve(o)
    monkeypatch.setattr(paypal, "capture_order", lambda oid: {"status": "COMPLETED", "capture_id": "C2", "amount_cents": 900, "currency": "EUR"})
    assert service.complete_payment(o["order_id"])["status"] == "refunded"


def test_refund_webhook_matches_via_capture_link_and_handles_partial(client):
    o = approve_and_return(client, _new_order(qty=4))  # $18.00
    cap = service.get_order(o["order_id"])["paypal_capture_id"]
    link = [{"rel": "up", "href": f"https://api-m.sandbox.paypal.com/v2/payments/captures/{cap}"}]
    partial = {"id": "WH-R1", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {"id": "REF1", "amount": {"value": "4.50"}, "links": link}}
    assert client.post("/webhooks/paypal", json=partial).json()["status"] == "partial_refund_logged"
    assert service.get_order(o["order_id"])["status"] == "paid"
    full = {"id": "WH-R2", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {"id": "REF2", "amount": {"value": "18.00"}, "links": link}}
    assert client.post("/webhooks/paypal", json=full).json()["status"] == "marked_refunded"


def test_approved_webhook_captures_when_buyer_closed_tab(client):
    o = _new_order()
    _approve(o)
    ev = {"id": "WH-A", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": o["paypal_approval_url"].split("token=")[1]}}
    assert client.post("/webhooks/paypal", json=ev).json()["status"] == "order_paid"


def test_dispute_webhook_is_logged_for_seller(client):
    o = approve_and_return(client, _new_order())
    cap = service.get_order(o["order_id"])["paypal_capture_id"]
    ev = {"id": "WH-D", "event_type": "CUSTOMER.DISPUTE.CREATED",
          "resource": {"dispute_id": "PP-D-1", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "disputed_transactions": [{"seller_transaction_id": cap}]}}
    assert client.post("/webhooks/paypal", json=ev).json()["status"] == "dispute_logged"
    assert db.query_one("SELECT order_id FROM events WHERE kind='dispute_created'")["order_id"] == o["order_id"]


def test_late_capture_webhook_for_sold_out_order_is_refunded(client):
    o = _new_order()
    service._set_status(o["order_id"], "sold_out")
    ev = {"id": "WH-S", "event_type": "PAYMENT.CAPTURE.COMPLETED",
          "resource": {"id": "CAPX", "custom_id": o["order_id"], "amount": {"value": "9.00", "currency_code": "USD"}}}
    assert client.post("/webhooks/paypal", json=ev).json()["status"] == "marked_refunded"


def test_hold_squatting_is_capped():
    sat = next_weekday("sat")
    bun = item_id(NOOR, "Cardamom bun")
    for _ in range(service.MAX_PENDING_PER_DAY):
        assert "order_id" in tools.place_order(stall_id=NOOR, items=[{"item_id": bun, "quantity": 1}], pickup_date=sat)
    assert tools.place_order(stall_id=NOOR, items=[{"item_id": bun, "quantity": 1}], pickup_date=sat)["error"] == "too_many_unpaid_orders"


def test_seller_paypal_account_problem_has_clear_message(monkeypatch):
    def payee_bad(**kw):
        raise PayPalError("Unprocessable", 422, details={"details": [{"issue": "PAYEE_ACCOUNT_INVALID"}]})
    monkeypatch.setattr(paypal, "create_order", payee_bad)
    assert _new_order()["error"] == "seller_cannot_receive"


def test_rate_limit_returns_429(client):
    from app.main import RATE_LIMITS
    limit = next(l for p, m, l, w in RATE_LIMITS if p == "/api/agent/chat")
    codes = [client.post("/api/agent/chat", json={"message": "hi"}).status_code for _ in range(limit + 1)]
    assert codes[-1] == 429 and codes[0] == 200


def test_security_headers(client):
    r = client.get(f"/s/{NOOR}")
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("window", [
    {"day": "sat", "start": "15:00", "end": "11:00"},
    {"day": "sat", "start": "25:00", "end": "26:00"},
    {"day": "someday", "start": "09:00", "end": "10:00"},
])
def test_bad_pickup_windows_rejected(client, window):
    r = client.post("/api/stalls", json={"name": "X", "pickup_windows": [window], "items": [{"name": "A", "price": 1}]})
    assert r.status_code == 422 and r.json()["error"] == "invalid_window"


def test_today_uses_seller_timezone():
    # Late evening in Boston is already "tomorrow" in UTC; the seller's date must win.
    assert service.today_local("America/New_York") == datetime.now(ZoneInfo("America/New_York")).date()


def test_money_math_is_exact():
    from app.paypal import money, to_cents
    assert money(1999)["value"] == "19.99" and to_cents("19.99") == 1999 and to_cents("0.29") == 29
    assert service.to_cents("$1,234.56") == 123456 and service.to_cents("abc") is None


# ------------------------------------------------------ onboarding / catalog
def test_extract_from_text_fallback(client):
    r = client.post("/api/catalog/extract", data={"text": "Samosa - $3 each\nMango lassi: $5\nChai 4.00\nPickup Saturdays"})
    data = r.json()
    names = [i["name"] for i in data["items"]]
    assert r.status_code == 200 and names == ["Samosa", "Mango lassi", "Chai"]
    assert data["items"][0]["price"] == 3.0


def test_extract_rejects_empty_and_bad_files(client):
    assert client.post("/api/catalog/extract", data={"text": ""}).status_code == 400
    r = client.post("/api/catalog/extract", files={"photo": ("menu.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 400 and "Unsupported" in r.json()["message"]
    big = b"\x89PNG" + b"0" * (5 * 1024 * 1024 + 10)
    assert client.post("/api/catalog/extract", files={"photo": ("m.png", big, "image/png")}).status_code == 400


def test_photo_without_ai_key_explains_itself(client):
    r = client.post("/api/catalog/extract", files={"photo": ("m.png", b"\x89PNG small", "image/png")})
    assert r.status_code == 200 and "ANTHROPIC_API_KEY" in r.json()["questions_for_seller"][0]


def test_create_stall_validation(client):
    base = {"name": "Sita's Samosa Cart", "pickup_windows": [{"day": "sat", "start": "11:00", "end": "15:00"}],
            "items": [{"name": "Aloo samosa", "price": 3, "daily_capacity": 60}]}
    assert client.post("/api/stalls", json={**base, "items": []}).status_code == 422
    assert client.post("/api/stalls", json={**base, "items": [{"name": "Free", "price": 0}]}).status_code == 422
    assert client.post("/api/stalls", json={**base, "pickup_windows": []}).status_code == 422
    assert client.post("/api/stalls", json={**base, "pickup_windows": [{"day": "sat", "start": "11am", "end": "3pm"}]}).status_code == 422
    r = client.post("/api/stalls", json=base)
    assert r.status_code == 200 and r.json()["stall_id"] == "sita-s-samosa-cart"
    r2 = client.post("/api/stalls", json=base)  # duplicate names get unique slugs
    assert r2.json()["stall_id"] == "sita-s-samosa-cart-2"
    assert tools.find_stalls(query="samosa")["count"] == 2


def test_html_is_escaped(client):
    r = client.post("/api/stalls", json={"name": "<script>alert(1)</script>", "pickup_windows": [{"day": "any", "start": "09:00", "end": "17:00"}],
                                         "items": [{"name": "<img src=x onerror=alert(1)>", "price": 2}]})
    sid = r.json()["stall_id"]
    page = client.get(f"/s/{sid}").text
    assert "<script>alert(1)</script>" not in page.split("application/ld+json")[0]
    assert "<img src=x" not in page


def test_pages_render(client):
    for path in ["/", "/sell", "/shop", f"/s/{NOOR}", "/llms.txt", "/healthz"]:
        assert client.get(path).status_code == 200, path
    assert client.get("/s/does-not-exist").status_code == 404
    assert client.get("/order/OS-NOPE").status_code == 404


def test_buyer_agent_without_key_degrades_gracefully(client):
    r = client.post("/api/agent/chat", json={"message": "buy tamales"})
    assert r.status_code == 200 and "ANTHROPIC_API_KEY" in r.json()["reply"]


def test_concurrent_orders_never_oversell():
    import threading
    box = item_id(NOOR, "box of 6")  # capacity 8
    sat = next_weekday("sat")
    results = []

    def buy():
        results.append(tools.place_order(stall_id=NOOR, items=[{"item_id": box, "quantity": 3}], pickup_date=sat))

    threads = [threading.Thread(target=buy) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ok = [r for r in results if "order_id" in r]
    assert len(ok) == 2  # 2 x 3 = 6 <= 8, a third would make 9


def test_stuck_capturing_order_is_reconciled():
    o = _new_order()
    _approve(o)
    paypal.capture_order(o["paypal_approval_url"].split("token=")[1])  # captured at PayPal, then we "crashed"
    service._set_status(o["order_id"], "capturing")
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    with db.tx() as c:
        c.execute("UPDATE orders SET updated_at=? WHERE id=?", (old, o["order_id"]))
    assert service.reconcile_stuck_captures() == 1
    assert service.get_order(o["order_id"])["status"] == "paid"


def test_claude_forced_tool_choice_fallback(monkeypatch):
    """Some models reject forced tool_choice: we retry with auto and still get a catalog."""
    import types
    import anthropic
    from app import ai

    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw.get("tool_choice", {"type": "default"})["type"])
            if "tool_choice" in kw:
                resp = types.SimpleNamespace(status_code=400, headers={}, request=None)
                raise anthropic.BadRequestError("tool_choice: type \"tool\" and \"any\" are not supported for this model.",
                                                response=_fake_response(), body=None)
            block = types.SimpleNamespace(type="tool_use", input={"items": [{"name": "Aloo samosa", "price": 3, "unit": "each"}],
                                                                  "questions_for_seller": []})
            return types.SimpleNamespace(content=[block])

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: types.SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(ai.settings.__class__, "ai_live", property(lambda self: True))
    out = ai.extract_catalog(text="Aloo samosa $3")
    assert calls == ["tool", "default"] and out["source"] == "claude" and out["items"][0]["name"] == "Aloo samosa"


def test_claude_text_json_answer_is_accepted(monkeypatch):
    import types
    import anthropic
    from app import ai

    class FakeMessages:
        def create(self, **kw):
            return types.SimpleNamespace(content=[types.SimpleNamespace(
                type="text", text='Here you go: {"items": [{"name": "Chai", "price": 4, "unit": "cup"}], "questions_for_seller": []}')])

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: types.SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(ai.settings.__class__, "ai_live", property(lambda self: True))
    assert ai.extract_catalog(text="Chai $4")["items"][0]["price"] == 4.0


def _fake_response():
    import httpx
    return httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_agent_reply_never_contains_invented_links():
    from app.buyer_agent import scrub_urls
    out = scrub_urls("Done! Capture at https://api-m.sandbox.paypal.com/v2/checkout/orders/4C9/capture or www.evil.example")
    assert "http" not in out and "www." not in out and "PayPal button" in out


def test_only_paypal_checkout_pages_are_valid_approval_links():
    from app.paypal import is_paypal_checkout_url
    assert is_paypal_checkout_url("https://www.sandbox.paypal.com/checkoutnow?token=ABC")
    assert is_paypal_checkout_url("https://www.paypal.com/checkoutnow?token=ABC")
    assert not is_paypal_checkout_url("https://api-m.sandbox.paypal.com/v2/checkout/orders/ABC/capture")
    assert not is_paypal_checkout_url("https://www.sandbox.paypal.com/v2/checkout/orders/ABC/capture")
    assert not is_paypal_checkout_url("http://www.paypal.com/checkoutnow")
    assert not is_paypal_checkout_url("https://paypal.com.evil.example/checkoutnow")


def test_confirm_endpoint_only_captures_after_approval(client):
    o = _new_order()
    r = client.post(f"/api/orders/{o['order_id']}/confirm")
    assert r.json()["status"] == "pending_payment"  # not approved yet: no capture attempted
    assert not db.query_one("SELECT 1 FROM events WHERE kind='capture_failed'")
    _approve(o)
    assert client.post(f"/api/orders/{o['order_id']}/confirm").json()["status"] == "paid"
    assert client.post(f"/api/orders/{o['order_id']}/confirm").json()["status"] == "paid"  # idempotent


def _states(view):
    return [(s["key"], s["state"]) for s in view["steps"]]


def test_buyer_progress_tracker_follows_seller_actions(client):
    o = _new_order()
    assert _states(o) == [("placed", "done"), ("paid", "current"), ("ready", "todo"), ("picked_up", "todo")]
    paid = approve_and_return(client, o)
    assert _states(paid)[1:3] == [("paid", "done"), ("ready", "current")]
    key = db.query_one("SELECT owner_key FROM stalls WHERE id=?", (NOOR,))["owner_key"]
    url = f"/api/stalls/{NOOR}/orders/{o['order_id']}"
    client.post(url, json={"key": key, "action": "ready"})
    view = client.get(f"/api/orders/{o['order_id']}").json()
    assert _states(view)[2:] == [("ready", "done"), ("picked_up", "current")] and view["steps"][2]["at"]
    client.post(url, json={"key": key, "action": "picked_up"})
    assert all(st == "done" for _, st in _states(client.get(f"/api/orders/{o['order_id']}").json()))


def test_buyer_tracker_shows_refund_and_cancel():
    o = _new_order()
    _approve(o)
    service.complete_payment(o["order_id"])
    refunded = service.seller_update_order(NOOR, o["order_id"], "refund")
    assert _states(refunded) == [("placed", "done"), ("paid", "done"), ("refunded", "stopped")]
    c = service.cancel_order(_new_order()["order_id"])
    assert _states(c) == [("placed", "done"), ("cancelled", "stopped")]


def test_my_orders_page_renders(client):
    assert client.get("/orders").status_code == 200
