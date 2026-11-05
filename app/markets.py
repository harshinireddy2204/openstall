"""Markets: a farmers market (or any group of stalls) that goes agent-ready together.

An organizer groups stalls under one page, one QR code and one MCP listing. Shoppers and
their AI agents fill one basket across the market and approve one PayPal payment that pays
each seller directly (see market.py). The organizer sees market-wide insights but can never
touch a seller's money: refunds and fulfilment stay with each stall.
"""

from __future__ import annotations

import secrets
from typing import Any

from . import insights, service
from .db import now_iso, query, query_one, tx
from .rules import RuleViolation

MAX_STALLS_PER_MARKET = 40


def create_market(name: str, city: str = "", schedule: str = "", description: str = "",
                  stall_ids: list[str] | None = None, organizer_key: str | None = None,
                  market_id: str | None = None) -> dict[str, Any]:
    name = (name or "").strip()[:80]
    if len(name) < 3:
        raise RuleViolation("invalid_market", "Give the market a name (at least 3 characters).")
    base = market_id or service.slugify(name)
    mid, n = base, 2
    while query_one("SELECT 1 FROM markets WHERE id=?", (mid,)):
        mid = f"{base}-{n}"; n += 1
    key = organizer_key or secrets.token_urlsafe(16)
    with tx() as c:
        c.execute("INSERT INTO markets (id,name,city,schedule,description,organizer_key,created_at) VALUES (?,?,?,?,?,?,?)",
                  (mid, name, city.strip()[:80], schedule.strip()[:80], description.strip()[:400], key, now_iso()))
    for sid in stall_ids or []:
        add_stall(mid, sid)
    return {**get_market(mid), "organizer_key": key}


def add_stall(market_id: str, stall_id: str) -> None:
    if not query_one("SELECT 1 FROM stalls WHERE id=?", (stall_id,)):
        raise RuleViolation("unknown_stall", f"No stall with id {stall_id!r}.")
    n = query_one("SELECT COUNT(*) AS n FROM market_stalls WHERE market_id=?", (market_id,))["n"]
    if n >= MAX_STALLS_PER_MARKET:
        raise RuleViolation("market_full", f"A market can list up to {MAX_STALLS_PER_MARKET} stalls.")
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO market_stalls (market_id, stall_id, joined_at) VALUES (?,?,?)", (market_id, stall_id, now_iso()))


def remove_stall(market_id: str, stall_id: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM market_stalls WHERE market_id=? AND stall_id=?", (market_id, stall_id))


def check_organizer(market_id: str, key: str) -> dict[str, Any]:
    m = query_one("SELECT * FROM markets WHERE id=?", (market_id,))
    if not m or not key or not secrets.compare_digest(m["organizer_key"], key):
        raise PermissionError("bad organizer key")
    return m


def stall_ids(market_id: str) -> list[str]:
    return [r["stall_id"] for r in query("SELECT stall_id FROM market_stalls WHERE market_id=? ORDER BY joined_at", (market_id,))]


def get_market(market_id: str) -> dict[str, Any]:
    m = query_one("SELECT * FROM markets WHERE id=?", (market_id,))
    if not m:
        raise RuleViolation("unknown_market", f"No market with id {market_id!r}.")
    stalls = [service.stall_summary(s) for sid in stall_ids(market_id) if (s := service.get_stall(sid))]
    return {"market_id": m["id"], "name": m["name"], "city": m["city"], "schedule": m["schedule"],
            "description": m["description"], "stall_count": len(stalls), "stalls": stalls}


def list_markets(query_text: str = "") -> list[dict[str, Any]]:
    words = [w for w in (query_text or "").lower().split() if len(w) > 2]
    out = []
    for m in query("SELECT * FROM markets ORDER BY created_at"):
        hay = f"{m['name']} {m['city']} {m['description']}".lower()
        if words and not any(w in hay for w in words):
            continue
        out.append({"market_id": m["id"], "name": m["name"], "city": m["city"], "schedule": m["schedule"],
                    "stall_count": len(stall_ids(m["id"]))})
    return out


def markets_for_stall(stall_id: str) -> list[dict[str, Any]]:
    return query("SELECT m.id, m.name FROM markets m JOIN market_stalls ms ON ms.market_id=m.id WHERE ms.stall_id=?", (stall_id,))


# ------------------------------------------------------------ organizer data
def market_orders(market_id: str) -> list[dict[str, Any]]:
    out = []
    for sid in stall_ids(market_id):
        out.extend(service.list_orders(sid))
    out.sort(key=lambda o: (o["pickup_date"], o["order_id"]))
    return out


def market_insights(market_id: str) -> dict[str, Any]:
    """The same three data sources as a stall's Insights, across every stall, with a stall column."""
    merged: dict[str, list[dict[str, Any]]] = {"orders": [], "demand": [], "agent_funnel": []}
    demand: list[dict[str, Any]] = []
    names = {}
    for sid in stall_ids(market_id):
        stall = service.get_stall(sid)
        if not stall:
            continue
        names[sid] = stall["name"]
        for src in insights.insights_sources(sid):
            rows = [r for r in src["data"] if any(v not in ("", 0, 0.0) for k, v in r.items() if k != "data")]
            merged[src["id"]].extend({"stall": stall["name"], **r} for r in rows)
        demand.extend({"stall": stall["name"], **g} for g in insights.demand_summary(sid))
    for k, blank in (("orders", {"stall": "", "order_id": "", "ordered_on": "", "pickup_date": "", "pickup_day": "", "item": "",
                                 "quantity": 0, "revenue": 0.0, "agent_revenue": 0.0, "week": "", "channel": "", "status": "", "data": "Live"}),
                     ("demand", {"stall": "", "requested_on": "", "pickup_date": "", "pickup_day": "", "item": "", "quantity": 0,
                                 "reason": "", "data": "Live"}),
                     ("agent_funnel", {"stall": "", "date": "", "stage": "", "agents": 0, "data": "Live"})):
        if not merged[k]:
            merged[k].append(blank)
    demand.sort(key=lambda g: (g["units"], g["requests"]), reverse=True)
    return {"sources": [{"id": k, "data": v} for k, v in merged.items()], "demand": demand, "stalls": names}
