CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    price_paise BIGINT NOT NULL CHECK (price_paise BETWEEN 0 AND 9000000000000000000),
    stock INTEGER NOT NULL CHECK (stock >= 0),
    restricted BOOLEAN NOT NULL DEFAULT FALSE
);

-- Optional merchandising data stays separate so legacy product fixtures remain
-- insert-compatible; price_paise remains the checkout authority.
CREATE TABLE IF NOT EXISTS product_metadata (
    product_id TEXT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    list_price_paise BIGINT,
    offer_label TEXT,
    offer_eligibility TEXT,
    offer_valid_until DATE,
    savings_paise BIGINT NOT NULL DEFAULT 0,
    related_product_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    recommendation_reason TEXT
);

CREATE TABLE IF NOT EXISTS mandates (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    max_amount_paise BIGINT NOT NULL CHECK (max_amount_paise >= 0),
    allowed_categories_json JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    token TEXT NOT NULL
);

-- Revisions live beside the legacy 8-column mandate table so old fixtures and
-- integrations remain insert-compatible.
CREATE TABLE IF NOT EXISTS mandate_revisions (
    mandate_id TEXT PRIMARY KEY REFERENCES mandates(id),
    version INTEGER NOT NULL CHECK (version > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS mandate_change_requests (
    request_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    mandate_id TEXT NOT NULL REFERENCES mandates(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS carts (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    items_json JSONB NOT NULL,
    total_paise BIGINT NOT NULL CHECK (total_paise BETWEEN 0 AND 9000000000000000000),
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkout_intents (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    mandate_id TEXT NOT NULL REFERENCES mandates(id),
    cart_id TEXT NOT NULL REFERENCES carts(id),
    client_request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (customer_id, client_request_id)
);

CREATE TABLE IF NOT EXISTS payment_attempts (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES checkout_intents(id),
    razorpay_order_id TEXT NOT NULL UNIQUE,
    razorpay_payment_id TEXT UNIQUE,
    status TEXT NOT NULL,
    last_authoritative_at TIMESTAMPTZ,
    resolution_reason TEXT
);

ALTER TABLE payment_attempts ADD COLUMN IF NOT EXISTS stock_reserved BOOLEAN NOT NULL DEFAULT FALSE;

-- A local CREATED attempt is persisted before the provider call so an uncertain
-- provider result cannot disappear and trigger a second external action.
ALTER TABLE payment_attempts ALTER COLUMN razorpay_order_id DROP NOT NULL;

ALTER TABLE payment_attempts DROP CONSTRAINT IF EXISTS payment_attempts_valid_status;
ALTER TABLE payment_attempts ADD CONSTRAINT payment_attempts_valid_status
    CHECK (status IN ('CREATED', 'PENDING', 'AMBIGUOUS', 'ABANDONED', 'CAPTURED', 'FAILED', 'REVERSED', 'REFUNDED'));

-- The database, not a future caller, enforces the unresolved-attempt boundary.
CREATE UNIQUE INDEX IF NOT EXISTS payment_attempts_one_active_per_intent
    ON payment_attempts (intent_id)
    WHERE status IN ('PENDING', 'AMBIGUOUS');

-- Customer-wide unresolved state is serialized by the transaction-scoped
-- customer advisory lock in start_checkout; PostgreSQL partial indexes cannot
-- express this invariant across payment_attempts and checkout_intents.
DROP INDEX IF EXISTS checkout_intents_one_unresolved_per_customer;

CREATE TABLE IF NOT EXISTS webhook_events (
    provider_event_id TEXT PRIMARY KEY,
    payload_json JSONB NOT NULL,
    signature_valid BOOLEAN NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    intent_id TEXT REFERENCES checkout_intents(id),
    attempt_id TEXT REFERENCES payment_attempts(id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    evidence_source TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (intent_id, sequence)
);

-- Mandate validation occurs before a checkout intent exists.
ALTER TABLE audit_events ALTER COLUMN intent_id DROP NOT NULL;

CREATE OR REPLACE FUNCTION prevent_audit_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_events are append-only';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'audit_events_append_only'
    ) THEN
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_event_mutation();
    END IF;
END;
$$;
