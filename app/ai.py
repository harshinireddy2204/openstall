"""Claude integration: menu photo / text -> structured catalog.

Uses forced tool output so the model must return JSON matching our schema.
Falls back to a simple line parser when no ANTHROPIC_API_KEY is configured.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

from .config import settings

log = logging.getLogger("openstall.ai")
EXTRACTOR_VERSION = "2"  # bump when the extraction call changes (shown by the smoke test)

CATALOG_TOOL = {
    "name": "save_catalog",
    "description": "Save the structured catalog extracted from the seller's menu.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Stall / business name if visible, else empty."},
            "description": {"type": "string", "description": "One friendly sentence describing what they sell."},
            "category": {"type": "string", "description": "e.g. bakery, tamales, pickles, crafts, produce."},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "price": {"type": "number", "description": "Price in dollars for one unit. Never invent; use 0 if unreadable."},
                        "unit": {"type": "string", "description": "each, dozen, half dozen, jar, lb, tray..."},
                        "allergens": {"type": "array", "items": {"type": "string"}, "description": "Only if stated or obvious (wheat, dairy, eggs, nuts, soy)."},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "e.g. vegan, gluten-free, spicy, seasonal."},
                        "daily_capacity": {"type": ["integer", "null"], "description": "Max units per day if stated, else null."},
                    },
                    "required": ["name", "price", "unit"],
                },
            },
            "pickup_windows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "string", "description": "mon..sun or 'any'"},
                        "start": {"type": "string", "description": "HH:MM 24h"},
                        "end": {"type": "string", "description": "HH:MM 24h"},
                    },
                    "required": ["day", "start", "end"],
                },
            },
            "lead_time_hours": {"type": ["integer", "null"], "description": "Advance notice required, if stated."},
            "questions_for_seller": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything unclear the seller must confirm (unreadable prices, missing capacity...).",
            },
        },
        "required": ["items", "questions_for_seller"],
    },
}

SYSTEM = (
    "You turn small sellers' menus (chalkboards, flyers, Instagram posts, notes) into a clean catalog "
    "that AI shopping agents can read. Be faithful: never invent items or prices. If a price is unreadable, "
    "set it to 0 and add a question for the seller. Normalize units (e.g. 'per dozen' -> unit 'dozen'). "
    "Always call the save_catalog tool. If you cannot call tools, reply with only a JSON object matching its schema."
)

SUPPORTED_MEDIA = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def extract_catalog(*, text: str = "", image: bytes | None = None, media_type: str | None = None) -> dict[str, Any]:
    if not text.strip() and not image:
        raise ValueError("Provide a menu photo or some menu text.")
    if image and media_type not in SUPPORTED_MEDIA:
        raise ValueError("Unsupported image type. Use JPG, PNG, GIF or WEBP.")
    if image and len(image) > 5 * 1024 * 1024:
        raise ValueError("Image is larger than 5 MB. Please use a smaller photo.")

    if not settings.ai_live:
        if not text.strip():
            return {
                "items": [],
                "questions_for_seller": [
                    "Photo reading needs ANTHROPIC_API_KEY. In demo mode, please type your menu as lines like 'Cardamom bun - $4'."
                ],
                "source": "fallback",
            }
        return {**parse_menu_text(text), "source": "fallback"}

    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    content: list[dict[str, Any]] = []
    if image:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image).decode()},
        })
    content.append({"type": "text", "text": ("Seller notes:\n" + text) if text.strip() else "Extract this menu."})
    request = dict(
        model=settings.anthropic_model,
        max_tokens=2000,
        system=SYSTEM,
        tools=[CATALOG_TOOL],
        messages=[{"role": "user", "content": content}],
    )
    try:
        try:
            msg = client.messages.create(**request, tool_choice={"type": "tool", "name": "save_catalog"})
        except Exception as e:
            # Some models reject forced tool choice; retry without it (default = auto) plus an explicit instruction.
            if "tool_choice" not in str(e):
                raise
            log.info("Model rejected forced tool_choice, retrying without it")
            msg = client.messages.create(**request)
    except Exception as e:  # network, auth, billing, rate limit
        log.warning("Claude extraction failed: %s", e)
        if text.strip():
            fallback = parse_menu_text(text)
            fallback["questions_for_seller"].insert(0, "AI reading was unavailable, so this was parsed from your text.")
            return {**fallback, "source": "fallback"}
        raise ValueError("The AI menu reader is unavailable right now. Please type your menu instead.") from e

    block = next((b for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if block is not None:
        return {**sanitize_catalog(dict(block.input)), "source": "claude"}
    # Model answered in text instead of calling the tool: accept a JSON object if it gave one.
    raw = "".join(getattr(b, "text", "") for b in msg.content)
    parsed = _json_from_text(raw)
    if parsed is None:
        raise ValueError("Could not read a catalog from that input. Try a clearer photo or type the menu.")
    return {**sanitize_catalog(parsed), "source": "claude"}


def _json_from_text(raw: str) -> dict[str, Any] | None:
    import json
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


def sanitize_catalog(cat: dict[str, Any]) -> dict[str, Any]:
    """Defensive cleanup of model output before it reaches the seller."""
    items = []
    questions = list(cat.get("questions_for_seller") or [])
    for it in (cat.get("items") or [])[:100]:
        name = str(it.get("name") or "").strip()[:120]
        if not name:
            continue
        try:
            price = round(float(it.get("price") or 0), 2)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            questions.append(f"What is the price of '{name}'?")
        cap = it.get("daily_capacity")
        items.append({
            "name": name,
            "description": str(it.get("description") or "")[:300],
            "price": max(price, 0),
            "unit": str(it.get("unit") or "each")[:30],
            "allergens": [str(a)[:30] for a in (it.get("allergens") or [])][:10],
            "tags": [str(t)[:30] for t in (it.get("tags") or [])][:10],
            "daily_capacity": int(cap) if isinstance(cap, (int, float)) and cap > 0 else None,
        })
    windows = []
    for w in cat.get("pickup_windows") or []:
        if all(re.fullmatch(r"\d{1,2}:\d{2}", str(w.get(k, ""))) for k in ("start", "end")):
            windows.append({"day": str(w.get("day", "any")).lower()[:9], "start": str(w["start"]).zfill(5), "end": str(w["end"]).zfill(5)})
    lead = cat.get("lead_time_hours")
    return {
        "name": str(cat.get("name") or "")[:80],
        "description": str(cat.get("description") or "")[:300],
        "category": str(cat.get("category") or "")[:40],
        "items": items,
        "pickup_windows": windows,
        "lead_time_hours": int(lead) if isinstance(lead, (int, float)) and lead >= 0 else None,
        "questions_for_seller": questions[:10],
    }


_LINE = re.compile(r"^\s*[-*•]?\s*(?P<name>[^$\d][^$]*?)\s*[-:.… ]*\s*\$?\s*(?P<price>\d+(?:\.\d{1,2})?)\s*(?:/\s*|per\s+)?(?P<unit>[a-zA-Z ]{0,15})?\s*$")


def parse_menu_text(text: str) -> dict[str, Any]:
    """Offline fallback: 'Cardamom bun - $4 each' style lines."""
    items, questions = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name = m.group("name").strip(" -:.")
        unit = (m.group("unit") or "each").strip().lower() or "each"
        items.append({
            "name": name[:120], "description": "", "price": float(m.group("price")), "unit": unit,
            "allergens": [], "tags": [], "daily_capacity": None,
        })
    if not items:
        questions.append("No priced items found. Use one item per line, like 'Cardamom bun - $4'.")
    questions.append("How many of each item can you make per day? (Optional, prevents overselling.)")
    return {"name": "", "description": "", "category": "", "items": items, "pickup_windows": [],
            "lead_time_hours": None, "questions_for_seller": questions}
