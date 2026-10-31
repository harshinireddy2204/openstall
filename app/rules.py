"""Deterministic seller rules.

The LLM (ours or a buyer's agent) can only *propose* an order. Everything here
decides whether it is allowed: prices come from the catalog, never from the
agent; capacity, lead time and pickup windows are hard limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_ALIASES = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun", "daily": "any",
    "everyday": "any", "every day": "any", "all": "any",
}


class RuleViolation(ValueError):
    """Raised when a proposed order breaks a seller rule. Message is agent-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def normalize_day(day: str) -> str:
    d = (day or "").strip().lower()
    d = DAY_ALIASES.get(d, d)
    return d[:3] if d[:3] in DAYS else ("any" if d == "any" else d)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise RuleViolation("invalid_date", f"pickup_date must be YYYY-MM-DD, got {value!r}")


def windows_for_date(pickup_windows: list[dict[str, str]], d: date) -> list[dict[str, str]]:
    dow = DAYS[d.weekday()]
    return [w for w in pickup_windows if normalize_day(w.get("day", "")) in (dow, "any")]


def window_label(w: dict[str, str]) -> str:
    return f"{w['start']}-{w['end']}"


@dataclass
class PricedLine:
    item_id: int
    name: str
    quantity: int
    unit_cents: int

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_cents

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "quantity": self.quantity,
            "unit_cents": self.unit_cents,
            "total_cents": self.total_cents,
        }


def resolve_window(stall: dict[str, Any], d: date, requested: str | None, now: datetime | None = None) -> str:
    """Pick a valid pickup window for the date and enforce lead time."""
    tz = ZoneInfo(stall.get("timezone") or "America/New_York")
    now = (now or datetime.now(tz)).astimezone(tz)
    windows = windows_for_date(stall.get("pickup_windows") or [], d)
    if not windows:
        open_days = sorted({normalize_day(w.get("day", "")) for w in stall.get("pickup_windows") or []})
        raise RuleViolation(
            "no_pickup_that_day",
            f"{stall['name']} has no pickup on {d.strftime('%A')}. Pickup days: {', '.join(open_days) or 'none set'}.",
        )
    lead = timedelta(hours=int(stall.get("lead_time_hours") or 0))
    candidates = windows
    if requested:
        candidates = [w for w in windows if window_label(w) == requested.strip()] or windows
    for w in candidates:
        start = datetime.combine(d, time.fromisoformat(w["start"]), tz)
        if start - now >= lead:
            return window_label(w)
    earliest = now + lead
    raise RuleViolation(
        "lead_time",
        f"{stall['name']} needs {stall.get('lead_time_hours')}h notice. "
        f"Earliest possible pickup is after {earliest.strftime('%a %b %d %H:%M')}.",
    )


def price_lines(
    items_by_id: dict[int, dict[str, Any]],
    requested: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[PricedLine]:
    """Turn agent-proposed [{item_id, quantity}] into catalog-priced lines."""
    if not requested:
        raise RuleViolation("empty_order", "An order needs at least one item.")
    merged: dict[int, int] = {}
    for line in requested:
        try:
            item_id = int(line["item_id"])
            qty = int(line["quantity"])
        except (KeyError, TypeError, ValueError):
            raise RuleViolation("invalid_line", "Each line needs an integer item_id and quantity.")
        if qty <= 0:
            raise RuleViolation("invalid_quantity", "Quantities must be positive.")
        if item_id not in items_by_id:
            raise RuleViolation("unknown_item", f"Item {item_id} is not on this stall's menu.")
        merged[item_id] = merged.get(item_id, 0) + qty
    if sum(merged.values()) > max_items:
        raise RuleViolation("too_many_items", f"Orders are limited to {max_items} items.")
    return [
        PricedLine(i, items_by_id[i]["name"], q, int(items_by_id[i]["price_cents"]))
        for i, q in merged.items()
    ]


def check_capacity(
    lines: list[PricedLine],
    items_by_id: dict[int, dict[str, Any]],
    reserved: dict[int, int],
) -> None:
    """reserved: item_id -> quantity already held/paid for that pickup date."""
    for line in lines:
        cap = items_by_id[line.item_id].get("daily_capacity")
        if cap is None:
            continue
        left = int(cap) - reserved.get(line.item_id, 0)
        if line.quantity > left:
            raise RuleViolation(
                "over_capacity",
                f"Only {max(left, 0)} x {line.name} left for that day (requested {line.quantity}).",
            )


def check_total(lines: list[PricedLine], max_cents: int) -> int:
    total = sum(l.total_cents for l in lines)
    if total <= 0:
        raise RuleViolation("invalid_total", "Order total must be positive.")
    if total > max_cents:
        raise RuleViolation("over_limit", f"Agent orders are capped at ${max_cents / 100:.2f}. Use a custom order instead.")
    return total
