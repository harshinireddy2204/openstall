"""PayPal REST client (sandbox).

Covers the surface OpenStall needs:
  * OAuth 2.0 client credentials (cached token)
  * Orders v2: create, get, capture, add tracking
  * Payments v2: refund a capture
  * Invoicing v2: create, send (custom orders with a deposit)
  * Webhooks: verify signature

When no credentials are configured the client runs in MOCK mode so the whole
app (and the test suite) works offline. Mock approval links point at a local
page that simulates the PayPal approval screen.
"""

from __future__ import annotations

import logging
import threading
from decimal import ROUND_HALF_UP, Decimal
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("openstall.paypal")


class PayPalError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None, details: Any = None):
        super().__init__(message)
        self.status = status
        self.debug_id = debug_id
        self.details = details


def issue_of(err: "PayPalError") -> str:
    """First PayPal issue code (e.g. INSTRUMENT_DECLINED) from an error response."""
    d = err.details if isinstance(err.details, dict) else {}
    details = d.get("details") or [{}]
    return (details[0].get("issue") if isinstance(details[0], dict) else "") or d.get("issue", "") or d.get("name", "")


def money(cents: int, currency: str | None = None) -> dict[str, str]:
    return {"currency_code": currency or settings.currency, "value": str((Decimal(cents) / 100).quantize(Decimal("0.01")))}


def to_cents(value: str | float | int) -> int:
    """Exact decimal parsing of PayPal amount strings (never via float)."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


CARRIERS = {"USPS", "UPS", "FEDEX", "DHL", "DHL_API", "ONTRAC", "LASERSHIP", "AMAZON"}


@dataclass
class CreatedOrder:
    id: str
    approve_url: str
    status: str


def _parse_capture(c: dict[str, Any], unit: dict[str, Any] | None = None) -> dict[str, Any]:
    payee = (unit or {}).get("payee") or {}
    return {
        "status": c["status"],  # COMPLETED, PENDING, DECLINED...
        "capture_id": c["id"],
        "amount_cents": to_cents(c["amount"]["value"]),
        "currency": c["amount"].get("currency_code", settings.currency),
        # Which PayPal account actually received this money (proof that each seller is paid directly).
        "payee": payee.get("merchant_id") or payee.get("email_address") or "",
    }


def captures_by_reference(order: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """reference_id -> parsed capture, for every purchase unit that has one (multi-seller orders)."""
    out: dict[str, dict[str, Any]] = {}
    for pu in (order or {}).get("purchase_units") or []:
        try:
            out[pu.get("reference_id") or ""] = _parse_capture(pu["payments"]["captures"][0], pu)
        except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
            continue
    return out


def capture_from_order(order: dict[str, Any], reference_id: str | None = None) -> dict[str, Any] | None:
    """Extract {status, capture_id, amount_cents, currency} from an order payload, if captured.
    With reference_id, only that purchase unit counts (a market order pays several sellers)."""
    units = (order or {}).get("purchase_units") or []
    if reference_id and len(units) > 1:
        return captures_by_reference(order).get(reference_id)
    try:
        return _parse_capture(units[0]["payments"]["captures"][0], units[0])
    except (KeyError, IndexError, TypeError, ValueError, ArithmeticError):
        return None


def is_paypal_checkout_url(url: str) -> bool:
    """Only PayPal's buyer-facing checkout pages may be sent to a shopper, never API endpoints."""
    from urllib.parse import urlparse
    u = urlparse(url)
    return u.scheme == "https" and u.hostname in ("www.paypal.com", "www.sandbox.paypal.com") and not u.path.startswith("/v")


class PayPalClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()
        # Mock-mode state: paypal order id -> dict
        self._mock_orders: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ infra
    @property
    def live(self) -> bool:
        return settings.paypal_live

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            try:
                resp = httpx.post(
                    f"{settings.paypal_api_base}/v1/oauth2/token",
                    auth=(settings.paypal_client_id, settings.paypal_client_secret),
                    data={"grant_type": "client_credentials"},
                    headers={"Accept": "application/json"},
                    timeout=20,
                )
            except httpx.HTTPError as e:
                raise PayPalError("Network error during PayPal authentication", None) from e
            if resp.status_code != 200:
                raise PayPalError("PayPal authentication failed", resp.status_code, details=resp.text)
            body = resp.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 3000))
            return self._token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        request_id: str | None = None,
        prefer_full: bool = True,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }
        if prefer_full:
            headers["Prefer"] = "return=representation"
        if request_id:
            # Idempotency: PayPal returns the original result for a repeated id.
            headers["PayPal-Request-Id"] = request_id
        try:
            resp = httpx.request(method, f"{settings.paypal_api_base}{path}", json=json, headers=headers, timeout=30)
        except httpx.HTTPError as e:
            # Timeouts are ambiguous: PayPal may have processed the request. Callers must reconcile.
            log.warning("PayPal %s %s network error: %s", method, path, e)
            raise PayPalError(f"Network error talking to PayPal: {e.__class__.__name__}", None) from e
        debug_id = resp.headers.get("Paypal-Debug-Id")
        if resp.status_code >= 400:
            try:
                details = resp.json()
            except ValueError:
                details = resp.text
            message = details.get("message") if isinstance(details, dict) else str(details)
            log.warning("PayPal %s %s -> %s (%s) %s", method, path, resp.status_code, debug_id, details)
            raise PayPalError(message or "PayPal request failed", resp.status_code, debug_id, details)
        if not resp.content:
            return {}
        return resp.json()

    # ----------------------------------------------------------------- orders
    def create_order(
        self,
        *,
        reference_id: str,
        lines: list[dict[str, Any]],
        total_cents: int,
        description: str,
        payee_email: str | None,
        brand_name: str,
        return_url: str,
        cancel_url: str,
    ) -> CreatedOrder:
        """Create a CAPTURE order. Lines: [{name, quantity, unit_cents}]."""
        if not self.live:
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            self._mock_orders[oid] = {"status": "CREATED", "reference_id": reference_id, "total_cents": total_cents}
            return CreatedOrder(oid, f"{settings.base_url}/mock-paypal/approve?token={oid}", "CREATED")

        purchase_unit = self._purchase_unit(reference_id, lines, total_cents, description, payee_email)
        body = {
            "intent": "CAPTURE",
            "purchase_units": [purchase_unit],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": brand_name[:127],
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }
        data = self._request("POST", "/v2/checkout/orders", json=body, request_id=f"create-{reference_id}")
        approve = next(
            (l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")),
            None,
        )
        if not approve or not is_paypal_checkout_url(approve):
            raise PayPalError("PayPal did not return a valid buyer approval link", details=data)
        return CreatedOrder(data["id"], approve, data.get("status", "CREATED"))

    @staticmethod
    def _purchase_unit(reference_id: str, lines: list[dict[str, Any]], total_cents: int, description: str,
                       payee_email: str | None) -> dict[str, Any]:
        unit: dict[str, Any] = {
            "reference_id": reference_id,
            "custom_id": reference_id,
            "description": description[:127],
            "amount": {**money(total_cents), "breakdown": {"item_total": money(total_cents)}},
            "items": [
                {"name": line["name"][:127], "quantity": str(line["quantity"]),
                 "unit_amount": money(line["unit_cents"]), "category": "PHYSICAL_GOODS"}
                for line in lines
            ],
        }
        if payee_email:
            # The seller is the payee / merchant of record.
            unit["payee"] = {"email_address": payee_email}
        return unit

    def create_multi_order(
        self,
        *,
        request_key: str,
        units: list[dict[str, Any]],
        brand_name: str,
        return_url: str,
        cancel_url: str,
    ) -> CreatedOrder:
        """One buyer approval that pays several sellers: one purchase unit per seller (max 10).
        units: [{reference_id, lines, total_cents, description, payee_email}]."""
        if not 1 <= len(units) <= 10:
            raise PayPalError("A PayPal order holds 1 to 10 purchase units")
        if not self.live:
            oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
            self._mock_orders[oid] = {"status": "CREATED", "reference_id": units[0]["reference_id"],
                                      "total_cents": sum(u["total_cents"] for u in units),
                                      "units": [{"reference_id": u["reference_id"], "total_cents": u["total_cents"],
                                                 "payee_email": u.get("payee_email")} for u in units]}
            return CreatedOrder(oid, f"{settings.base_url}/mock-paypal/approve?token={oid}", "CREATED")
        body = {
            "intent": "CAPTURE",
            "purchase_units": [self._purchase_unit(u["reference_id"], u["lines"], u["total_cents"], u["description"],
                                                   u.get("payee_email")) for u in units],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": brand_name[:127], "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                "return_url": return_url, "cancel_url": cancel_url,
            }}},
        }
        data = self._request("POST", "/v2/checkout/orders", json=body, request_id=f"create-{request_key}")
        approve = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
        if not approve or not is_paypal_checkout_url(approve):
            raise PayPalError("PayPal did not return a valid buyer approval link", details=data)
        return CreatedOrder(data["id"], approve, data.get("status", "CREATED"))

    def capture_order_all(self, paypal_order_id: str) -> dict[str, dict[str, Any]]:
        """Capture every purchase unit of an approved order. Returns reference_id -> capture.
        PayPal answers 207 when only some units were captured; missing units simply aren't in the result."""
        if not self.live:
            o = self._mock_orders.get(paypal_order_id)
            if not o:
                raise PayPalError("Unknown order", 404)
            if o["status"] == "COMPLETED":
                raise PayPalError("Order already captured", 422, details={"details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
            if o["status"] != "APPROVED":
                raise PayPalError("Order not approved by payer", 422, details={"issue": "ORDER_NOT_APPROVED"})
            o["status"] = "COMPLETED"
            o["captures"] = {u["reference_id"]: "MOCKCAP-" + uuid.uuid4().hex[:10].upper()
                             for u in o.get("units") or [{"reference_id": o["reference_id"]}]}
            return captures_by_reference(self.get_order(paypal_order_id))
        data = self._request("POST", f"/v2/checkout/orders/{paypal_order_id}/capture", json={},
                             request_id=f"capture-{paypal_order_id}")
        caps = captures_by_reference(data)
        if not caps:
            raise PayPalError("Capture response had no capture", details=data)
        return caps

    def get_order(self, paypal_order_id: str) -> dict[str, Any]:
        if not self.live:
            o = self._mock_orders.get(paypal_order_id, {"status": "UNKNOWN"})
            out: dict[str, Any] = {"id": paypal_order_id, "status": o["status"]}
            if o.get("units"):
                out["purchase_units"] = [
                    {"reference_id": u["reference_id"],
                     "payee": {"email_address": u.get("payee_email") or "openstall-platform@business.example.com"},
                     **({"payments": {"captures": [{
                        "id": o["captures"][u["reference_id"]], "status": "COMPLETED", "amount": money(u["total_cents"]),
                    }]}} if o.get("captures", {}).get(u["reference_id"]) else {})}
                    for u in o["units"]
                ]
            elif o.get("capture_id"):
                out["purchase_units"] = [{"payments": {"captures": [{
                    "id": o["capture_id"], "status": "COMPLETED",
                    "amount": money(o["total_cents"]),
                }]}}]
            return out
        return self._request("GET", f"/v2/checkout/orders/{paypal_order_id}", prefer_full=False)

    def mock_approve(self, paypal_order_id: str) -> None:
        if paypal_order_id in self._mock_orders:
            self._mock_orders[paypal_order_id]["status"] = "APPROVED"

    def capture_order(self, paypal_order_id: str) -> dict[str, Any]:
        """Capture an approved order. Returns {status, capture_id, amount_cents}."""
        if not self.live:
            o = self._mock_orders.get(paypal_order_id)
            if not o:
                raise PayPalError("Unknown order", 404)
            if o["status"] == "COMPLETED":
                raise PayPalError("Order already captured", 422, details={"details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
            if o["status"] != "APPROVED":
                raise PayPalError("Order not approved by payer", 422, details={"issue": "ORDER_NOT_APPROVED"})
            o["status"] = "COMPLETED"
            o["capture_id"] = "MOCKCAP-" + uuid.uuid4().hex[:10].upper()
            return {"status": "COMPLETED", "capture_id": o["capture_id"], "amount_cents": o["total_cents"],
                    "currency": settings.currency}

        data = self._request(
            "POST",
            f"/v2/checkout/orders/{paypal_order_id}/capture",
            json={},
            request_id=f"capture-{paypal_order_id}",
        )
        parsed = capture_from_order(data)
        if not parsed:
            raise PayPalError("Capture response had no capture", details=data)
        return parsed

    def refund_capture(self, capture_id: str, *, amount_cents: int | None = None, note: str = "") -> dict[str, Any]:
        if not self.live:
            return {"id": "MOCKREF-" + uuid.uuid4().hex[:10].upper(), "status": "COMPLETED"}
        body: dict[str, Any] = {"note_to_payer": note[:255]} if note else {}
        if amount_cents is not None:
            body["amount"] = money(amount_cents)
        data = self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json=body,
            request_id=f"refund-{capture_id}-{amount_cents or 'full'}",
        )
        return {"id": data.get("id"), "status": data.get("status")}

    def add_tracking(self, paypal_order_id: str, capture_id: str, tracking_number: str, carrier: str) -> dict[str, Any]:
        if not self.live:
            return {"id": "MOCKTRK-" + uuid.uuid4().hex[:8].upper(), "status": "SHIPPED"}
        code = carrier.strip().upper().replace(" ", "_")
        body: dict[str, Any] = {
            "capture_id": capture_id,
            "tracking_number": tracking_number,
            "carrier": code if code in CARRIERS else "OTHER",
            "notify_payer": True,
        }
        if body["carrier"] == "OTHER":
            body["carrier_name_other"] = carrier[:64]
        data = self._request("POST", f"/v2/checkout/orders/{paypal_order_id}/track", json=body)
        return {"id": data.get("id", paypal_order_id), "status": "SHIPPED"}

    # --------------------------------------------------------------- invoices
    def create_and_send_invoice(
        self,
        *,
        reference: str,
        seller_name: str,
        seller_email: str | None,
        buyer_email: str,
        description: str,
        amount_cents: int,
        deposit_cents: int,
        due_date: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Draft + send an invoice that allows a partial (deposit) payment."""
        if not self.live:
            iid = "MOCKINV-" + uuid.uuid4().hex[:10].upper()
            return {"id": iid, "status": "SENT", "pay_url": f"{settings.base_url}/mock-paypal/invoice?id={iid}"}

        # The invoice is issued by the API caller's account (the platform); invoicer.email_address
        # must match that account, so we only set the display business name.
        invoicer: dict[str, Any] = {"business_name": seller_name[:100]}
        body = {
            "detail": {
                "invoice_number": reference[:25],
                "currency_code": settings.currency,
                "note": note[:4000],
                "payment_term": {"due_date": due_date},
            },
            "invoicer": invoicer,
            "primary_recipients": [{"billing_info": {"email_address": buyer_email}}],
            "items": [
                {
                    "name": description[:200],
                    "quantity": "1",
                    "unit_amount": money(amount_cents),
                    "unit_of_measure": "QUANTITY",
                }
            ],
            "configuration": {
                "allow_tip": False,
                "partial_payment": {
                    "allow_partial_payment": deposit_cents < amount_cents,
                    "minimum_amount_due": money(deposit_cents),
                },
            },
        }
        draft = self._request("POST", "/v2/invoicing/invoices", json=body, request_id=f"inv-{reference}")
        invoice_id = draft.get("id") or draft.get("href", "").rstrip("/").split("/")[-1]
        sent = self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/send",
            json={"send_to_invoicer": False, "send_to_recipient": True},
            prefer_full=False,
        )
        pay_url = sent.get("href")
        if not pay_url:
            detail = self._request("GET", f"/v2/invoicing/invoices/{invoice_id}", prefer_full=False)
            pay_url = detail.get("detail", {}).get("metadata", {}).get("recipient_view_url")
        return {"id": invoice_id, "status": "SENT", "pay_url": pay_url}

    # --------------------------------------------------------------- webhooks
    def verify_webhook(self, headers: dict[str, str], event: dict[str, Any]) -> bool:
        if not self.live or not settings.paypal_webhook_id:
            # Without a configured webhook id we cannot verify; only accept in mock mode.
            return not self.live
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": settings.paypal_webhook_id,
            "webhook_event": event,
        }
        try:
            data = self._request("POST", "/v1/notifications/verify-webhook-signature", json=body, prefer_full=False)
        except PayPalError:
            return False
        return data.get("verification_status") == "SUCCESS"


paypal = PayPalClient()
