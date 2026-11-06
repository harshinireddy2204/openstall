"""Markets: organizer view, public page, agent tools."""

from app import db, markets, tools

MID = "lowell-saturday-market"
NOOR, TAMALES, PICKLE = "noor-s-cardamom-kitchen", "merrimack-tamales", "pawtucket-pickle-co"


def okey():
    return db.query_one("SELECT organizer_key FROM markets WHERE id=?", (MID,))["organizer_key"]


def test_demo_market_is_seeded_with_demo_stalls(client):
    m = client.get(f"/api/markets/{MID}").json()
    assert m["name"] == "Lowell Saturday Market"
    assert {s["stall_id"] for s in m["stalls"]} == {NOOR, TAMALES, PICKLE}


def test_public_market_page_has_qr_and_one_payment_link(client):
    page = client.get(f"/m/{MID}")
    assert page.status_code == 200 and "<svg" in page.text and f"/market?m={MID}" in page.text
    assert client.get(f"/m/{MID}/qr.svg").headers["content-type"].startswith("image/svg")
    assert client.get("/m/nope").status_code == 404
    builder = client.get(f"/market?m={MID}")
    assert builder.status_code == 200 and "Lowell Saturday Market" in builder.text


def test_organizer_pages_need_the_organizer_key(client):
    for url in (f"/m/{MID}/organizer?key=bad", f"/m/{MID}/insights?key=bad", f"/api/markets/{MID}/orders?key=bad",
                f"/api/markets/{MID}/insights?key=bad"):
        assert client.get(url).status_code == 403
    assert client.post(f"/api/grid/command?market={MID}&key=bad", json={}).status_code == 403
    assert client.post(f"/api/studio/llm?market={MID}&key=bad", json={}).status_code == 403
    assert client.get(f"/m/{MID}/organizer?key={okey()}").status_code == 200
    assert client.get(f"/m/{MID}/insights?key={okey()}").status_code == 200


def test_market_insights_span_every_stall(client):
    body = client.get(f"/api/markets/{MID}/insights?key={okey()}").json()
    src = {s["id"]: s["data"] for s in body["sources"]}
    assert {r["stall"] for r in src["orders"]} == {"Noor's Cardamom Kitchen", "Merrimack Tamales", "Pawtucket Pickle Co."}
    assert body["demand"] and all("stall" in g for g in body["demand"])


def test_create_market_and_manage_stalls(client):
    r = client.post("/api/markets", json={"name": "Dracut Night Market", "city": "Dracut, MA", "stall_ids": [PICKLE]})
    assert r.status_code == 200
    m = r.json()
    assert m["organizer_url"].endswith(m["organizer_key"]) and m["stall_count"] == 1
    r = client.post(f"/api/markets/{m['market_id']}/stalls", json={"key": m["organizer_key"], "stall_id": NOOR, "action": "add"})
    assert r.json()["stall_count"] == 2
    r = client.post(f"/api/markets/{m['market_id']}/stalls", json={"key": okey(), "stall_id": TAMALES, "action": "add"})
    assert r.status_code == 403  # another market's key can't edit this one
    assert client.post("/api/markets", json={"name": "x"}).status_code == 422


def test_agent_can_find_and_open_a_market():
    found = tools.find_markets("lowell")
    assert found["count"] >= 1 and found["markets"][0]["market_id"] == MID
    m = tools.get_market(MID)
    assert m["stall_count"] == 3
    assert tools.get_market("nope")["error"] == "unknown_market"
    assert "find_markets" in tools.BY_NAME and "get_market" in tools.BY_NAME


def test_organizer_sees_orders_but_cannot_refund(client):
    orders = client.get(f"/api/markets/{MID}/orders?key={okey()}").json()["orders"]
    assert isinstance(orders, list)
    # The organizer key is not a seller key for a stall with its own key.
    with db.tx() as c:
        c.execute("UPDATE stalls SET owner_key='seller-only' WHERE id=?", (NOOR,))
        c.execute("UPDATE markets SET organizer_key='organizer-only' WHERE id=?", (MID,))
    assert client.get(f"/s/{NOOR}/dashboard?key=organizer-only").status_code == 403


def test_shared_demo_market_stalls_stay_fixed(client):
    for action in ("remove", "add"):
        r = client.post(f"/api/markets/{MID}/stalls", json={"key": okey(), "stall_id": TAMALES, "action": action})
        assert r.status_code == 422 and r.json()["error"] == "demo_market_locked"
    assert client.get(f"/api/markets/{MID}").json()["stall_count"] == 3
    page = client.get(f"/m/{MID}/organizer?key={okey()}").text
    assert 'data-action="remove"' not in page and "stay fixed" in page


def test_demo_capacity_floor_raises_low_limits_only(monkeypatch):
    from app import main
    rows = db.query("SELECT id, daily_capacity FROM items WHERE stall_id=? AND daily_capacity IS NOT NULL", (NOOR,))
    before = {r["id"]: r["daily_capacity"] for r in rows}
    high = max(before, key=before.get)
    with db.tx() as c:
        c.execute("UPDATE items SET daily_capacity=500 WHERE id=?", (high,))
    monkeypatch.setenv("DEMO_CAPACITY_FLOOR", "150")
    try:
        main.apply_demo_capacity()
        after = {r["id"]: r["daily_capacity"] for r in db.query("SELECT id, daily_capacity FROM items WHERE stall_id=?", (NOOR,)) if r["id"] in before}
        assert after[high] == 500 and all(v == 150 for k, v in after.items() if k != high)
    finally:
        with db.tx() as c:
            for k, v in before.items():
                c.execute("UPDATE items SET daily_capacity=? WHERE id=?", (v, k))


def test_demo_dashboard_says_it_is_shared(client):
    key = db.query_one("SELECT owner_key FROM stalls WHERE id=?", (NOOR,))["owner_key"]
    assert "shared demo stall" in client.get(f"/s/{NOOR}/dashboard?key={key}").text
