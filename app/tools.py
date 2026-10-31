"""Agent-facing tools. One definition, used by the MCP server and the demo buyer agent.

Each tool returns a JSON-serializable dict. Rule violations come back as
{"error": code, "message": ...} so agents can recover instead of crashing.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from . import service
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
    return {
        **service.stall_summary(stall),
        "pickup_address": stall["pickup_address"],
        "pickup_windows": stall["pickup_windows"],
        "items": [service.public_item(i) for i in stall["items"]],
    }


@_safe
def check_availability(stall_id: str, pickup_date: str) -> dict[str, Any]:
    """Check if a stall can fill orders for a pickup date (YYYY-MM-DD): open windows and remaining quantity per item."""
    return service.check_availability(stall_id, pickup_date)


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
    return service.place_order(
        stall_id, items, pickup_date, buyer_name=buyer_name, note=note, pickup_window=pickup_window, channel="agent"
    )


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
    find_stalls, get_menu, check_availability, place_order, get_order_status, request_custom_order, get_custom_order_status,
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
