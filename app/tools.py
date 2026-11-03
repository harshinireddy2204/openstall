"""Agent-facing tools. One definition, used by the MCP server and the demo buyer agent.

Each tool returns a JSON-serializable dict. Rule violations come back as
{"error": code, "message": ...} so agents can recover instead of crashing.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from . import insights, market, service
from .rules import RuleViolation


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except RuleViolation as e:
            return e.as_dict()
    return wrapper


@_safe
def find_stalls(query: str = "", lat: float | None = None, lng: float | None = None, radius_km: float = 40) -> dict[str, Any]:
    """Search independent local sellers (home bakers, market stalls, makers) listed on OpenStall.

    query: what the shopper wants, e.g. "cardamom buns", "gluten free", "tamales".
    lat/lng: shopper location for distance filtering (optional). Lowell, MA is 42.6334, -71.3162.
    """
    stalls = service.find_stalls(query, lat, lng, radius_km)
    return {"count": len(stalls), "stalls": stalls}


@_safe
def get_menu(stall_id: str) -> dict[str, Any]:
    """Get a stall's full menu with item_ids, prices, allergens, pickup days and lead time."""
    stall = service.get_stall(stall_id)
    if not stall:
        raise RuleViolation("unknown_stall", f"No stall with id {stall_id!r}.")
    insights.record_agent_activity(stall_id, "1. Viewed menu")
    return {
        **service.stall_summary(stall),
        "pickup_address": stall["pickup_address"],
        "pickup_windows": stall["pickup_windows"],
        "items": [service.public_item(i) for i in stall["items"]],
    }


@_safe
def check_availability(stall_id: str, pickup_date: str) -> dict[str, Any]:
    """Check if a stall can fill orders for a pickup date (YYYY-MM-DD): open windows and remaining quantity per item."""
    result = service.check_availability(stall_id, pickup_date)
    insights.record_agent_activity(stall_id, "2. Checked availability")
    if not result["open"]:
        reason = "lead_time" if "notice" in (result.get("reason_closed") or "") else "no_pickup_that_day"
        insights.record_unmet(stall_id, reason, result["pickup_date"])
    else:
        for it in result["items"]:
            if it["remaining"] == 0:
                insights.record_unmet(stall_id, "over_capacity", result["pickup_date"], [{"item_id": it["item_id"], "quantity": 1}])
    return result


@_safe
def place_order(
    stall_id: str,
    items: list[dict[str, Any]],
    pickup_date: str,
    buyer_name: str = "",
    note: str = "",
    pickup_window: str | None = None,
) -> dict[str, Any]:
    """Create an order and a PayPal checkout for it.

    items: [{"item_id": 3, "quantity": 12}]. Prices are always taken from the menu.
    Returns paypal_approval_url. Give the HUMAN buyer exactly that URL to approve the payment in PayPal.
    Never construct or modify PayPal URLs yourself, and never claim the order is paid until
    get_order_status says "paid".
    """
    try:
        order = service.place_order(
            stall_id, items, pickup_date, buyer_name=buyer_name, note=note, pickup_window=pickup_window, channel="agent"
        )
    except RuleViolation as e:
        # Demand the seller could not meet is the most useful signal a small seller can get.
        if e.code in ("over_capacity", "no_pickup_that_day", "lead_time"):
            insights.record_unmet(stall_id, e.code, pickup_date, items if isinstance(items, list) else [])
        raise
    insights.record_agent_activity(stall_id, "3. Started PayPal checkout")
    return order


