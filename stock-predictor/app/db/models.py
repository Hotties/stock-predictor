"""8개 테이블의 SQLAlchemy 정의. sql/001_create_tables.sql과 같은 구조다.

MariaDB에서는 sql/001_create_tables.sql로 테이블을 만들고, 이 모델은 읽기·쓰기용이다.
테스트(SQLite)에서는 connection.create_all()로 이 모델에서 테이블을 만든다.
숫자 컬럼은 asdecimal=False라서 Decimal이 아니라 float으로 읽힌다(pandas에서 쓰기 편하게).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer,
    Numeric, SmallInteger, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite는 INTEGER PRIMARY KEY만 자동 증가하므로 SQLite에서는 Integer로 바꿔 쓴다.
BigIntPk = BigInteger().with_variant(Integer, "sqlite")


def num(precision: int, scale: int) -> Numeric:
    return Numeric(precision, scale, asdecimal=False)


class Base(DeclarativeBase):
    pass


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = (Index("idx_stocks_name", "name"),)

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(Enum("KOSPI", "KOSDAQ", name="market_enum"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("1"))
    is_tracked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    is_seed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    last_loaded_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class PriceDaily(Base):
    __tablename__ = "prices_daily"

    code: Mapped[str] = mapped_column(String(6), ForeignKey("stocks.code"), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(num(14, 2))
    high: Mapped[float | None] = mapped_column(num(14, 2))
    low: Mapped[float | None] = mapped_column(num(14, 2))
    close: Mapped[float] = mapped_column(num(14, 2), nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    change_pct: Mapped[float | None] = mapped_column(num(8, 4))


class IndexDaily(Base):
    __tablename__ = "index_daily"

    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(num(14, 4), nullable=False)
    change_pct: Mapped[float | None] = mapped_column(num(8, 4))


class FeatureDaily(Base):
    __tablename__ = "features_daily"

    code: Mapped[str] = mapped_column(String(6), ForeignKey("stocks.code"), primary_key=True)
    base_date: Mapped[date] = mapped_column(Date, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    ma7_score: Mapped[float | None] = mapped_column(num(6, 4))
    ma14_score: Mapped[float | None] = mapped_column(num(6, 4))
    ma30_score: Mapped[float | None] = mapped_column(num(6, 4))
    ma120_score: Mapped[float | None] = mapped_column(num(6, 4))
    market_score: Mapped[float | None] = mapped_column(num(6, 4))
    market5_score: Mapped[float | None] = mapped_column(num(6, 4))
    us_score: Mapped[float | None] = mapped_column(num(6, 4))
    volume_score: Mapped[float | None] = mapped_column(num(6, 4))


class ModelVersion(Base):
    __tablename__ = "model_versions"

    version: Mapped[str] = mapped_column(String(20), primary_key=True)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    train_start: Mapped[date | None] = mapped_column(Date)
    train_end: Mapped[date | None] = mapped_column(Date)
    n_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("500"))
    direction_acc: Mapped[float | None] = mapped_column(num(5, 4))
    baseline_acc: Mapped[float | None] = mapped_column(num(5, 4))
    mae: Mapped[float | None] = mapped_column(num(8, 4))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ModelWeight(Base):
    __tablename__ = "model_weights"

    version: Mapped[str] = mapped_column(String(20), ForeignKey("model_versions.version"), primary_key=True)
    component: Mapped[str] = mapped_column(String(20), primary_key=True)
    weight: Mapped[float] = mapped_column(num(10, 6), nullable=False)


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("code", "base_date", "version", name="uq_pred"),
        Index("idx_pred_target", "code", "target_date"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), ForeignKey("stocks.code"), nullable=False)
    base_date: Mapped[date] = mapped_column(Date, nullable=False)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    version: Mapped[str] = mapped_column(String(20), ForeignKey("model_versions.version"), nullable=False)
    base_close: Mapped[float] = mapped_column(num(14, 2), nullable=False)
    pred_return_pct: Mapped[float] = mapped_column(num(8, 4), nullable=False)
    up_prob: Mapped[float | None] = mapped_column(num(5, 4))
    price_low: Mapped[float | None] = mapped_column(num(14, 2))
    price_high: Mapped[float | None] = mapped_column(num(14, 2))
    actual_close: Mapped[float | None] = mapped_column(num(14, 2))
    actual_return_pct: Mapped[float | None] = mapped_column(num(8, 4))
    hit: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("idx_runs_job", "job", "started_at"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(Enum("running", "success", "failed", name="run_status"), nullable=False)
    processed_rows: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
