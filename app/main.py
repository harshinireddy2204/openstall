"""OpenStall web app: seller onboarding, public stall pages, buyer agent, PayPal
return/webhook handling, and the MCP endpoint at /agent/mcp."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import ai, buyer_agent, service
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


def seed_demo_data() -> None:
    if query_one("SELECT 1 FROM stalls LIMIT 1"):
        return
    demo_email = os.getenv("DEMO_SELLER_PAYPAL_EMAIL", "").strip()
    demo_key = os.getenv("DEMO_OWNER_KEY", "").strip()  # stable dashboard links for judges across restarts
    for stall in json.loads((ROOT / "seed" / "demo_stalls.json").read_text()):
        stall.setdefault("paypal_email", demo_email)
        if demo_key:
            stall["owner_key"] = demo_key
        created = service.create_stall(stall)
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

# ------------------------------------------------------------ rate limiting
# (path prefix, method, max requests, window seconds). In-memory, per client IP: fine for one instance.
RATE_LIMITS = [
    ("/api/stalls", "POST", 30, 600),        # stall creation + order creation + seller actions
    ("/api/agent/chat", "POST", 20, 60),
    ("/api/catalog/extract", "POST", 10, 600),
    ("/agent/mcp", "POST", 120, 60),
    ("/webhooks/", "POST", 300, 60),
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
    events = query("SELECT * FROM events WHERE stall_id=? ORDER BY id DESC LIMIT 25", (stall_id,))
    return templates.TemplateResponse(
        request, "dashboard.html", {"stall": stall, "orders": orders, "invoices": invoices, "events": events, "key": key}
    )


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


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    return (
        "# OpenStall\n\n"
        "> Independent local sellers (home bakers, market stalls, makers) made buyable by AI agents. Payments via PayPal.\n\n"
        f"MCP endpoint (streamable HTTP): {settings.mcp_url}\n"
        "Tools: find_stalls, get_menu, check_availability, place_order, get_order_status, request_custom_order, get_custom_order_status\n"
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
    url = f"/order/{order}"
    if result.get("error"):
        url += "?error=" + result["error"].replace(" ", "+")
    return RedirectResponse(url, status_code=303)


@app.get("/paypal/cancel")
def paypal_cancel(order: str):
    try:
        service.cancel_order(order)
    except RuleViolation:
        raise HTTPException(404, "Order not found")
    return RedirectResponse(f"/order/{order}?error=Payment+cancelled.+No+money+was+taken.", status_code=303)


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
    return templates.TemplateResponse(request, "mock_paypal.html", {"order": service.order_view(o["id"]), "token": token})


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
