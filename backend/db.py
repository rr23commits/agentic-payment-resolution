"""PostgreSQL connection and schema setup for the backend."""

import os
from pathlib import Path

import psycopg


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect() -> psycopg.Connection:
    """Open the configured local PostgreSQL database."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set; copy .env.example to .env")
    return psycopg.connect(database_url)


def migrate() -> None:
    """Apply the idempotent initial schema."""
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(SCHEMA_PATH.read_text())

