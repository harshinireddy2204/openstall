"""Market runs: one basket across stalls, one PayPal approval, each seller paid on its own purchase unit."""

import pytest

from app import db, market, service, tools
from app.paypal import PayPalError, paypal

from .conftest import item_id, next_weekday

NOOR, TAMALES, PICKLE = "noor-s-cardamom-kitchen", "merrimack-tamales", "pawtucket-pickle-co"


def common_day():
    """A weekday at least two demo stalls are open."""
    for day in ("sat", "fri", "sun", "thu", "wed", "tue", "mon"):
        d = next_weekday(day)
        open_ = [s for s in (NOOR, TAMALES, PICKLE) if service.check_availability(s, d)["open"]]
        if len(open_) >= 2:
            return d, open_
    pytest.skip("demo stalls share no pickup day")


def first_item(stall_id):
    return db.query_one("SELECT id, price_cents, daily_capacity FROM items WHERE stall_id=? ORDER BY id", (stall_id,))


def basket_for(stalls, qty=1):
    return [{"stall_id": s, "items": [{"item_id": first_item(s)["id"], "quantity": qty}]} for s in stalls]


def approve(client, basket):
    token = basket["paypal_approval_url"].split("token=")[1]
    r = client.post("/mock-paypal/approve", data={"token": token, "decision": "approve"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get(r.headers["location"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/market/{basket['market_order_id']}")
    return client.get(f"/api/market/{basket['market_order_id']}").json()


def test_one_approval_pays_every_stall(client):
    day, stalls = common_day()
    b = client.post("/api/market", json={"pickup_date": day, "stalls": basket_for(stalls, 2)}).json()
    assert b["mode"] == "single" and b["status"] == "pending_payment" and b["stall_count"] == len(stalls)
    # Every stall's order shares the one PayPal order, and each is its own purchase unit.
    assert len({o["order_id"] for o in b["orders"]}) == len(stalls)
    mock = paypal._mock_orders[b["paypal_approval_url"].split("token=")[1]]
    assert [u["reference_id"] for u in mock["units"]] == [o["order_id"] for o in b["orders"]]
    assert sum(u["total_cents"] for u in mock["units"]) == b["total_cents"]

    after = approve(client, b)
    assert after["status"] == "paid"
    caps = {o["order_id"]: db.query_one("SELECT paypal_capture_id FROM orders WHERE id=?", (o["order_id"],))["paypal_capture_id"]
            for o in after["orders"]}
    assert all(caps.values()) and len(set(caps.values())) == len(caps)  # one capture per seller
    # Each seller sees only its own order on its dashboard.
    for s in stalls:
        assert [o["order_id"] for o in service.list_orders(s)] == [o["order_id"] for o in after["orders"] if o["stall_id"] == s]


def test_refresh_after_payment_is_idempotent(client):
    day, stalls = common_day()
    b = market.place_basket(basket_for(stalls), day)
    approve(client, b)
    again = market.confirm_basket(b["market_order_id"])
    assert again["status"] == "paid"
    paid_events = db.query("SELECT 1 FROM events WHERE kind='order_paid' AND order_id IN (%s)" % ",".join("?" * len(b["orders"])),
                           tuple(o["order_id"] for o in b["orders"]))
    assert len(paid_events) == len(stalls)


def test_all_or_nothing_when_one_stall_cannot_fill(client):
    day, stalls = common_day()
    parts = basket_for(stalls)
    parts[-1]["items"][0]["quantity"] = 10_000
    before = db.query_one("SELECT COUNT(*) AS n FROM orders WHERE pickup_date=?", (day,))["n"]
    r = client.post("/api/market", json={"pickup_date": day, "stalls": parts})
    assert r.status_code == 422
    name = service.get_stall(stalls[-1])["name"]
    assert r.json()["message"].startswith(name)
    assert db.query_one("SELECT COUNT(*) AS n FROM orders WHERE pickup_date=?", (day,))["n"] == before  # nothing held


def test_closed_stall_rejected_with_its_name_and_logged_as_demand():
    day = next_weekday("fri")  # Merrimack Tamales picks up Sat/Sun only
    r = tools.place_market_order(day, basket_for([NOOR, TAMALES]))
    assert r["error"] == "no_pickup_that_day" and r["message"].startswith("Merrimack Tamales")
    demand = db.query("SELECT detail FROM events WHERE kind='demand_unmet' AND stall_id=? AND detail NOT LIKE '%sample%'", (TAMALES,))
    assert demand


def test_sold_out_unit_is_refunded_rest_paid(client):
    day, stalls = common_day()
    item = first_item(stalls[0])
    if item["daily_capacity"] is None:
        pytest.skip("no capacity on that item")
    b = market.place_basket(basket_for(stalls), day)
    # While our buyer is in PayPal, stall 0 sells out (the seller cuts the day's batch to zero).
    with db.tx() as c:
        c.execute("UPDATE items SET daily_capacity=0 WHERE id=?", (item["id"],))
    after = approve(client, b)
    by_stall = {o["stall_id"]: o["status"] for o in after["orders"]}
    assert by_stall[stalls[0]] == "refunded"
    assert all(by_stall[s] == "paid" for s in stalls[1:])
    assert after["status"] == "partly_paid"


def test_partial_capture_marks_only_missing_unit_failed(client, monkeypatch):
    day, stalls = common_day()
    b = market.place_basket(basket_for(stalls), day)
    token = b["paypal_approval_url"].split("token=")[1]
    paypal.mock_approve(token)
    real = paypal.capture_order_all

    def partial(pp_id):  # PayPal HTTP 207: one seller's unit was not captured
        caps = real(pp_id)
        caps.pop(b["orders"][-1]["order_id"])
        return caps
    monkeypatch.setattr(paypal, "capture_order_all", partial)
    after = market.confirm_basket(b["market_order_id"])
    statuses = [o["status"] for o in after["orders"]]
    assert statuses[-1] == "payment_failed" and all(s == "paid" for s in statuses[:-1])


def test_ambiguous_capture_timeout_reconciles(client, monkeypatch):
    day, stalls = common_day()
    b = market.place_basket(basket_for(stalls), day)
    token = b["paypal_approval_url"].split("token=")[1]
    paypal.mock_approve(token)
    real = paypal.capture_order_all

    def timeout(pp_id):  # PayPal captured, but our connection dropped
        real(pp_id)
        raise PayPalError("Network error talking to PayPal: ReadTimeout", None)
    monkeypatch.setattr(paypal, "capture_order_all", timeout)
    after = market.confirm_basket(b["market_order_id"])
    assert after["status"] == "paid"


def test_declined_keeps_basket_retryable(client, monkeypatch):
    day, stalls = common_day()
    b = market.place_basket(basket_for(stalls), day)
    paypal.mock_approve(b["paypal_approval_url"].split("token=")[1])
    monkeypatch.setattr(paypal, "capture_order_all", lambda pp: (_ for _ in ()).throw(
        PayPalError("declined", 422, details={"details": [{"issue": "INSTRUMENT_DECLINED"}]})))
    after = market.confirm_basket(b["market_order_id"])
    assert after["status"] == "pending_payment" and "declined" in after["error"]


def test_cancel_in_paypal_cancels_every_stall(client):
    day, stalls = common_day()
    b = market.place_basket(basket_for(stalls), day)
    token = b["paypal_approval_url"].split("token=")[1]
    r = client.post("/mock-paypal/approve", data={"token": token, "decision": "cancel"}, follow_redirects=False)
    r = client.get(r.headers["location"], follow_redirects=False)
    assert r.headers["location"].startswith(f"/market/{b['market_order_id']}")
    assert {o["status"] for o in market.basket_view(b["market_order_id"])["orders"]} == {"cancelled"}


def test_falls_back_to_one_checkout_per_stall_when_paypal_refuses(client, monkeypatch):
    day, stalls = common_day()
    monkeypatch.setattr(paypal, "create_multi_order", lambda **kw: (_ for _ in ()).throw(
        PayPalError("not allowed", 422, details={"details": [{"issue": "PERMISSION_DENIED"}]})))
    b = market.place_basket(basket_for(stalls), day)
    assert b["mode"] == "split" and "paypal_approval_url" not in b
    urls = [o["paypal_approval_url"] for o in b["orders"]]
    assert len(set(urls)) == len(stalls)
    # Each approval completes just its own stall.
    first = b["orders"][0]
    paypal.mock_approve(urls[0].split("token=")[1])
    service.confirm_if_approved(first["order_id"])
    view = market.basket_view(b["market_order_id"])
    assert view["orders"][0]["status"] == "paid" and view["orders"][1]["status"] == "pending_payment"


def test_webhook_capture_for_one_unit_maps_to_that_stall(client):
    day, stalls = common_day()
    b = market.place_basket(basket_for(stalls), day)
    target = b["orders"][1]
    event = {"id": "WH-MK-1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
             "resource": {"id": "CAP-X", "custom_id": target["order_id"], "status": "COMPLETED",
                          "amount": {"value": f"{target['total_cents'] / 100:.2f}", "currency_code": "USD"}}}
    assert service.handle_webhook(event) == "marked_paid"
    v = market.basket_view(b["market_order_id"])
    assert v["orders"][1]["status"] == "paid" and v["orders"][0]["status"] == "pending_payment"


def test_too_many_stalls_and_total_cap(client, monkeypatch):
    day, stalls = common_day()
    r = client.post("/api/market", json={"pickup_date": day, "stalls": basket_for(stalls) * 3})
    assert r.status_code == 422  # pydantic: more than 5 stall entries
    from dataclasses import replace
    monkeypatch.setattr(market, "settings", replace(market.settings, max_order_cents=100))
    r2 = tools.place_market_order(day, basket_for(stalls, 1))
    assert r2["error"] in ("over_limit",)


def test_agent_tools_and_mcp_listing(client):
    day, stalls = common_day()
    b = tools.place_market_order(day, basket_for(stalls), buyer_name="Ana")
    assert b["paypal_approval_url"] and b["orders"][0]["channel"] == "agent"
    assert tools.get_market_order_status(b["market_order_id"])["status"] == "pending_payment"
    assert "place_market_order" in client.get("/llms.txt").text
    assert client.get("/market").status_code == 200
    assert client.get(f"/market/{b['market_order_id']}").status_code == 200
    page = client.get(f"/mock-paypal/approve?token={b['paypal_approval_url'].split('token=')[1]}")
    assert b["total"] in page.text


def test_seller_refunds_only_their_part_of_a_market_run(client):
    day, stalls = common_day()
    b = client.post("/api/market", json={"pickup_date": day, "stalls": basket_for(stalls, 1)}).json()
    after = approve(client, b)
    first, rest = after["orders"][0], after["orders"][1:]
    key = db.query_one("SELECT owner_key FROM stalls WHERE id=?", (first["stall_id"],))["owner_key"]
    r = client.post(f"/api/stalls/{first['stall_id']}/orders/{first['order_id']}", json={"key": key, "action": "refund"})
    assert r.status_code == 200 and r.json()["status"] == "refunded"
    view = client.get(f"/api/market/{b['market_order_id']}").json()
    by_id = {o["order_id"]: o["status"] for o in view["orders"]}
    assert by_id[first["order_id"]] == "refunded"
    assert all(by_id[o["order_id"]] == "paid" for o in rest)
    # Another stall's owner key can't touch this stall's order.
    other_key = db.query_one("SELECT owner_key FROM stalls WHERE id=?", (rest[0]["stall_id"],))["owner_key"]
    r = client.post(f"/api/stalls/{first['stall_id']}/orders/{rest[0]['order_id']}", json={"key": other_key, "action": "refund"})
    assert r.status_code == 403


def test_same_basket_sent_twice_creates_one_market_run(client):
    day, stalls = common_day()
    body = {"pickup_date": day, "stalls": basket_for(stalls, 1), "client_ref": "basket-123"}
    a = client.post("/api/market", json=body).json()
    b = client.post("/api/market", json=body).json()
    assert a["market_order_id"] == b["market_order_id"]
    assert db.query_one("SELECT COUNT(*) AS n FROM baskets")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM orders WHERE basket_id IS NOT NULL")["n"] == len(stalls)
    # After paying, a resubmit shows the paid run; it never charges again.
    approve(client, a)
    again = client.post("/api/market", json=body).json()
    assert again["market_order_id"] == a["market_order_id"] and again["status"] == "paid"
    assert "paypal_approval_url" not in again


def test_dashboard_lists_other_stalls_for_the_same_key(client, monkeypatch):
    shared = "shared-demo-key"
    with db.tx() as c:
        c.execute("UPDATE stalls SET owner_key=?", (shared,))
    page = client.get(f"/s/{NOOR}/dashboard?key={shared}").text
    assert f"/s/{TAMALES}/dashboard?key={shared}" in page and f"/s/{PICKLE}/dashboard?key={shared}" in page
    # A seller with their own key sees no other stalls.
    with db.tx() as c:
        c.execute("UPDATE stalls SET owner_key='only-noor' WHERE id=?", (NOOR,))
    assert "Your other stalls" not in client.get(f"/s/{NOOR}/dashboard?key=only-noor").text