@_safe
def place_market_order(
    pickup_date: str,
    stalls: list[dict[str, Any]],
    buyer_name: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Buy from SEVERAL stalls for the same pickup date with ONE PayPal approval (a market run).

    stalls: [{"stall_id": "...", "items": [{"item_id": 3, "quantity": 2}], "pickup_window": "09:00-12:00" (optional)}]
    Up to 5 stalls. Each seller is paid directly and keeps its own order, pickup window and refunds.
    All or nothing: if any stall can't fill its part, nothing is held; the error names that stall.
    Returns market_order_id and paypal_approval_url for the HUMAN to approve once.
    """
    try:
        basket = market.place_basket(stalls, pickup_date, buyer_name=buyer_name, note=note, channel="agent")
    except RuleViolation as e:
        sid = getattr(e, "stall_id", None)
        if sid and e.code in ("over_capacity", "no_pickup_that_day", "lead_time"):
            part = next((p for p in stalls if isinstance(p, dict) and p.get("stall_id") == sid), {})
            insights.record_unmet(sid, e.code, pickup_date, part.get("items") or [])
        raise
    for o in basket["orders"]:
        insights.record_agent_activity(o["stall_id"], "3. Started PayPal checkout")
    return basket


@_safe
def get_market_order_status(market_order_id: str) -> dict[str, Any]:
    """Status of a market run and of each stall's order inside it. Captures payment if the human has approved."""
    return market.confirm_basket(market_order_id)


@_safe
def get_order_status(order_id: str) -> dict[str, Any]:
    """Get the status of an order (pending_payment, paid, ready, picked_up, shipped, refunded, expired)."""
    return service.order_view(order_id)


@_safe
def request_custom_order(
    stall_id: str, buyer_email: str, description: str, needed_by: str, budget_usd: float | None = None
) -> dict[str, Any]:
    """Ask a seller for a custom order (e.g. a birthday cake or catering tray) for a date (YYYY-MM-DD).

    The SELLER reviews the request and sets the price. If accepted, the buyer receives a PayPal
    invoice with a deposit by email. budget_usd is optional guidance for the seller.
    """
    budget = int(round(float(budget_usd) * 100)) if budget_usd else None
    return service.request_custom_order(stall_id, buyer_email, description, needed_by, budget)


@_safe
def get_custom_order_status(custom_order_id: str) -> dict[str, Any]:
    """Check a custom order request: requested (awaiting seller), sent (PayPal invoice emailed) or paid."""
    return service.custom_order_status(custom_order_id)


TOOLS: list[Callable[..., Any]] = [
    find_stalls, get_menu, check_availability, place_order, place_market_order, get_order_status, get_market_order_status,
    request_custom_order, get_custom_order_status,
]

# JSON schemas for the Claude tool-use buyer agent (the MCP server derives its own from signatures).
SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "find_stalls",
        "description": find_stalls.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "lat": {"type": "number"},
                "lng": {"type": "number"},
                "radius_km": {"type": "number"},
            },
        },
    },
    {
        "name": "get_menu",
        "description": get_menu.__doc__,
        "input_schema": {"type": "object", "properties": {"stall_id": {"type": "string"}}, "required": ["stall_id"]},
    },
    {
        "name": "check_availability",
        "description": check_availability.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {"stall_id": {"type": "string"}, "pickup_date": {"type": "string"}},
            "required": ["stall_id", "pickup_date"],
        },
    },
    {
        "name": "place_order",
        "description": place_order.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {
                "stall_id": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"item_id": {"type": "integer"}, "quantity": {"type": "integer"}},
                        "required": ["item_id", "quantity"],
                    },
                },
                "pickup_date": {"type": "string"},
                "buyer_name": {"type": "string"},
                "note": {"type": "string"},
                "pickup_window": {"type": "string"},
            },
            "required": ["stall_id", "items", "pickup_date"],
        },
    },
    {
        "name": "place_market_order",
        "description": place_market_order.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {
                "pickup_date": {"type": "string"},
                "stalls": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "stall_id": {"type": "string"},
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"item_id": {"type": "integer"}, "quantity": {"type": "integer"}},
                                    "required": ["item_id", "quantity"],
                                },
                            },
                            "pickup_window": {"type": "string"},
                        },
                        "required": ["stall_id", "items"],
                    },
                },
                "buyer_name": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["pickup_date", "stalls"],
        },
    },
    {
        "name": "get_market_order_status",
        "description": get_market_order_status.__doc__,
        "input_schema": {"type": "object", "properties": {"market_order_id": {"type": "string"}}, "required": ["market_order_id"]},
    },
    {
        "name": "get_order_status",
        "description": get_order_status.__doc__,
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "request_custom_order",
        "description": request_custom_order.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {
                "stall_id": {"type": "string"},
                "buyer_email": {"type": "string"},
                "description": {"type": "string"},
                "needed_by": {"type": "string"},
                "budget_usd": {"type": "number"},
            },
            "required": ["stall_id", "buyer_email", "description", "needed_by"],
        },
    },
    {
        "name": "get_custom_order_status",
        "description": get_custom_order_status.__doc__,
        "input_schema": {"type": "object", "properties": {"custom_order_id": {"type": "string"}}, "required": ["custom_order_id"]},
    },
]

BY_NAME = {fn.__name__: fn for fn in TOOLS}


def call(name: str, args: dict[str, Any]) -> Any:
    fn = BY_NAME.get(name)
    if not fn:
        return {"error": "unknown_tool", "message": f"No tool named {name}"}
    try:
        return fn(**(args or {}))
    except TypeError as e:  # bad/missing arguments from the model
        return {"error": "bad_arguments", "message": str(e)}
