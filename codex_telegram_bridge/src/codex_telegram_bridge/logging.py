from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

TELEGRAM_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
TELEGRAM_BARE_TOKEN_RE = re.compile(r"\b\d+:[A-Za-z0-9_-]{10,}\b")


class RedactTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True

        redacted = TELEGRAM_TOKEN_RE.sub("bot[REDACTED]", message)
        redacted = TELEGRAM_BARE_TOKEN_RE.sub("[REDACTED_TOKEN]", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(log_file: str | None, *, debug: bool = False) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = RedactTokenFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(fmt)
    console.addFilter(redactor)
    root_logger.addHandler(console)

    if log_file:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redactor)
        root_logger.addHandler(file_handler)
        logging.getLogger(__name__).debug("[debug] file logger initialized path=%r", log_file)
