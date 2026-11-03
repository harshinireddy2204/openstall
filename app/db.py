"""SQLite persistence. Small, dependency-free, good enough for a single-node demo."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS stalls (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT '',
    city TEXT DEFAULT '',
    lat REAL,
    lng REAL,
    pickup_address TEXT DEFAULT '',
    paypal_email TEXT DEFAULT '',
    timezone TEXT DEFAULT 'America/New_York',
    lead_time_hours INTEGER DEFAULT 24,
    pickup_windows TEXT DEFAULT '[]',
    ships INTEGER DEFAULT 0,
    owner_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_id TEXT NOT NULL REFERENCES stalls(id),
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    price_cents INTEGER NOT NULL,
    unit TEXT DEFAULT 'each',
    allergens TEXT DEFAULT '[]',
    tags TEXT DEFAULT '[]',
    daily_capacity INTEGER,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    stall_id TEXT NOT NULL REFERENCES stalls(id),
    buyer_name TEXT DEFAULT '',
    buyer_note TEXT DEFAULT '',
    pickup_date TEXT NOT NULL,
    pickup_window TEXT DEFAULT '',
    lines TEXT NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    channel TEXT DEFAULT 'web',
    paypal_order_id TEXT,
    paypal_capture_id TEXT,
    approve_url TEXT,
    tracking TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_stall_date ON orders(stall_id, pickup_date);
CREATE INDEX IF NOT EXISTS idx_orders_paypal ON orders(paypal_order_id);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    stall_id TEXT NOT NULL REFERENCES stalls(id),
    paypal_invoice_id TEXT,
    buyer_email TEXT DEFAULT '',
    description TEXT DEFAULT '',
    amount_cents INTEGER NOT NULL,
    deposit_cents INTEGER NOT NULL,
    due_date TEXT DEFAULT '',
    status TEXT NOT NULL,
    pay_url TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
    id TEXT PRIMARY KEY,
    event_type TEXT,
    outcome TEXT,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baskets (
    id TEXT PRIMARY KEY,
    pickup_date TEXT NOT NULL,
    mode TEXT NOT NULL,              -- 'single': one PayPal approval pays every seller; 'split': one checkout per seller
    paypal_order_id TEXT,
    approve_url TEXT,
    channel TEXT DEFAULT 'web',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stall_id TEXT,
    order_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

JSON_COLUMNS = {"pickup_windows", "allergens", "tags", "lines", "tracking", "detail"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    # isolation_level=None: autocommit, transactions are opened explicitly in tx().
    conn = sqlite3.connect(settings.database_path, check_same_thread=False, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(c: sqlite3.Connection) -> None:
    """Additive column migrations for databases created by older versions."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(orders)")}
    if "basket_id" not in cols:
        c.execute("ALTER TABLE orders ADD COLUMN basket_id TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_basket ON orders(basket_id)")


def reset_connection() -> None:
    """Used by tests to point at a fresh database."""
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Serialized transaction. Capacity checks + inserts must not interleave."""
    with _lock:
        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        if isinstance(out[key], str) and out[key]:
            out[key] = json.loads(out[key])
    return out


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        return [row_to_dict(r) for r in conn().execute(sql, params).fetchall()]  # type: ignore[misc]


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with _lock:
        return row_to_dict(conn().execute(sql, params).fetchone())


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def log_event(kind: str, *, stall_id: str | None = None, order_id: str | None = None, **detail: Any) -> None:
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO events (stall_id, order_id, kind, detail, created_at) VALUES (?,?,?,?,?)",
            (stall_id, order_id, kind, dumps(detail), now_iso()),
        )
        c.commit()
