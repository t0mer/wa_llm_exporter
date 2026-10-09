import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app as exporter

TEST_DB_URI = os.getenv(
    "EXPORTER_TEST_DB_URI",
    "postgresql+asyncpg://user:password@localhost:55437/exp_test",
)

SCHEMA = [
    'DROP TABLE IF EXISTS kb_topic_message, kbtopic, opt_out, reaction, message, sender, "group" CASCADE',
    """CREATE TABLE "group" (
        group_jid text PRIMARY KEY, group_name text, display_name text,
        managed boolean NOT NULL DEFAULT false,
        notify_on_spam boolean NOT NULL DEFAULT false,
        community_keys text[], created_at timestamptz DEFAULT now())""",
    "CREATE TABLE sender (jid text PRIMARY KEY, push_name text)",
    """CREATE TABLE message (
        message_id text PRIMARY KEY, timestamp timestamptz NOT NULL,
        text text, media_url text, chat_jid text, sender_jid text,
        group_jid text, reply_to_id text)""",
    "CREATE TABLE reaction (reaction_id text PRIMARY KEY, emoji text)",
    "CREATE TABLE opt_out (jid text PRIMARY KEY)",
    "CREATE TABLE kbtopic (id text PRIMARY KEY)",
    "CREATE TABLE kb_topic_message (kb_topic_id text, message_id text)",
]


@pytest_asyncio.fixture
async def db(monkeypatch):
    """A throwaway Postgres schema wired into the exporter's session factory."""
    engine = create_async_engine(TEST_DB_URI, poolclass=NullPool)
    async with engine.begin() as conn:
        for stmt in SCHEMA:
            await conn.execute(text(stmt))
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(exporter, "async_session_factory", factory)

    async def run(sql, **params):
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)

    yield run
    await engine.dispose()
