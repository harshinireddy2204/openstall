"""Live check against the REAL PayPal sandbox and Anthropic API, using your .env.

Usage:  python scripts/sandbox_smoke.py
It never touches real money (sandbox only) and never prints secrets.
"""

import sys
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
    reference_id="SMOKE-TEST", lines=[{"name": "Smoke test bun", "quantity": 1, "unit_cents": 100}], total_cents=100,
    description="OpenStall sandbox smoke test", payee_email=None, brand_name="OpenStall",
    return_url=f"{settings.base_url}/healthz", cancel_url=f"{settings.base_url}/healthz"))
if order:
    print(f"       Open this link, log in with a SANDBOX PERSONAL account, approve:\n       {order.approve_url}")
    input("       Press Enter after approving...")
    cap = step("Capture", lambda: paypal.capture_order(order.id))
    if cap:
        step("Refund", lambda: paypal.refund_capture(cap["capture_id"], note="Smoke test refund"))

if settings.ai_live:
    from app.ai import extract_catalog
    img = (Path(__file__).resolve().parent.parent / "app/static/samples/chalkboard_menu.png").read_bytes()
    step("Claude reads sample chalkboard", lambda: [
        (i["name"], i["price"]) for i in extract_catalog(image=img, media_type="image/png")["items"]])
else:
    print("[SKIP] Claude: no ANTHROPIC_API_KEY")
