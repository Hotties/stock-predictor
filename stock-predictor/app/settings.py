"""설정 읽기. 설정 파일(TOML)과 환경변수(DB 비밀번호)를 합친다.

찾는 순서: load_settings(path) 인자 → 환경변수 STOCK_CONFIG → config/settings.toml → config/settings.example.toml
DB 비밀번호는 파일에 쓰지 않고 환경변수 STOCK_DB_PASSWORD로 받는다.
알 수 없는 설정 이름이 있으면 오타를 막기 위해 오류를 낸다.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from datetime import time
from pathlib import Path
from urllib.parse import quote_plus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_CONFIG = "STOCK_CONFIG"
ENV_DB_PASSWORD = "STOCK_DB_PASSWORD"
DEFAULT_CONFIG_FILES = ("config/settings.toml", "config/settings.example.toml")


@dataclass(frozen=True)
class DatabaseSettings:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "stock"
    name: str = "stockdb"
    password: str = ""  # 환경변수로만 받는다

    def url(self) -> str:
        pw = quote_plus(self.password)
        return f"mysql+pymysql://{quote_plus(self.user)}:{pw}@{self.host}:{self.port}/{self.name}?charset=utf8mb4"


@dataclass(frozen=True)
class ScoringSettings:
    ma_k: float = 1.5
    market_div: float = 2.7
    us_div: float = 2.5
    volume_ln_base: float = 4.0
    sigma_window: int = 60
    volume_avg_window: int = 20


@dataclass(frozen=True)
class HistorySettings:
    load_trading_days: int = 870
    train_days: int = 500
    validate_days: int = 250


@dataclass(frozen=True)
class CollectorSettings:
    sleep_seconds: float = 0.3
    max_retries: int = 3
    retry_wait_seconds: float = 1.0


@dataclass(frozen=True)
class ScheduleSettings:
    close_confirm_time: time = time(16, 30)
    prediction_open_time: time = time(7, 0)


@dataclass(frozen=True)
class SeedSettings:
    kospi_count: int = 70
    kosdaq_count: int = 30


@dataclass(frozen=True)
class PathSettings:
    extra_holidays: str = "config/extra_holidays.csv"
    backup_dir: str = "backup"
    backup_keep_days: int = 7
    dump_command: str = ""   # mariadb-dump/mysqldump가 PATH에 없을 때 실행 파일 전체 경로를 쓴다(비우면 PATH에서 찾는다)


@dataclass(frozen=True)
class RuntimeSettings:
    stale_run_hours: float = 6.0


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    history: HistorySettings = field(default_factory=HistorySettings)
    collector: CollectorSettings = field(default_factory=CollectorSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    seed: SeedSettings = field(default_factory=SeedSettings)
    paths: PathSettings = field(default_factory=PathSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)

    def resolve(self, relative: str) -> Path:
        """프로젝트 루트 기준 상대 경로를 절대 경로로 바꾼다."""
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p


def parse_hhmm(value: str) -> time:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"시각은 HH:MM 형식이어야 합니다: {value!r}") from exc


def _build(cls, table: dict, section: str):
    allowed = {f.name for f in fields(cls)}
    unknown = set(table) - allowed
    if unknown:
        raise ValueError(f"[{section}]에 알 수 없는 설정이 있습니다: {sorted(unknown)}")
    return cls(**table)


def _validate(s: Settings) -> None:
    sc = s.scoring
    for name in ("ma_k", "market_div", "us_div"):
        if getattr(sc, name) <= 0:
            raise ValueError(f"[scoring] {name}은 0보다 커야 합니다")
    if sc.volume_ln_base <= 1:
        raise ValueError("[scoring] volume_ln_base는 1보다 커야 합니다")
    if sc.sigma_window < 2 or sc.volume_avg_window < 2:
        raise ValueError("[scoring] 기간(window)은 2 이상이어야 합니다")
    h = s.history
    if h.load_trading_days < 121:
        raise ValueError("[history] load_trading_days는 121 이상이어야 합니다(120일 이평선 준비)")
    if s.collector.max_retries < 1:
        raise ValueError("[collector] max_retries는 1 이상이어야 합니다")
    if s.seed.kospi_count < 0 or s.seed.kosdaq_count < 0:
        raise ValueError("[seed] 종목 수는 0 이상이어야 합니다")


def _find_config(path, environ):
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    elif environ.get(ENV_CONFIG):
        candidates.append(Path(environ[ENV_CONFIG]))
    else:
        candidates.extend(PROJECT_ROOT / f for f in DEFAULT_CONFIG_FILES)
    for c in candidates:
        if c.exists():
            return c
    if path or environ.get(ENV_CONFIG):
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {candidates[0]}")
    return None


def load_settings(path=None, environ=None) -> Settings:
    environ = os.environ if environ is None else environ
    cfg = _find_config(path, environ)
    data: dict = {}
    if cfg is not None:
        with open(cfg, "rb") as f:
            data = tomllib.load(f)

    known = {"database", "scoring", "history", "collector", "schedule", "seed", "paths", "runtime"}
    unknown_sections = set(data) - known
    if unknown_sections:
        raise ValueError(f"알 수 없는 설정 구역이 있습니다: {sorted(unknown_sections)}")

    db = dict(data.get("database", {}))
    if "password" in db:
        raise ValueError("DB 비밀번호는 설정 파일에 쓰지 말고 환경변수 STOCK_DB_PASSWORD로 넘기세요")
    if environ.get(ENV_DB_PASSWORD):
        db["password"] = environ[ENV_DB_PASSWORD]

    sched = dict(data.get("schedule", {}))
    for key in ("close_confirm_time", "prediction_open_time"):
        if key in sched:
            sched[key] = parse_hhmm(sched[key])

    settings = Settings(
        database=_build(DatabaseSettings, db, "database"),
        scoring=_build(ScoringSettings, data.get("scoring", {}), "scoring"),
        history=_build(HistorySettings, data.get("history", {}), "history"),
        collector=_build(CollectorSettings, data.get("collector", {}), "collector"),
        schedule=_build(ScheduleSettings, sched, "schedule"),
        seed=_build(SeedSettings, data.get("seed", {}), "seed"),
        paths=_build(PathSettings, data.get("paths", {}), "paths"),
        runtime=_build(RuntimeSettings, data.get("runtime", {}), "runtime"),
    )
    _validate(settings)
    return settings
