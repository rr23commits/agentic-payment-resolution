"""Apply the local PostgreSQL schema."""

from backend.db import migrate


def main() -> None:
    migrate()
    print("PostgreSQL schema is ready.")


if __name__ == "__main__":
    main()

