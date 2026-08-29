# Agentic Checkout with Authoritative Payment Resolution

## Decision

Build the Razorpay submission around an AI shopper with a signed spending mandate, but make the existing `payment-exception-resolution` repository the reference design for its **state machine, exception classification, policy boundary, and audit timeline**.

Do **not** present that repository as a live payment-resolution system. Its lifecycle events, later outcomes, and retry are controlled simulations. The new project adds the small live Razorpay boundary required to make the central claim true: receive verified provider events, reconcile authoritative status, and never retry an unresolved attempt.

## Product promise

When a customer asks the agent to buy an eligible item, the agent can create one checkout attempt. If the account is debited but the merchant does not yet have a final result, it says:

> “Your payment is still being confirmed. I will not start another payment while this one is unresolved.”

It confirms an order only after an authoritative terminal result. It records every decision in an audit timeline.

## Scope

### In scope

- Small product catalogue and natural-language shopping request.
- Signed mandate: merchant, allowed categories, spending cap, expiry, and agent ID.
- Razorpay Test Mode order/checkout creation.
- Verified Razorpay webhook ingestion and provider status reconciliation.
- Per-attempt state machine for `PENDING`, `AMBIGUOUS`, `CAPTURED`, `FAILED`, and `REVERSED`/`REFUNDED` where supported by the available provider evidence.
- Durable idempotency and audit records.
- Minimal customer status page and operator timeline.
- A deterministic demo harness that delays a local webhook-processing step; it must be visibly labelled as a demo harness, not a bank/payment simulation.

### Deliberately out of scope

- Real-money payments; use Razorpay Test Mode only.
- Autonomous retry of an unresolved payment.
- Refund/capture automation beyond what is needed for the demo.
- ML-based resolution decisions. The existing repository's model is not needed for a 2–3 week build.
- Copying its controlled generated-data evaluator into the product.

## Architecture

```text
Chat UI
  -> deterministic mandate + catalogue checks
  -> Checkout service (creates one Razorpay order with an idempotency key)
  -> PaymentAttempt store + append-only AuditEvent store
  -> Razorpay Checkout

Razorpay webhook / status lookup
  -> signature verification + webhook-event deduplication
  -> authoritative status mapper
  -> PaymentAttempt state transition
  -> audit event + customer/operator status update
```

The agent may choose products, but it never decides financial state. The payment resolver does that from verified provider evidence only.

## Agent tool contract

The LLM gets only these allowlisted server tools. It never receives Razorpay credentials, direct database access, a raw webhook payload, or a generic `retry_payment()` tool.

| Tool | Purpose | Safety boundary |
|---|---|---|
| `search_catalogue(query, category?)` | Find purchasable products. | Returns public product summaries only. |
| `get_product_details(product_id)` | Get price, stock, category, and restricted flag. | Server is the catalogue authority. |
| `create_cart(items)` | Validate quantities and calculate the server-side total. | Ignores any model-supplied price or total. |
| `get_mandate()` | Read the current customer's mandate constraints. | Exposes the rules; cannot change them. |
| `validate_purchase(cart_id, mandate_id)` | Check cap, expiry, merchant, stock, and category restrictions. | Deterministic gate; returns reasons and writes an audit event. |
| `start_checkout(cart_id, mandate_id, client_request_id)` | Create or return the one permitted checkout intent/order. | Runs `validate_purchase` again, is idempotent, and refuses an active unresolved attempt. |
| `get_payment_status(intent_id)` | Read the current resolver status and safe customer wording. | Read-only; does not treat browser data as final. |
| `get_audit_timeline(intent_id)` | Show the decision trail. | Read-only, with customer/operator fields filtered by viewer role. |

The intended agent sequence is:

`search_catalogue → get_product_details → create_cart → get_mandate → validate_purchase → start_checkout → get_payment_status`

`start_checkout` returns either a checkout URL/order reference, an existing idempotent intent, or a refusal explaining the failed mandate or unresolved prior attempt. Once it returns `PENDING` or `AMBIGUOUS`, the only agent tool that is relevant is `get_payment_status`; the agent cannot create another charge.

## Core data model

`Mandate`

- `id`, `merchant_id`, `agent_id`, `max_amount`, `allowed_categories`, `expires_at`, `signed_token`

`CheckoutIntent`

- `id`, `customer_id`, `mandate_id`, `cart_hash`, `amount`, `status`
- Unique key: `(customer_id, client_request_id)`. Repeating the same user request returns the existing intent.

`PaymentAttempt`

- `id`, `intent_id`, `razorpay_order_id`, `razorpay_payment_id` (nullable until known), `status`, `last_authoritative_at`, `resolution_reason`
- One active attempt per intent. A new attempt is prohibited while status is `PENDING` or `AMBIGUOUS`.

