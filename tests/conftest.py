import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

# Force an isolated DB and offline (mock) mode BEFORE the app is imported.
_TMP = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_PATH"] = str(_TMP)
for var in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "ANTHROPIC_API_KEY", "DEMO_SELLER_PAYPAL_EMAIL"):
    os.environ[var] = ""
os.environ["BASE_URL"] = "http://testserver"

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import _hits, app, seed_demo_data  # noqa: E402
from app.paypal import paypal  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_connection()
    if _TMP.exists():
        _TMP.unlink()
    paypal._mock_orders.clear()
    _hits.clear()
    seed_demo_data()
    yield


@pytest.fixture(scope="session")
def _client_session():
    # The MCP session manager can only be started once per process, so share one client.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(_client_session):
    return _client_session


def next_weekday(name: str, min_days_ahead: int = 3) -> str:
    """Next given weekday at least N days out, so lead-time rules never interfere."""
    target = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(name)
    d = date.today() + timedelta(days=min_days_ahead)
    while d.weekday() != target:
        d += timedelta(days=1)
    return d.isoformat()


def item_id(stall_id: str, name_contains: str) -> int:
    row = db.query_one("SELECT id FROM items WHERE stall_id=? AND name LIKE ?", (stall_id, f"%{name_contains}%"))
    assert row, name_contains
    return row["id"]


def approve_and_return(client, order: dict) -> dict:
    """Simulate the human approving in PayPal and being sent back to OpenStall."""
    token = order["paypal_approval_url"].split("token=")[1]
    r = client.post("/mock-paypal/approve", data={"token": token, "decision": "approve"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get(r.headers["location"], follow_redirects=False)
    assert r.status_code == 303
    return client.get(f"/api/orders/{order['order_id']}").json()
