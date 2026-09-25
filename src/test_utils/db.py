"""Real-Postgres fixture for integration tests.

Tests using `db_session` are skipped unless TEST_DB_URI points at a disposable
Postgres with pgvector available. The schema is dropped and recreated per test.
"""

import os
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, text
from sqlmodel.ext.asyncio.session import AsyncSession

import models  # noqa: F401  - registers every table on SQLModel.metadata


@pytest_asyncio.fixture(loop_scope="function")
async def db_session() -> AsyncIterator[AsyncSession]:
    uri = os.environ.get("TEST_DB_URI")
    if not uri:
        pytest.skip("TEST_DB_URI not set")

    engine = create_async_engine(uri)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()
