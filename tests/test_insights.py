"""Seller Insights: demand signals, data feed, capacity tool, and the Claude bridge for AG Studio."""

import json
from types import SimpleNamespace

from app import db, insights, service, tools

from .conftest import item_id, next_weekday

NOOR = "noor-s-cardamom-kitchen"


def owner_key(stall_id=NOOR):
    return db.query_one("SELECT owner_key FROM stalls WHERE id=?", (stall_id,))["owner_key"]


def test_insights_requires_owner_key(client):
    assert client.get(f"/api/stalls/{NOOR}/insights?key=nope").status_code == 403
    assert client.get(f"/s/{NOOR}/insights?key=nope").status_code == 403
    assert client.post(f"/api/studio/llm?stall={NOOR}&key=nope", json={}).status_code == 403
    assert client.post(f"/api/stalls/{NOOR}/capacity", json={"key": "nope", "item_id": 1, "daily_capacity": 5}).status_code == 403


def test_insights_feed_has_labelled_sample_history(client):
    body = client.get(f"/api/stalls/{NOOR}/insights?key={owner_key()}").json()
    sources = {s["id"]: s["data"] for s in body["sources"]}
    assert set(sources) == {"orders", "demand", "agent_funnel"}
    assert sources["orders"] and all(r["data"] == "Sample history" for r in sources["orders"])
    assert {r["channel"] for r in sources["orders"]} == {"AI agent", "Web"}
    assert all(r["agent_revenue"] in (0, r["revenue"]) for r in sources["orders"])
    assert body["demand"] and "suggestion" in body["demand"][0]
    assert body["rules"]["items"] and body["pickup_board"]
    page = client.get(f"/s/{NOOR}/insights?key={owner_key()}")
    assert page.status_code == 200 and "/static/studio/insights.js" in page.text


def test_sample_history_never_shows_as_seller_orders():
    assert all(not o["order_id"].startswith("OS-S") for o in service.list_orders(NOOR))


def test_live_orders_are_marked_live(client):
    bun = item_id(NOOR, "Cardamom bun")
    r = tools.place_order(NOOR, [{"item_id": bun, "quantity": 2}], next_weekday("sat"))
    assert "order_id" in r
    db.query("SELECT 1")  # noqa
    with db.tx() as c:
        c.execute("UPDATE orders SET status='paid' WHERE id=?", (r["order_id"],))
    rows = insights.insights_sources(NOOR)[0]["data"]
    live = [x for x in rows if x["order_id"] == r["order_id"]]
    assert live and live[0]["data"] == "Live" and live[0]["channel"] == "AI agent"
    funnel = insights.insights_sources(NOOR)[2]["data"]
    assert any(f["stage"] == "3. Started PayPal checkout" and f["data"] == "Live" for f in funnel)


def _live_demand():
    return [d for d in insights.insights_sources(NOOR)[1]["data"] if d["data"] == "Live" and d["reason"]]


def test_agent_sold_out_request_becomes_demand_signal():
    box = item_id(NOOR, "box of 6")  # capacity 8
    day = next_weekday("sat")
    r = tools.place_order(NOOR, [{"item_id": box, "quantity": 9}], day)
    assert r["error"] == "over_capacity"
    d = _live_demand()
    assert d and d[-1]["reason"] == "Sold out" and d[-1]["quantity"] == 9 and d[-1]["item"].startswith("Cardamom buns")


def test_agent_asking_for_closed_day_becomes_demand_signal():
    r = tools.check_availability(NOOR, next_weekday("tue"))
    assert r["open"] is False
    assert any(d["reason"] == "No pickup that day" and d["pickup_day"] == "Tue" for d in _live_demand())


def test_web_orders_do_not_count_as_agent_demand(client):
    box = item_id(NOOR, "box of 6")
    r = client.post(f"/api/stalls/{NOOR}/orders", json={"items": [{"item_id": box, "quantity": 9}], "pickup_date": next_weekday("sat")})
    assert r.status_code == 422
    assert not _live_demand()


def test_set_daily_capacity_via_api(client):
    bun = item_id(NOOR, "Cardamom bun")
    r = client.post(f"/api/stalls/{NOOR}/capacity", json={"key": owner_key(), "item_id": bun, "daily_capacity": 70})
    assert r.status_code == 200 and r.json()["daily_capacity"] == 70
    assert db.query_one("SELECT daily_capacity FROM items WHERE id=?", (bun,))["daily_capacity"] == 70
    # An item from another stall can't be changed with this stall's key.
    other = item_id("merrimack-tamales", "")
    r = client.post(f"/api/stalls/{NOOR}/capacity", json={"key": owner_key(), "item_id": other, "daily_capacity": 1})
    assert r.status_code == 422
    r = client.post(f"/api/stalls/{NOOR}/capacity", json={"key": owner_key(), "item_id": bun, "daily_capacity": -3})
    assert r.status_code == 422


