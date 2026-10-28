"""Explicitly create missing tables from registered SQLAlchemy models.

This is an initial/local setup helper, not a schema migration system.
"""

import asyncio

import app.main as _main_app  # noqa: F401 - imports all feature ORM models
from app.database.base import Base
from app.database.session import init_db


async def create_tables() -> None:
    if not Base.metadata.tables:
        raise RuntimeError(
            "No ORM tables are registered yet. Implement and import model modules first."
        )

    await init_db()


if __name__ == "__main__":
    asyncio.run(create_tables())
