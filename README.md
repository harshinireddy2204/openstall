# OpenStall

**Agent-ready shops for sellers without a website.** Snap a photo of your menu and AI shopping agents can find you, order, and pay with PayPal.

**Live demo: https://openstall.onrender.com** (free hosting: the first load may take up to a minute)

Built for the [PayPal AI Hackathon 2026](https://paypalaihackathon.devpost.com/). PayPal sandbox only; no real money moves.

## The problem

AI-referred retail traffic grew 393% year over year in Q1 2026 (Adobe). Shopify and Square have made their merchants visible to AI agents, and PayPal Store Sync reaches merchants on platforms like Wix, BigCommerce and Shopware. But home bakers, market stalls and makers who sell through DMs and a PayPal link have **no platform, no catalog and no API**, so AI agents cannot see them.

## What OpenStall does

1. **Snap.** The seller uploads a menu photo (or types it). Claude extracts a structured catalog: items, prices, allergens, daily capacity, pickup windows, lead time. The seller reviews it.
2. **List.** The stall is published to the OpenStall **MCP server**, plus a public page with schema.org JSON-LD and `/llms.txt`.
3. **Get paid.** Any agent can `find_stalls`, `get_menu`, `check_availability` and `place_order`. Each order is a **PayPal Orders v2** checkout. The human approves in PayPal; the agent never touches credentials.

## How PayPal is used

| PayPal capability | Where |
|---|---|
| OAuth 2.0 client credentials (cached) | `app/paypal.py` |
| **Orders v2** create (seller as payee) and capture, idempotent `PayPal-Request-Id` | `place_order`, `complete_payment` |
| **Payments v2** refunds (amount/currency mismatch, closed orders, seller refunds) | `service.py` |
| **Webhooks** with signature verification: `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED/PENDING/DENIED/REFUNDED/REVERSED`, `CUSTOMER.DISPUTE.*` | `/webhooks/paypal` |
| **Invoicing v2** with partial payment (deposit) for custom orders, priced by the seller | `send_custom_quote` |
| **Order tracking** (`/v2/checkout/orders/{id}/track`) for shipped goods | seller dashboard |

## How AI is used

- **Claude (Anthropic API)**: vision menu-to-catalog extraction with forced tool output and a strict JSON schema (`app/ai.py`), and a demo buyer agent that shops through exactly the same tools external agents get (`app/buyer_agent.py`).
- **Model Context Protocol**: official MCP Python SDK, streamable HTTP at `/agent/mcp` (`app/mcp_server.py`).

## Payment safety design (the part we are proudest of)

The model proposes, deterministic code decides, PayPal settles.

- **Prices are never taken from an agent.** Lines are re-priced from the catalog server-side.
- **Stock is checked before money moves.** On return from PayPal, capacity is re-checked and reserved (`capturing`) in one lock, then captured. A sold-out order is never charged, so there is nothing to refund.
- **Ambiguous captures are reconciled, not guessed.** Timeouts and 5xx responses trigger a `GET` on the PayPal order; capture retries reuse the same idempotency key. Orders stuck mid-capture are reconciled automatically.
- **PENDING captures are not "paid".** They become `payment_review` (stock held, goods not released) until `PAYMENT.CAPTURE.COMPLETED` arrives.
- **Every capture is verified** for amount and currency; mismatches are refunded automatically. A failed refund is escalated (`needs_attention`) on the seller dashboard, never swallowed.
- **Webhooks are verified and deduplicated** with a unique key; the approved-order webhook captures even if the buyer closed the tab.
- **Agents can only ask for custom orders.** The seller sets the price before any PayPal invoice is sent, so OpenStall cannot be used to spam people with invoices.
- **Abuse limits.** Per-IP rate limits, a cap on unpaid holds per stall per day, holds that expire, order total caps.
- **Prompt-injection aware.** Seller-written text is marked as untrusted data in both the MCP instructions and the buyer agent prompt, and the server enforces every rule regardless of what an agent says.

`tests/test_scenarios.py` covers 76 scenarios: overselling, concurrent orders, lead time, price tampering, timeouts, declines, pending captures, refunds failing, webhook replays, partial refunds, disputes, XSS, rate limits and more.

## Run locally

Requires Python 3.12+.

```bash
git clone https://github.com/<you>/openstall.git
cd openstall
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
uvicorn app.main:app --reload
```

Open http://localhost:8000. With no keys the app runs in **mock mode** (simulated PayPal approval, text-only menu parsing), so everything is testable offline.

Run the tests: `python -m pytest -q`

### Use the real PayPal sandbox

1. At [developer.paypal.com](https://developer.paypal.com/) open **Apps & Credentials (Sandbox)**, create an app, copy the client ID and secret into `.env`.
2. Under **Testing Tools > Sandbox Accounts**, note a **Personal** (buyer) account login. Optionally put a **Business** account email in `DEMO_SELLER_PAYPAL_EMAIL` to receive payments for the demo stalls.
3. Add a webhook for `https://<your-host>/webhooks/paypal` with the events listed above and copy its ID into `PAYPAL_WEBHOOK_ID`. (Webhooks need a public URL, so use the Render deployment or a tunnel.)
4. Run `python scripts/sandbox_smoke.py` to verify OAuth, create, capture and refund against the sandbox.

### Connect an AI assistant

Add `https://<your-host>/agent/mcp` as a remote MCP server (streamable HTTP) in Claude or any MCP client, then ask: *"Find me a dozen vegan tamales near Lowell for Saturday pickup."*

## Deploy on Render

`render.yaml` is included. Create a Blueprint from the repo, then set `BASE_URL` (your Render URL), the PayPal sandbox keys, `PAYPAL_WEBHOOK_ID`, `ANTHROPIC_API_KEY` and optionally `DEMO_OWNER_KEY`.

Note: the free plan uses an ephemeral disk, so SQLite resets on redeploy and the demo stalls are re-seeded.

## Known limitations (honest list)

- **Seller PayPal accounts are not verified.** A seller types their PayPal email. Production needs PayPal's partner onboarding (Partner Referrals API) so the platform can confirm account ownership and consent.
- **Invoices are issued by the platform account** (PayPal requires the invoicer to be the API caller); production would invoice on the seller's behalf through partner permissions.
- Single-instance SQLite and in-memory rate limits; production would use Postgres and a shared rate limiter.
- No sales tax calculation; sellers are responsible for their own tax obligations.
- The seller dashboard uses a private link (magic-link style) instead of accounts.

## Project structure

```
app/
  main.py          FastAPI routes, webhooks, rate limits, MCP mount
  service.py       business logic and payment state machine
  rules.py         deterministic seller rules (capacity, lead time, pricing)
  paypal.py        PayPal REST client (sandbox) with mock mode
  ai.py            Claude menu extraction
  tools.py         agent tools shared by MCP and the buyer agent
  mcp_server.py    MCP server
  buyer_agent.py   demo Claude shopping agent
seed/              demo stalls
scripts/           sandbox smoke test
tests/             scenario tests
```

## License

MIT
