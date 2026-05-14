"""按自然日切分日志文件：活动文件为 ``{prefix}-YYYY-MM-DD{suffix}``，零点滚动，并可按份数清理旧文件。"""

from __future__ import annotations

import re
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class DailyDatedFileHandler(TimedRotatingFileHandler):
    """每天一个独立文件，活动文件名本身带日期；跨天后打开新日期文件，旧文件保留原名。"""

    def __init__(
        self,
        log_dir: Path,
        file_prefix: str,
        *,
        file_suffix: str = ".log",
        backup_days: int = 30,
    ) -> None:
        self._log_dir = log_dir
        self._file_prefix = file_prefix
        self._file_suffix = file_suffix
        self._backup_days = backup_days
        core = re.escape(file_prefix) + r"-(\d{4}-\d{2}-\d{2})" + re.escape(file_suffix) + "$"
        self._date_pattern = re.compile(core)
        super().__init__(
            filename=str(log_dir / self._dated_filename()),
            when="midnight",
            backupCount=0,
            encoding="utf-8",
            utc=False,
        )

    def _dated_filename(self, now: datetime | None = None) -> str:
        stamp = (now or datetime.now()).strftime("%Y-%m-%d")
        return f"{self._file_prefix}-{stamp}{self._file_suffix}"

    def doRollover(self) -> None:  # noqa: N802 (stdlib naming)
        if self.stream:
            self.stream.close()
            self.stream = None

        self.baseFilename = str(self._log_dir / self._dated_filename())
        if not self.delay:
            self.stream = self._open()

        current_time = int(time.time())
        new_rollover_at = self.computeRollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at += self.interval
        self.rolloverAt = new_rollover_at

        self._cleanup_old_files()

    def _cleanup_old_files(self) -> None:
        if self._backup_days <= 0:
            return
        matched: list[tuple[str, Path]] = []
        for entry in self._log_dir.iterdir():
            if not entry.is_file():
                continue
            m = self._date_pattern.match(entry.name)
            if m:
                matched.append((m.group(1), entry))
        matched.sort()
        excess = len(matched) - self._backup_days
        for _, path in matched[: max(excess, 0)]:
            try:
                path.unlink()
            except OSError:
                pass
