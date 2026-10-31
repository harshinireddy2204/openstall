"""Runtime configuration, loaded from environment variables (.env supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional dependency: load a local .env when present
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Public base URL of this deployment, used for PayPal return URLs and MCP docs.
    # BASE_URL wins; on Render, RENDER_EXTERNAL_URL is set automatically (e.g. https://openstall.onrender.com).
    base_url: str = field(default_factory=lambda: (
        _env("BASE_URL") or _env("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/"))

    # PayPal (sandbox only for the hackathon)
    paypal_client_id: str = field(default_factory=lambda: _env("PAYPAL_CLIENT_ID"))
    paypal_client_secret: str = field(default_factory=lambda: _env("PAYPAL_CLIENT_SECRET"))
    paypal_api_base: str = field(default_factory=lambda: _env("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com"))
    paypal_webhook_id: str = field(default_factory=lambda: _env("PAYPAL_WEBHOOK_ID"))
    currency: str = field(default_factory=lambda: _env("CURRENCY", "USD"))

    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-5-5"))

    # Storage
    database_path: str = field(default_factory=lambda: _env("DATABASE_PATH", str(ROOT / "openstall.db")))

    # Business rules
    hold_minutes: int = field(default_factory=lambda: int(_env("HOLD_MINUTES", "30")))
    max_items_per_order: int = field(default_factory=lambda: int(_env("MAX_ITEMS_PER_ORDER", "200")))
    max_order_cents: int = field(default_factory=lambda: int(_env("MAX_ORDER_CENTS", "100000")))
    default_timezone: str = field(default_factory=lambda: _env("DEFAULT_TIMEZONE", "America/New_York"))

    @property
    def paypal_live(self) -> bool:
        """True when real PayPal sandbox credentials are configured."""
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def ai_live(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/agent/mcp"


settings = Settings()
