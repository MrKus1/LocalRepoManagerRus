import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings

_loggers = {}


def mirror_logger(name: str) -> logging.Logger:
    logger = _loggers.get(name)
    if logger:
        return logger
    log_dir = Path(settings.LOGS_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"mirror.{name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    _loggers[name] = logger
    return logger
