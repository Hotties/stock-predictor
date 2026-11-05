"""DB 연결. 엔진과 세션을 만든다. 연결 정보는 settings에서 온다."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base


def make_engine(url: str, echo: bool = False):
    """URL로 엔진을 만든다. 메모리 SQLite는 연결 하나를 공유하도록 한다(테스트용)."""
    if url in ("sqlite://", "sqlite:///:memory:"):
        return create_engine(
            url, echo=echo, poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
    return create_engine(url, echo=echo, pool_pre_ping=True)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory):
    """with session_scope(factory) as s: ... 끝나면 커밋, 오류가 나면 되돌린다."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine) -> None:
    """모델로 테이블을 만든다. 테스트(SQLite)용이고, MariaDB는 sql/001_create_tables.sql을 쓴다."""
    Base.metadata.create_all(engine)
