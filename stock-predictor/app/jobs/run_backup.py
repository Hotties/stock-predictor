"""하루 한 번 DB를 덤프해 파일로 남기고, 오래된 파일은 지운다(기본 7일 보관).

mariadb-dump 또는 mysqldump 명령이 PATH에 있어야 한다. 비밀번호는 명령줄에 쓰지 않고 환경변수 MYSQL_PWD로 넘긴다.
백업 실패는 갱신을 멈추지 않는다(호출한 쪽이 결과만 기록한다).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .context import JobContext, build_context, setup_logging


@dataclass
class BackupResult:
    status: str                      # created | skipped_exists | unavailable | failed
    path: Path | None = None
    removed: list[Path] = field(default_factory=list)
    message: str = ""


def _find_dump_binary(which) -> str | None:
    for name in ("mariadb-dump", "mysqldump"):
        found = which(name)
        if found:
            return found
    return None


def _cleanup(directory: Path, db_name: str, today: date, keep_days: int) -> list[Path]:
    pattern = re.compile(rf"^{re.escape(db_name)}_(\d{{4}}-\d{{2}}-\d{{2}})\.sql$")
    removed = []
    for f in directory.iterdir():
        m = pattern.match(f.name)
        if not m:
            continue                                   # 우리가 만든 이름이 아니면 건드리지 않는다
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if (today - d).days >= keep_days:
            f.unlink()
            removed.append(f)
    return removed


def run_backup(ctx: JobContext, *, runner=subprocess.run, which=shutil.which) -> BackupResult:
    cfg = ctx.settings
    today = ctx.now().date()
    directory = cfg.resolve(cfg.paths.backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    db = cfg.database
    target = directory / f"{db.name}_{today.isoformat()}.sql"
    if target.exists():
        return BackupResult("skipped_exists", target, message="오늘 백업이 이미 있습니다")

    binary = _find_dump_binary(which)
    if binary is None:
        return BackupResult("unavailable", message="mariadb-dump 또는 mysqldump를 찾을 수 없어 백업을 건너뜁니다")

    cmd = [binary, f"--host={db.host}", f"--port={db.port}", f"--user={db.user}",
           "--single-transaction", "--routines", db.name]
    env = dict(os.environ)
    if db.password:
        env["MYSQL_PWD"] = db.password
    part = target.with_suffix(".sql.part")
    try:
        with open(part, "wb") as out:
            runner(cmd, stdout=out, stderr=subprocess.PIPE, env=env, check=True)
        os.replace(part, target)                       # 다 쓴 뒤에 이름을 바꿔 미완성 파일이 남지 않게 한다
    except (subprocess.CalledProcessError, OSError) as exc:
        part.unlink(missing_ok=True)
        detail = getattr(exc, "stderr", None)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        return BackupResult("failed", message=f"백업 실패: {exc!r} {detail or ''}".strip())
    removed = _cleanup(directory, db.name, today, cfg.paths.backup_keep_days)
    return BackupResult("created", target, removed, f"{target.name} 생성, 오래된 파일 {len(removed)}개 삭제")


def main() -> None:
    setup_logging()
    result = run_backup(build_context())
    print(result.status, result.message)


if __name__ == "__main__":
    main()
