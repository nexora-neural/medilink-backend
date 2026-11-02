"""Direct developers to the migration workflow for database schema changes."""


if __name__ == "__main__":
    raise SystemExit(
        "Table creation is managed by Alembic. Run: python -m alembic upgrade head"
    )
