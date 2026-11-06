# OpenStall

**Agent-ready shops for sellers without a website.** Snap a photo of your menu and AI shopping agents can find you, order, and pay with PayPal.

**Live demo: https://openstall-production.up.railway.app** · MCP endpoint: `https://openstall-production.up.railway.app/agent/mcp`

Built for the [PayPal AI Hackathon 2026](https://paypalaihackathon.devpost.com/). PayPal sandbox only; no real money moves.

## The problem

AI-referred retail traffic grew 393% year over year in Q1 2026 (Adobe). Shopify and Square have made their merchants visible to AI agents, and PayPal Store Sync reaches merchants on platforms like Wix, BigCommerce and Shopware. But home bakers, market stalls and makers who sell through DMs and a PayPal link have **no platform, no catalog and no API**, so AI agents cannot see them.

## What OpenStall does

1. **Snap.** The seller uploads a menu photo (or types it). Claude extracts a structured catalog: items, prices, allergens, daily capacity, pickup windows, lead time. The seller reviews it.
2. **List.** The stall is published to the OpenStall **MCP server**, plus a public page with schema.org JSON-LD and `/llms.txt`.
3. **Get paid.** Any agent can `find_stalls`, `get_menu`, `check_availability` and `place_order`. Each order is a **PayPal Orders v2** checkout. The human approves in PayPal; the agent never touches credentials.
4. **Market runs.** A buyer (or their agent, via `place_market_order`) fills one basket from up to 5 stalls for the same pickup day and approves **one PayPal payment**. It is one Orders v2 order with one purchase unit per seller, each with its own `payee`, so every seller is paid into their own PayPal account (the payee PayPal returns on each capture is shown on the order) and keeps its own order, capacity, pickup window and refunds. Holds are all or nothing; a stall that sells out before capture is refunded alone while the rest stay paid. If PayPal refuses a multi-seller order, OpenStall falls back to one checkout per stall.
5. **Learn.** Seller **Insights**, built on **AG Studio**, shows how much revenue AI agents bring, the agent funnel (viewed menu, checked availability, started PayPal checkout, paid), a pickup board for the next 7 days, and a **demand radar**: what agents asked for that the seller could not sell (sold out, no pickup that day, too little notice). A Claude-powered Studio assistant answers plain-language questions, builds charts, and can raise an item's daily capacity after the seller confirms.
6. **Manage orders on AG Grid.** The seller dashboard is a live **AG Grid Enterprise** grid (grouping, totals, set filters, CSV export, inline Mark ready / Picked up / Tracking / Refund). An ask box uses the **AG Grid AI Toolkit**: the grid's `getStructuredSchema()` and `getState()` go to Claude, which returns a grid state applied with `setState()`. Claude can change the view, never press Refund.
7. **Markets.** An organizer groups stalls into a market (`/markets/new`) and gets a public page with a QR code (`/m/<id>`), an MCP listing (`find_markets`, `get_market`), a market-wide orders grid and Market Insights on AG Studio. Organizers see every order but cannot fulfil or refund; only each seller's key can.

## How PayPal is used

| PayPal capability | Where |
|---|---|
| OAuth 2.0 client credentials (cached) | `app/paypal.py` |
| **Orders v2** create (seller as payee) and capture, idempotent `PayPal-Request-Id` | `place_order`, `complete_payment` |
| **Payments v2** refunds (amount/currency mismatch, closed orders, seller refunds) | `service.py` |
| **Webhooks** with signature verification: `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED/PENDING/DENIED/REFUNDED/REVERSED`, `CUSTOMER.DISPUTE.*` | `/webhooks/paypal` |
| **Invoicing v2** with partial payment (deposit) for custom orders, priced by the seller | `send_custom_quote` |
| **Orders v2 multiple purchase units**: one buyer approval, one capture, a separate capture per seller (handles HTTP 207 partial capture) | `app/market.py` |
| **Order tracking** (`/v2/checkout/orders/{id}/track`) for shipped goods | seller dashboard |

## How AI is used

- **Claude (Anthropic API)**: vision menu-to-catalog extraction with forced tool output and a strict JSON schema (`app/ai.py`), and a demo buyer agent that shops through exactly the same tools external agents get (`app/buyer_agent.py`).
- **Model Context Protocol**: official MCP Python SDK, streamable HTTP at `/agent/mcp` (`app/mcp_server.py`).
- **AG Studio AI**: the Studio agent harness runs on Claude through a small server bridge (`/api/studio/llm`, `app/insights.py`) that translates Studio turns into Anthropic Messages with tool use. OpenStall adds its own Studio tools: `get_unmet_demand`, `get_stall_rules`, `get_pickup_board` and `set_daily_capacity` (the seller confirms every change), plus two custom widgets (Pickup board, Demand radar).

## Seller Insights (AG Studio)

- Open it from the seller dashboard ("Open Insights"). It is behind the same private owner key.
- Data sources: `orders` (one row per paid order line, channel `AI agent` or `Web`), `demand` (missed agent demand), `agent_funnel`.
- Demo stalls ship with clearly labelled `Sample history` rows so the dashboard is not empty; real orders are labelled `Live` and can be filtered.
- The front end lives in `studio/` and is bundled with esbuild into `app/static/studio/insights.js`, which is committed, so the Python deploy needs no Node. To rebuild: `cd studio && npm install && npm run build`.
- `AG_STUDIO_LICENSE_KEY` removes the trial watermark. It is read from the environment, never committed.

## Payment safety design (the part I am proudest of)

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

121 tests (`tests/`) cover overselling, concurrent orders, lead time, price tampering, timeouts, declines, pending captures, refunds failing, webhook replays, partial refunds, disputes, XSS, rate limits and more.

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

## Deploy on Railway (recommended)

1. New project, Deploy from GitHub repo, pick this repo. `railway.json` sets the start command and health check; `.python-version` pins Python 3.13.
2. Add a **Volume** to the service mounted at `/data`, and set `DATABASE_PATH=/data/openstall.db` so stalls and orders survive deploys.
3. Settings, Networking, **Generate Domain**. The app reads `RAILWAY_PUBLIC_DOMAIN` automatically (or set `BASE_URL`).
4. Add the same variables as `.env.example`. Point a PayPal sandbox webhook at `https://<your-domain>/webhooks/paypal` and set `PAYPAL_WEBHOOK_ID`.

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
  insights.py      Insights data, demand radar, sample history, Claude bridges for AG Studio and the AG Grid AI Toolkit
  markets.py       farmers markets: organizer key, stalls, market-wide orders and insights
studio/            AG Studio and AG Grid front end source (built into app/static/studio/insights.js and orders.js)
seed/              demo stalls
scripts/           sandbox smoke test
tests/             scenario tests
```

## License

MIT
