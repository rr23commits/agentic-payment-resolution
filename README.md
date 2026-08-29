# Agentic Checkout with Authoritative Payment Resolution

Phase 11 Razorpay Test Mode checkout prototype with PostgreSQL-backed catalogue, mandate validation, idempotent checkout, browser correlation, verified resolution, agent loop, and customer/operator views.

## Prerequisites

- Python 3.11+
- Docker Compose
- `uv`

## Local setup

```sh
cp .env.example .env
docker compose up -d postgres
set -a; source .env; set +a
uv run python -m backend.migrate
uv run python -m backend.seed
uv run python -m unittest discover -s tests
uv run python -m backend.main
```

The schema uses integer paise (`BIGINT`) for every amount. It enforces checkout idempotency, one unresolved attempt per intent, webhook deduplication, ordered audit sequences, and append-only audit records at the database boundary. `make dev` runs the migration and idempotent demo seed automatically.

The seed creates `customer_demo` with a demo book and valid, cap-failing, category-failing, and expired mandates. After creating a checkout, the customer page has a labelled demo timeout/debit action. To replay a signed webhook through the real HTTP boundary, run `uv run python -m backend.demo --order-id ORDER_ID --delay 5`.

## Phase 2 tools

`backend.catalogue` provides `search_catalogue`, `get_product_details`, `create_cart`, `get_mandate`, and `validate_purchase`. `create_cart` requires the authenticated caller's `customer_id` as keyword context; it ignores supplied prices and calculates the total from PostgreSQL. `validate_purchase` reloads the cart's products and records every allow/deny decision in the append-only audit table.

## Phase 3 checkout

`backend.checkout.start_checkout` requires the caller's `customer_id`, revalidates the cart and mandate in its transaction, and uses the `(customer_id, client_request_id)` key to return the original Razorpay Test Mode order on replay. It permits a replacement order for the cart only after the prior attempt is verified `FAILED`.

## Phase 4 browser boundary

The local server exposes `POST /checkout/client-report` for the Razorpay browser callback. A matching client payment reference is recorded as `CLIENT_REPORTED`; it never marks an attempt captured. The supplied browser code immediately displays “Payment is being confirmed” until a later authoritative webhook or reconciliation result resolves the attempt.

## Phase 5 verified webhooks

`POST /webhooks/razorpay` verifies `X-Razorpay-Signature` against the unmodified request bytes using `RAZORPAY_WEBHOOK_SECRET`. It deduplicates `X-Razorpay-Event-Id`, keeps raw provider data server-side, records `WEBHOOK_RECEIVED`, and passes only verified evidence to the resolver.

## Phase 6 resolution

`backend.resolver.resolve_attempt` is the only payment-state mutation boundary. Verified Razorpay captured/failed/reversed/refunded evidence resolves the original attempt; invalid or contradictory evidence preserves the existing state and creates an audit exception. `reconcile_status` reads Razorpay's existing order-payment records and never retries checkout; it is operator-only in this local demo.

## Phase 7 agent tools

`agent.tools.tools_for(customer_id)` provides only catalogue, cart, mandate, checkout, payment-status, and audit-timeline operations. Its checkout description explicitly states that unresolved `PENDING`/`AMBIGUOUS` attempts cannot create another payment; rejected checkout results are terminal for the request. It provides no retry, database, webhook, credential, reconciliation, or payment-state tool.

## Phase 8 model loop

`agent.loop.run_agent` accepts a caller-supplied model function. The customer route uses the standard-library Gemini adapter first (`GEMINI_API_KEY`, optional `GEMINI_MODEL`) and falls back to OpenRouter when Gemini is unavailable or its quota is exhausted (`OPENROUTER_API_KEY`, optional `OPENROUTER_MODEL`). The model selects the next listed tool from request context and prior results; after an unresolved checkout, the loop exposes only safe observation tools. No model SDK or additional payment authority is included.

## Phase 9 views

Run `python -m backend.main`, then open `/` for the customer chat and Test Mode checkout flow, or `/operator` for the operator timeline. Set `OPERATOR_VIEW_TOKEN` to use the operator view's reconcile action. Customer chat state is process-local/in-memory and resets when the server restarts. The customer view is local Test Mode demo functionality keyed by opaque intent ID; production authentication is still required.

## Phase 10 verification

Run `uv run python -m unittest discover -s tests` with `DATABASE_URL` set. The suite covers the financial safety boundary, model adapters, customer/operator views, and demo timeout/reconciliation paths; Razorpay provider calls are mocked during verification.

Never commit `.env`; it contains Test Mode credentials and local database secrets.

## Phase 11 pitch check

The full test suite includes the pitch acceptance flow: one agent-created `PENDING` checkout, an attempted second charge blocked by the loop, then a signed Razorpay webhook resolving that same attempt with its customer-safe audit trail visible. The test uses provider mocks; `backend.demo` is the scripted HTTP-boundary replay.
