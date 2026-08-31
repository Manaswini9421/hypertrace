"""SQLAlchemy engine/session helpers, shared by services from Phase 2 onward.

Requires the `db` extra (`pip install hypertrace-common[db]`) — kept optional
so lightweight services like the Phase 1 collector, which only talk to
RabbitMQ, don't need SQLAlchemy/psycopg2 in their image.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DatabaseSettings


class Base(DeclarativeBase):
    pass


def make_engine(settings: DatabaseSettings | None = None) -> Engine:
    settings = settings or DatabaseSettings()
    return create_engine(settings.url, pool_pre_ping=True)


def make_session_factory(settings: DatabaseSettings | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(settings), expire_on_commit=False)
