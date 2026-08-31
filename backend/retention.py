"""Safe cleanup for raw provider payloads after payment processing completes."""

import os

from backend.db import connect


DEFAULT_WEBHOOK_RETENTION_DAYS = 30


def cleanup_webhook_payloads(retention_days: int | None = None) -> int:
    """Redact old processed raw payloads while preserving event identity and status."""
    days = retention_days
    if days is None:
        days = int(os.environ.get("WEBHOOK_RETENTION_DAYS", DEFAULT_WEBHOOK_RETENTION_DAYS))
    if days < 1:
        raise ValueError("retention_days must be positive")
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE webhook_events SET payload_json = jsonb_build_object(" 
            "'retained', FALSE, 'provider_event_id', provider_event_id) "
            "WHERE processed_at IS NOT NULL AND processed_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') "
            "AND payload_json->>'retained' IS DISTINCT FROM 'false'",
            (days,),
        )
        return cursor.rowcount


if __name__ == "__main__":
    print(cleanup_webhook_payloads())
