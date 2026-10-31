"""Live check against the REAL PayPal sandbox and Anthropic API, using your .env.

Usage:  python scripts/sandbox_smoke.py
It never touches real money (sandbox only) and never prints secrets.
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.paypal import PayPalError, paypal  # noqa: E402


def step(name, fn):
    try:
        out = fn()
        print(f"[PASS] {name}: {out}")
        return out
    except Exception as e:  # noqa: BLE001
        extra = f" (debug_id={e.debug_id}, details={e.details})" if isinstance(e, PayPalError) else ""
        print(f"[FAIL] {name}: {e}{extra}")
        return None


if not settings.paypal_live:
    sys.exit("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env first.")
if "sandbox" not in settings.paypal_api_base:
    sys.exit("Refusing to run: PAYPAL_API_BASE is not the sandbox.")

step("OAuth token", lambda: "ok" if paypal._access_token() else "no token")
order = step("Create order ($1.00)", lambda: paypal.create_order(
    reference_id=f"SMOKE-{uuid.uuid4().hex[:10].upper()}", lines=[{"name": "Smoke test bun", "quantity": 1, "unit_cents": 100}], total_cents=100,
    description="OpenStall sandbox smoke test", payee_email=None, brand_name="OpenStall",
    return_url=f"{settings.base_url}/healthz", cancel_url=f"{settings.base_url}/healthz"))
if order:
    print(f"       1. Open this link in a private/incognito window:\n          {order.approve_url}")
    print("       2. Log in with your SANDBOX PERSONAL (buyer) account, not your real PayPal and not the sandbox Business one.")
    print("       3. Click the final Pay / Complete Purchase button.")
    print("       4. You'll be sent to a page that may not load (localhost isn't running). That's expected: approval is done.")
    status = ""
    for attempt in range(5):
        input("       Press Enter once you've clicked Pay...")
        status = paypal.get_order(order.id).get("status", "")
        if status == "APPROVED":
            print("[PASS] Buyer approval: APPROVED")
            break
        print(f"       PayPal still shows status {status!r}, not APPROVED. Finish the Pay step in the browser, then press Enter again.")
    cap = step("Capture", lambda: paypal.capture_order(order.id)) if status == "APPROVED" else None
    if cap:
        step("Refund", lambda: paypal.refund_capture(cap["capture_id"], note="Smoke test refund"))

if settings.ai_live:
    from app.ai import EXTRACTOR_VERSION, extract_catalog
    print(f"       (menu reader version {EXTRACTOR_VERSION}, model {settings.anthropic_model})")
    img = (Path(__file__).resolve().parent.parent / "app/static/samples/chalkboard_menu.png").read_bytes()
    step("Claude reads sample chalkboard", lambda: [
        (i["name"], i["price"]) for i in extract_catalog(image=img, media_type="image/png")["items"]])
else:
    print("[SKIP] Claude: no ANTHROPIC_API_KEY")