`WebhookEvent`

- `provider_event_id` (unique), verified payload, received time, processing result.

`AuditEvent`

- `sequence`, `intent_id`, `attempt_id`, `type`, `actor`, `evidence_source`, `payload`, `created_at`.
- Append-only: mandate decision, order creation, checkout start, received webhook, reconciliation lookup, state transition, customer message, and any operator decision are all recorded.

SQLite is sufficient for the demo. Its unique constraints are the idempotency mechanism; no cache or separate queue is needed initially.

## State and resolution rules

| Current state | Verified evidence | Next state | Customer-facing result |
|---|---|---|---|
| `CREATED` | Razorpay order created | `PENDING` | “Awaiting payment.” |
| `PENDING` | captured/authorized success event or successful authoritative lookup | `CAPTURED` | “Payment confirmed.” |
| `PENDING` | explicit failed event/lookup | `FAILED` | “Payment failed; you may try again.” |
| `PENDING` | client says debited, provider result absent/delayed, or inconsistent evidence | `AMBIGUOUS` | “Still being confirmed; no new charge will be started.” |
| `AMBIGUOUS` | captured/failed/reversed terminal provider result | corresponding terminal state | final accurate result |
| `AMBIGUOUS` | no terminal result after a configured demo window | remains `AMBIGUOUS`; operator recheck | still no retry |

Only a terminal `FAILED` result permits a new payment attempt. `CAPTURED`, `REVERSED`, and `REFUNDED` close the attempt. Provider webhooks and provider status lookup are authoritative; browser success/failure callbacks are useful evidence but never final authority.

## Live payment flow

1. User asks the agent to buy products.
2. Server deterministically validates the cart against the signed mandate and logs each pass/fail.
3. Server creates or returns the existing `CheckoutIntent`, then creates exactly one Razorpay order for its idempotency key.
4. The browser opens Razorpay Checkout. Its response is recorded as non-final evidence.
5. The webhook endpoint verifies the Razorpay signature, deduplicates `provider_event_id`, stores the raw event, and maps it to the attempt.
6. The resolver applies the authoritative transition and appends an audit event.
7. If the browser reports debit/timeout but no terminal provider event is available, the resolver marks the attempt `AMBIGUOUS`, schedules/requires a status lookup, and blocks another checkout.
8. The later webhook or lookup resolves the existing attempt. It never mutates history or creates a replacement charge.

## Reuse from the existing repository

Reuse/adapt these ideas, not its simulated runtime:

- Authority-typed state transitions and invalid-transition handling.
- Explicit timeout, merchant mismatch, reversal-pending, and refund-pending classifications.
- Policy rule: money-moving operations require human approval.
- Append-only, ordered audit trail and operator/customer separation.

Do not reuse `generate_scenario_instance`, hidden-future revelation, or `simulate_retry` as production behavior. They are excellent demo/test fixtures, not live payment plumbing.

## Demo scenarios

1. **Normal success:** mandate passes → order → verified webhook → `CAPTURED` → customer confirmation.
2. **Mandate rejection:** cap/category/expiry fails → no Razorpay order is created.
3. **Ambiguous payment:** delay the local webhook processor after checkout → show `AMBIGUOUS`, blocked retry button, and audit timeline → release/process the verified event → show final result.
4. **Duplicate protection:** submit the same client request twice → same intent/order returned; no second charge attempt.

## Delivery plan

### Week 1 — safe checkout foundation

- Catalogue, mandate issuance/validation, SQLite schema, audit writer.
- Create Razorpay Test Mode orders with stable client request IDs.
- Prove normal checkout and verified webhook processing before adding the AI UI.

### Week 2 — ambiguity resolver

- Implement the attempt state machine, webhook-event dedupe, authoritative status lookup, and retry block.
- Build the local delayed-webhook harness and test all four demo scenarios.
- Adapt the existing repository's audit-timeline presentation.

### Week 3 — agent experience and pitch

- Add natural-language cart construction around deterministic mandate checks.
- Finish customer status and operator audit views.
- Record the ambiguity demo: debit/unknown → hold → authoritative result → accurate final message.

## Acceptance checks

- Replayed client request does not create another Razorpay order.
- Duplicate webhook does not create another state transition or audit decision.
- No UI/API path can retry while an attempt is `PENDING` or `AMBIGUOUS`.
- Browser callback alone cannot mark a payment successful.
- A verified terminal webhook or status lookup resolves the original attempt.
- Every state change and user-visible message has a linked audit event.
- The UI labels all payments as Razorpay Test Mode and all delayed-webhook behavior as a demo harness.