STUDIO_REQUEST = {
    "instructions": "You are the AG Studio lead agent.",
    "responseFormat": {"type": "text"},
    "tools": [
        {"name": "view_schema", "description": "See fields", "parameters": {"type": "object", "properties": {}}, "kind": "client"},
        {"name": "get_unmet_demand", "description": "Missed demand", "parameters": {"type": "object", "properties": {}}},
        {"name": "web", "description": "provider tool", "parameters": {}, "kind": "provided"},
    ],
    "input": [
        {"type": "message", "role": "system", "content": [{"type": "text", "text": "Report context"}]},
        {"type": "message", "role": "user", "content": [{"type": "text", "text": "What should I make more of?"}]},
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "callId": "c1", "name": "get_unmet_demand", "arguments": "{}"},
        {"type": "function_call_output", "callId": "c1", "output": "{\"success\":true}"},
    ],
}


def test_bridge_translates_studio_turn_to_claude():
    body = insights._to_anthropic(STUDIO_REQUEST)
    assert "lead agent" in body["system"] and "Report context" in body["system"]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "get_unmet_demand", "input": {}}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert [t["name"] for t in body["tools"]] == ["view_schema", "get_unmet_demand"]
    js = insights._to_anthropic({**STUDIO_REQUEST, "responseFormat": {"type": "json", "schema": {"type": "object"}}})
    assert "ONLY a JSON object" in js["system"]


class _FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **body):
        self.calls.append(body)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _msg(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_bridge_returns_tool_calls_and_text(monkeypatch):
    import anthropic

    fake = _FakeClient([
        Exception("tool_choice is not supported with this model"),
        _msg(SimpleNamespace(type="text", text="Checking."), SimpleNamespace(type="tool_use", id="tu_1", name="get_unmet_demand", input={})),
    ])
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: fake)
    monkeypatch.setattr(insights, "settings", SimpleNamespace(ai_live=True, anthropic_api_key="k", anthropic_model="m"))
    out = insights.run_studio_turn({**STUDIO_REQUEST, "toolChoice": "required"})
    assert "tool_choice" in fake.calls[0] and "tool_choice" not in fake.calls[1]
    assert out["status"] == "completed"
    assert [o["type"] for o in out["output"]] == ["message", "function_call"]
    assert out["output"][1]["callId"] == "tu_1" and json.loads(out["output"][1]["arguments"]) == {}


def test_bridge_fails_cleanly(monkeypatch):
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: _FakeClient([Exception("overloaded")]))
    monkeypatch.setattr(insights, "settings", SimpleNamespace(ai_live=True, anthropic_api_key="k", anthropic_model="m"))
    out = insights.run_studio_turn(STUDIO_REQUEST)
    assert out["status"] == "failed" and out["output"] == []


def test_bridge_without_api_key_explains(client):
    r = client.post(f"/api/studio/llm?stall={NOOR}&key={owner_key()}", json=STUDIO_REQUEST)
    assert r.status_code == 200 and "ANTHROPIC_API_KEY" in r.json()["output"][0]["content"][0]["text"]


def test_orders_api_and_grid_command_need_owner_key(client):
    assert client.get(f"/api/stalls/{NOOR}/orders?key=nope").status_code == 403
    assert client.post(f"/api/grid/command?stall={NOOR}&key=nope", json={}).status_code == 403
    r = client.get(f"/api/stalls/{NOOR}/orders?key={owner_key()}")
    assert r.status_code == 200 and isinstance(r.json()["orders"], list)
    r = client.post(f"/api/grid/command?stall={NOOR}&key={owner_key()}", json={"request": "only AI agent orders", "schema": {"type": "object"}})
    assert r.status_code == 200 and "ANTHROPIC_API_KEY" in r.json()["message"]


def test_grid_command_returns_validated_state(monkeypatch):
    import anthropic

    reply = _msg(SimpleNamespace(type="tool_use", id="t1", name="apply_grid_state", input={
        "gridState": {"filter": {"filterModel": {"channel": {"filterType": "set", "values": ["AI agent"]}}}},
        "propertiesToIgnore": ["sort", "bogus"], "explanation": "Showing AI agent orders."}))
    fake = _FakeClient([reply])
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: fake)
    monkeypatch.setattr(insights, "settings", SimpleNamespace(ai_live=True, anthropic_api_key="k", anthropic_model="m"))
    out = insights.run_grid_command({"request": "only AI agent orders", "schema": {"type": "object"}, "state": {}})
    assert out["propertiesToIgnore"] == ["sort"] and out["gridState"]["filter"]
    assert fake.calls[0]["tool_choice"]["name"] == "apply_grid_state"
    assert insights.run_grid_command({"request": "", "schema": {}})["error"] == "bad_request"
