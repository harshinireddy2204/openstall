"""OpenStall web app: seller onboarding, public stall pages, buyer agent, PayPal
return/webhook handling, and the MCP endpoint at /agent/mcp."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import ai, buyer_agent, insights, market, markets, service
from .config import ROOT, settings
from .db import log_event, now_iso, query, query_one, tx
from .mcp_server import build_app, mcp
from .paypal import paypal
from .rules import RuleViolation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("openstall")

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals.update(settings=settings, fmt_money=service.fmt_money, paypal_live=paypal.live)


def apply_demo_payees() -> None:
    """DEMO_STALL_PAYEES="stall-id=seller@business.example.com,..." gives each demo stall its own
    sandbox PayPal account, so a market run visibly pays several different sellers."""
    for pair in os.getenv("DEMO_STALL_PAYEES", "").split(","):
        sid, _, email = pair.strip().partition("=")
        if sid and "@" in email:
            with tx() as c:
                c.execute("UPDATE stalls SET paypal_email=? WHERE id=?", (email.strip(), sid.strip()))


def demo_stall_ids() -> set[str]:
    """The seeded demo stalls (shared by everyone trying OpenStall)."""
    names = [s["name"] for s in json.loads((ROOT / "seed" / "demo_stalls.json").read_text())]
    return {r["id"] for n in names if (r := query_one("SELECT id FROM stalls WHERE name=?", (n,)))}


def apply_demo_capacity() -> None:
    """Many people order from the shared demo stalls, so each item makes at least DEMO_CAPACITY_FLOOR per day.
    Higher limits set on purpose (for example through the Insights assistant) are kept."""
    floor = int(os.getenv("DEMO_CAPACITY_FLOOR", "150") or 0)
    ids = demo_stall_ids()
    if floor <= 0 or not ids:
        return
    with tx() as c:
        c.execute(f"UPDATE items SET daily_capacity=? WHERE stall_id IN ({','.join('?' * len(ids))}) "
                  "AND daily_capacity IS NOT NULL AND daily_capacity < ?", (floor, *ids, floor))


def seed_demo_data() -> None:
    _seed_demo_data()
    apply_demo_payees()
    apply_demo_capacity()
    seed_demo_market()


DEMO_MARKET_ID = "lowell-saturday-market"


def seed_demo_market() -> None:
    """One demo farmers market grouping the demo stalls, so judges can see the organizer view."""
    if query_one("SELECT 1 FROM markets WHERE id=?", (DEMO_MARKET_ID,)):
        return
    ids = [r["id"] for name in (s["name"] for s in json.loads((ROOT / "seed" / "demo_stalls.json").read_text()))
           if (r := query_one("SELECT id FROM stalls WHERE name=?", (name,)))]
    key = os.getenv("DEMO_OWNER_KEY", "").strip() or None
    m = markets.create_market("Lowell Saturday Market", "Lowell, MA", "Saturdays 9:00 to 13:00",
                              "Home bakers, tamale makers and picklers from around the Merrimack Valley.",
                              ids, organizer_key=key, market_id=DEMO_MARKET_ID)
    log.info("Seeded market %s (organizer key: %s)", m["market_id"], m["organizer_key"])


def _seed_demo_data() -> None:
    if query_one("SELECT 1 FROM stalls LIMIT 1"):
        # Older databases: give the demo stalls their sample history once.
        for stall in json.loads((ROOT / "seed" / "demo_stalls.json").read_text()):
            sid = query_one("SELECT id FROM stalls WHERE name=?", (stall["name"],))
            if sid and not query_one("SELECT 1 FROM orders WHERE stall_id=? AND buyer_note LIKE '[sample]%' LIMIT 1", (sid["id"],)):
                insights.seed_sample_history(sid["id"])
        return
    demo_email = os.getenv("DEMO_SELLER_PAYPAL_EMAIL", "").strip()
    demo_key = os.getenv("DEMO_OWNER_KEY", "").strip()  # stable dashboard links for judges across restarts
    for stall in json.loads((ROOT / "seed" / "demo_stalls.json").read_text()):
        stall.setdefault("paypal_email", demo_email)
        if demo_key:
            stall["owner_key"] = demo_key
        created = service.create_stall(stall)
        insights.seed_sample_history(created["id"])
        log.info("Seeded stall %s (dashboard key: %s)", created["id"], created["owner_key"])


mcp_app = build_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_demo_data()
    log.info("PayPal mode: %s | AI mode: %s | MCP: %s",
             "SANDBOX" if paypal.live else "MOCK", "CLAUDE" if settings.ai_live else "FALLBACK", settings.mcp_url)
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="OpenStall", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)  # the Insights bundle is ~5 MB raw, ~1.3 MB gzipped

# ------------------------------------------------------------ rate limiting
# (path prefix, method, max requests, window seconds). In-memory, per client IP: fine for one instance.
RATE_LIMITS = [
    ("/api/stalls", "POST", 30, 600),        # stall creation + order creation + seller actions
    ("/api/market", "POST", 60, 600),        # market runs (creation + confirm polling)
    ("/api/agent/chat", "POST", 20, 60),
    ("/api/catalog/extract", "POST", 10, 600),
    ("/agent/mcp", "POST", 120, 60),
    ("/webhooks/", "POST", 300, 60),
    ("/api/studio/llm", "POST", 40, 600),
    ("/api/grid/command", "POST", 30, 600),   # orders grid: plain-English view changes (Claude)
    ("/api/markets", "POST", 10, 600),
]
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


@app.middleware("http")
async def guard(request: Request, call_next):
    path, method = request.url.path, request.method
    for prefix, m, limit, window in RATE_LIMITS:
        if method == m and path.startswith(prefix):
            key, now = (prefix, _client_ip(request)), time.monotonic()
            with _hits_lock:
                q = _hits[key]
                while q and now - q[0] > window:
                    q.popleft()
                if len(q) >= limit:
                    return JSONResponse(status_code=429, content={"error": "rate_limited", "message": "Too many requests, slow down."},
                                        headers={"Retry-After": str(window)})
                q.append(now)
            break
    response = await call_next(request)
    # Security headers. no-referrer keeps the private dashboard key out of Referer headers.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
app.mount("/agent", mcp_app)


@app.exception_handler(RuleViolation)
async def rule_violation_handler(request: Request, exc: RuleViolation):
    return JSONResponse(status_code=422, content=exc.as_dict())


@app.exception_handler(PermissionError)
async def permission_handler(request: Request, exc: PermissionError):
    return JSONResponse(status_code=403, content={"error": "forbidden", "message": str(exc)})


# ----------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    stalls = [service.get_stall(s["id"]) for s in query("SELECT id FROM stalls ORDER BY created_at")]
    return templates.TemplateResponse(request, "index.html", {"stalls": stalls})


@app.get("/sell", response_class=HTMLResponse)
def sell(request: Request):
    return templates.TemplateResponse(request, "sell.html", {})


@app.get("/s/{stall_id}", response_class=HTMLResponse)
def stall_page(request: Request, stall_id: str):
    stall = service.get_stall(stall_id)
    if not stall:
        raise HTTPException(404, "Stall not found")
    jsonld = {
        "@context": "https://schema.org",
        "@type": "Store",
        "name": stall["name"],
        "description": stall["description"],
        "address": stall["city"],
        "url": f"{settings.base_url}/s/{stall['id']}",
        "paymentAccepted": "PayPal",
        "makesOffer": [
            {
                "@type": "Offer",
                "name": it["name"],
                "price": f"{it['price_cents'] / 100:.2f}",
                "priceCurrency": settings.currency,
                "description": it["description"],
            }
            for it in stall["items"]
        ],
    }
    # Escape <, > and & so seller-controlled text can never close the <script> tag (XSS).
    safe_jsonld = json.dumps(jsonld, indent=2).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return templates.TemplateResponse(request, "stall.html", {"stall": stall, "jsonld": safe_jsonld})


@app.get("/s/{stall_id}/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, stall_id: str, key: str = ""):
    try:
        stall = service.check_owner(stall_id, key)
    except PermissionError:
        raise HTTPException(403, "Open the dashboard link you received when you created the stall.")
    orders = service.list_orders(stall_id)
    invoices = query("SELECT * FROM invoices WHERE stall_id=? ORDER BY created_at DESC", (stall_id,))
    events = query("SELECT * FROM events WHERE stall_id=? AND kind NOT IN ('agent_activity','demand_unmet') "
                   "ORDER BY id DESC LIMIT 25", (stall_id,))
    # Someone running several stalls with one key (like the demo stalls) can switch between them.
    siblings = [s for s in query("SELECT id, name, owner_key FROM stalls ORDER BY created_at")
                if s["id"] != stall_id and secrets.compare_digest(s["owner_key"], key)]
    return templates.TemplateResponse(
        request, "dashboard.html", {"stall": stall, "orders": orders, "invoices": invoices, "events": events, "key": key,
                                    "siblings": [{"id": s["id"], "name": s["name"]} for s in siblings],
                                    "shared_demo": stall_id in demo_stall_ids(),
                                    "orders_config": {"stallId": stall_id, "key": key, "ships": bool(stall.get("ships")),
                                                      "licenseKey": settings.ag_grid_license_key, "aiLive": settings.ai_live}}
    )


@app.get("/s/{stall_id}/insights", response_class=HTMLResponse)
def insights_page(request: Request, stall_id: str, key: str = ""):
    try:
        stall = service.check_owner(stall_id, key)
    except PermissionError:
        raise HTTPException(403, "Open the Insights link from your seller dashboard.")
    return templates.TemplateResponse(request, "insights.html", {
        "stall": stall, "key": key,
        "studio_config": {
            "stallId": stall_id, "key": key, "stallName": stall["name"],
            "licenseKey": settings.ag_studio_license_key, "aiLive": settings.ai_live,
        },
    })


# ------------------------------------------------------------------ markets
@app.get("/m/{market_id}", response_class=HTMLResponse)
def market_home(request: Request, market_id: str):
    try:
        m = markets.get_market(market_id)
    except RuleViolation:
        raise HTTPException(404, "Market not found")
    return templates.TemplateResponse(request, "market_home.html", {
        "m": m, "page_url": f"{settings.base_url}/m/{market_id}", "qr": _qr_svg(f"{settings.base_url}/m/{market_id}"),
    })


def _qr_svg(url: str) -> str:
    import segno
    return segno.make(url, error="m").svg_inline(scale=6, dark="#1f1b16", light="#ffffff", border=2)


@app.get("/m/{market_id}/qr.svg")
def market_qr(market_id: str):
    markets.get_market(market_id)
    return Response(_qr_svg(f"{settings.base_url}/m/{market_id}"), media_type="image/svg+xml")


@app.get("/m/{market_id}/organizer", response_class=HTMLResponse)
def market_organizer(request: Request, market_id: str, key: str = ""):
    try:
        mrow = markets.check_organizer(market_id, key)
    except PermissionError:
        raise HTTPException(403, "Open the organizer link you received when you created the market.")
    m = markets.get_market(market_id)
    in_market = set(markets.stall_ids(market_id))
    others = [s for s in query("SELECT id, name, city FROM stalls ORDER BY created_at") if s["id"] not in in_market]
    return templates.TemplateResponse(request, "market_organizer.html", {
        "m": m, "key": key, "others": others, "locked": market_id == DEMO_MARKET_ID, "page_url": f"{settings.base_url}/m/{market_id}",
        "orders_config": {"marketId": market_id, "key": key, "licenseKey": settings.ag_grid_license_key, "aiLive": settings.ai_live},
    })


@app.get("/m/{market_id}/insights", response_class=HTMLResponse)
def market_insights_page(request: Request, market_id: str, key: str = ""):
    try:
        markets.check_organizer(market_id, key)
    except PermissionError:
        raise HTTPException(403, "Open Insights from the organizer page.")
    m = markets.get_market(market_id)
    return templates.TemplateResponse(request, "insights.html", {
        "stall": {"id": market_id, "name": m["name"]}, "key": key, "market": m,
        "studio_config": {"marketId": market_id, "key": key, "stallName": m["name"], "stalls": [s["name"] for s in m["stalls"]],
                          "licenseKey": settings.ag_studio_license_key, "aiLive": settings.ai_live},
    })


@app.get("/markets/new", response_class=HTMLResponse)
def market_new(request: Request):
    stalls = query("SELECT id, name, city, category FROM stalls ORDER BY created_at")
    return templates.TemplateResponse(request, "market_new.html", {"stalls": stalls})


class MarketCreateIn(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    city: str = Field(default="", max_length=80)
    schedule: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=400)
    stall_ids: list[str] = Field(default_factory=list, max_length=40)


@app.post("/api/markets")
def api_create_market(body: MarketCreateIn):
    m = markets.create_market(body.name, body.city, body.schedule, body.description, body.stall_ids)
    return {**m, "public_url": f"{settings.base_url}/m/{m['market_id']}",
            "organizer_url": f"{settings.base_url}/m/{m['market_id']}/organizer?key={m['organizer_key']}"}


class MarketStallIn(BaseModel):
    key: str
    stall_id: str
    action: str = Field(pattern="^(add|remove)$")


@app.post("/api/markets/{market_id}/stalls")
def api_market_stalls(market_id: str, body: MarketStallIn):
    markets.check_organizer(market_id, body.key)
    if market_id == DEMO_MARKET_ID:
        raise RuleViolation("demo_market_locked", "This demo market is shared by everyone trying OpenStall, so its stalls "
                            "stay fixed. Create your own market at /markets/new to add or remove stalls.")
    (markets.add_stall if body.action == "add" else markets.remove_stall)(market_id, body.stall_id)
    return markets.get_market(market_id)


@app.get("/api/markets")
def api_markets(q: str = ""):
    return {"markets": markets.list_markets(q)}


@app.get("/api/markets/{market_id}")
def api_market(market_id: str):
    return markets.get_market(market_id)


@app.get("/api/markets/{market_id}/orders")
def api_market_orders(market_id: str, key: str = ""):
    markets.check_organizer(market_id, key)
    return {"orders": markets.market_orders(market_id)}


@app.get("/api/markets/{market_id}/insights")
def api_market_insights(market_id: str, key: str = ""):
    markets.check_organizer(market_id, key)
    return markets.market_insights(market_id)


@app.get("/orders", response_class=HTMLResponse)
def my_orders(request: Request):
    return templates.TemplateResponse(request, "my_orders.html", {})


@app.get("/shop", response_class=HTMLResponse)
def agent_page(request: Request):
    return templates.TemplateResponse(request, "agent.html", {})


@app.get("/order/{order_id}", response_class=HTMLResponse)
def order_page(request: Request, order_id: str, error: str = ""):
    try:
        order = service.order_view(order_id)
    except RuleViolation:
        raise HTTPException(404, "Order not found")
    return templates.TemplateResponse(request, "order.html", {"order": order, "error": error})


@app.get("/market", response_class=HTMLResponse)
def market_builder(request: Request, m: str = ""):
    group = None
    if m:
        try:
            group = markets.get_market(m)
        except RuleViolation:
            raise HTTPException(404, "Market not found")
        ids = markets.stall_ids(m)
    else:
        ids = [s["id"] for s in query("SELECT id FROM stalls ORDER BY created_at")]
    stalls = [s for sid in ids if (s := service.get_stall(sid))]
    return templates.TemplateResponse(request, "market.html", {"stalls": stalls, "group": group})


@app.get("/market/{market_order_id}", response_class=HTMLResponse)
def market_page(request: Request, market_order_id: str, error: str = ""):
    try:
        basket = market.basket_view(market_order_id)
    except RuleViolation:
        raise HTTPException(404, "Market order not found")
    return templates.TemplateResponse(request, "market_order.html", {"basket": basket, "error": error})


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    return (
        "# OpenStall\n\n"
        "> Independent local sellers (home bakers, market stalls, makers) made buyable by AI agents. Payments via PayPal.\n\n"
        f"MCP endpoint (streamable HTTP): {settings.mcp_url}\n"
        "Tools: find_stalls, get_menu, check_availability, place_order, place_market_order, get_order_status, "
        "get_market_order_status, request_custom_order, get_custom_order_status\n"
        "Market runs: place_market_order buys from up to 5 stalls for one pickup day with ONE PayPal approval; "
        "each seller is paid directly.\n"
        "Orders return a PayPal approval link that the human buyer must approve. Agents never handle payment credentials.\n"
    )


# ----------------------------------------------------------------- seller API
@app.post("/api/catalog/extract")
async def api_extract(text: str = Form(""), photo: UploadFile | None = File(None)):
    image = await photo.read() if photo and photo.filename else None
    try:
        catalog = ai.extract_catalog(text=text, image=image, media_type=photo.content_type if image else None)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": "extract_failed", "message": str(e)})
    return catalog


class ItemIn(BaseModel):
    name: str
    description: str = ""
    price: float = Field(gt=0, le=10000)
    unit: str = "each"
    allergens: list[str] = []
    tags: list[str] = []
    daily_capacity: int | None = Field(default=None, ge=1, le=100000)


class WindowIn(BaseModel):
    day: str
    start: str
    end: str


class StallIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    category: str = ""
    city: str = ""
    lat: float | None = None
    lng: float | None = None
    pickup_address: str = ""
    paypal_email: str = ""
    lead_time_hours: int = Field(default=24, ge=0, le=24 * 30)
    ships: bool = False
    pickup_windows: list[WindowIn] = Field(min_length=1)
    items: list[ItemIn] = Field(min_length=1, max_length=100)


@app.post("/api/stalls")
def api_create_stall(body: StallIn):
    stall = service.create_stall(body.model_dump())
    return {
        "stall_id": stall["id"],
        "public_url": f"{settings.base_url}/s/{stall['id']}",
        "dashboard_url": f"{settings.base_url}/s/{stall['id']}/dashboard?key={stall['owner_key']}",
        "mcp_url": settings.mcp_url,
    }


class SellerAction(BaseModel):
    key: str
    action: str
    tracking_number: str = ""
    carrier: str = "USPS"
    note: str = ""


@app.post("/api/stalls/{stall_id}/orders/{order_id}")
def api_seller_action(stall_id: str, order_id: str, body: SellerAction):
    service.check_owner(stall_id, body.key)
    return service.seller_update_order(
        stall_id, order_id, body.action, tracking_number=body.tracking_number, carrier=body.carrier, note=body.note
    )


class QuoteIn(BaseModel):
    key: str
    amount: float = Field(gt=0, le=50000)
    deposit_percent: int = Field(default=30, ge=10, le=100)


@app.post("/api/stalls/{stall_id}/custom/{ref}/quote")
def api_send_quote(stall_id: str, ref: str, body: QuoteIn):
    service.check_owner(stall_id, body.key)
    cents = service.to_cents(body.amount) or 0
    return service.send_custom_quote(stall_id, ref, cents, body.deposit_percent)


# --------------------------------------------------------------- seller Insights
@app.get("/api/stalls/{stall_id}/insights")
def api_insights(stall_id: str, key: str = ""):
    stall = service.check_owner(stall_id, key)
    return {
        "sources": insights.insights_sources(stall_id),
        "demand": insights.demand_summary(stall_id),
        "pickup_board": insights.pickup_board(stall_id),
        "rules": insights.stall_rules(stall),
    }


class CapacityIn(BaseModel):
    key: str
    item_id: int
    daily_capacity: int | None = Field(default=None, ge=0, le=10000)


@app.post("/api/stalls/{stall_id}/capacity")
def api_set_capacity(stall_id: str, body: CapacityIn):
    service.check_owner(stall_id, body.key)
    return service.set_daily_capacity(stall_id, body.item_id, body.daily_capacity)


def _check_scope(stall: str, market_id: str, key: str) -> None:
    """Seller key for one stall, or organizer key for a market (read-only across its stalls)."""
    if market_id:
        markets.check_organizer(market_id, key)
    else:
        service.check_owner(stall, key)


@app.get("/api/stalls/{stall_id}/orders")
def api_seller_orders(stall_id: str, key: str = ""):
    service.check_owner(stall_id, key)
    return {"orders": service.list_orders(stall_id)}


@app.post("/api/grid/command")
async def api_grid_command(request: Request, stall: str = "", market: str = "", key: str = ""):
    """AG Grid AI Toolkit: turn a plain-English request into grid state with Claude. Owner or organizer key required."""
    _check_scope(stall, market, key)
    raw = await request.body()
    if len(raw) > 200_000:
        raise HTTPException(413, "Request too large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    return await run_in_threadpool(insights.run_grid_command, body)


@app.post("/api/studio/llm")
async def api_studio_llm(request: Request, stall: str = "", market: str = "", key: str = ""):
    """Claude bridge for the AG Studio agent. Owner or organizer key required so only they spend tokens."""
    _check_scope(stall, market, key)
    raw = await request.body()
    if len(raw) > 400_000:
        raise HTTPException(413, "Request too large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    return await run_in_threadpool(insights.run_studio_turn, body)


# ------------------------------------------------------------------ market runs
class MarketStallIn(BaseModel):
    stall_id: str
    items: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    pickup_window: str | None = None


class MarketIn(BaseModel):
    pickup_date: str
    stalls: list[MarketStallIn] = Field(min_length=1, max_length=market.MAX_STALLS)
    buyer_name: str = ""
    note: str = ""
    client_ref: str | None = Field(default=None, max_length=64)  # one per basket, makes resubmits safe


@app.post("/api/market")
def api_place_market(body: MarketIn):
    return market.place_basket([s.model_dump() for s in body.stalls], body.pickup_date,
                               buyer_name=body.buyer_name, note=body.note, channel="web", client_ref=body.client_ref)


@app.get("/api/market/{market_order_id}")
def api_market(market_order_id: str):
    return market.basket_view(market_order_id)


@app.post("/api/market/{market_order_id}/confirm")
def api_market_confirm(market_order_id: str):
    return market.confirm_basket(market_order_id)


# ------------------------------------------------------------------ buyer API
class OrderIn(BaseModel):
    items: list[dict[str, Any]]
    pickup_date: str
    pickup_window: str | None = None
    buyer_name: str = ""
    note: str = ""


@app.get("/api/stalls/{stall_id}/availability")
def api_availability(stall_id: str, date: str):
    return service.check_availability(stall_id, date)


@app.post("/api/stalls/{stall_id}/orders")
def api_place_order(stall_id: str, body: OrderIn):
    return service.place_order(
        stall_id, body.items, body.pickup_date, buyer_name=body.buyer_name, note=body.note,
        pickup_window=body.pickup_window, channel="web",
    )


@app.get("/api/orders/{order_id}")
def api_order(order_id: str):
    return service.order_view(order_id)


@app.post("/api/orders/{order_id}/confirm")
def api_confirm_order(order_id: str):
    """Called by the page after the PayPal popup closes (or while polling)."""
    return service.confirm_if_approved(order_id)


class ChatIn(BaseModel):
    message: str = Field(max_length=2000)
    session_id: str | None = None


@app.post("/api/agent/chat")
def api_agent_chat(body: ChatIn):
    return buyer_agent.chat(body.session_id, body.message)


# --------------------------------------------------------------------- PayPal
@app.get("/paypal/return")
def paypal_return(order: str, token: str = ""):
    o = service.get_order(order)
    if not o:
        raise HTTPException(404, "Order not found")
    if token and o["paypal_order_id"] and token != o["paypal_order_id"]:
        raise HTTPException(400, "PayPal token does not match this order")
    result = service.complete_payment(order)
    url = f"/market/{o['basket_id']}" if o.get("basket_id") and market.is_single(o["basket_id"]) else f"/order/{order}"
    if result.get("error"):
        url += "?error=" + result["error"].replace(" ", "+")
    return RedirectResponse(url, status_code=303)


@app.get("/paypal/cancel")
def paypal_cancel(order: str):
    try:
        service.cancel_order(order)
    except RuleViolation:
        raise HTTPException(404, "Order not found")
    o = service.get_order(order)
    base = f"/market/{o['basket_id']}" if o and o.get("basket_id") and market.is_single(o["basket_id"]) else f"/order/{order}"
    return RedirectResponse(f"{base}?error=Payment+cancelled.+No+money+was+taken.", status_code=303)


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    if not paypal.verify_webhook(dict(request.headers), event):
        log.warning("Rejected unverified webhook %s", event.get("id"))
        return JSONResponse(status_code=401, content={"error": "unverified"})
    event_id = event.get("id") or ""
    if event_id:
        try:  # unique key = atomic dedupe; PayPal retries deliveries
            with tx() as c:
                c.execute("INSERT INTO webhook_events (id, event_type, outcome, received_at) VALUES (?,?,?,?)",
                          (event_id, event.get("event_type"), "processing", now_iso()))
        except sqlite3.IntegrityError:
            return {"status": "duplicate"}
    try:
        outcome = service.handle_webhook(event)
    except RuleViolation as e:
        outcome = f"error:{e.code}"
    except Exception:
        log.exception("Webhook processing failed for %s", event_id)
        if event_id:  # let PayPal retry later
            with tx() as c:
                c.execute("DELETE FROM webhook_events WHERE id=?", (event_id,))
        return JSONResponse(status_code=500, content={"error": "processing_failed"})
    if event_id:
        with tx() as c:
            c.execute("UPDATE webhook_events SET outcome=? WHERE id=?", (outcome, event_id))
    log_event("webhook", event_id=event_id, type=event.get("event_type"), outcome=outcome)
    return {"status": outcome}


# Local stand-in for the PayPal approval screen when running without credentials.
@app.get("/mock-paypal/approve", response_class=HTMLResponse)
def mock_approve_page(request: Request, token: str):
    o = query_one("SELECT * FROM orders WHERE paypal_order_id=?", (token,))
    if paypal.live or not o:
        raise HTTPException(404)
    view = service.order_view(o["id"])
    if o.get("basket_id") and market.is_single(o["basket_id"]):
        b = market.basket_view(o["basket_id"])  # one approval pays every stall in the market run
        view = {**view, "total": b["total"], "stall_name": ", ".join(x["stall_name"] for x in b["orders"]),
                "lines": [{**l, "name": f"{l['name']} ({x['stall_name']})"} for x in b["orders"] for l in x["lines"]]}
    return templates.TemplateResponse(request, "mock_paypal.html", {"order": view, "token": token})


@app.post("/mock-paypal/approve")
def mock_approve(token: str = Form(...), decision: str = Form("approve")):
    o = query_one("SELECT * FROM orders WHERE paypal_order_id=?", (token,))
    if paypal.live or not o:
        raise HTTPException(404)
    if decision == "approve":
        paypal.mock_approve(token)
        return RedirectResponse(f"/paypal/return?order={o['id']}&token={token}", status_code=303)
    return RedirectResponse(f"/paypal/cancel?order={o['id']}", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "paypal": "sandbox" if paypal.live else "mock", "ai": "claude" if settings.ai_live else "fallback"}
