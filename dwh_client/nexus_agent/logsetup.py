"""
logsetup.py — logging del agente.

* Archivo ``logs/nexus_agent.log`` con rotación DIARIA a medianoche
  (TimedRotatingFileHandler); se conservan ``log_retention_days`` archivos
  (defecto 7) con sufijo de fecha: nexus_agent.log.2026-09-27
* Todo pasa por ``RedactingFormatter``: se quitan secretos conocidos, SQL,
  credenciales y valores de filas, incluso en trazas.
"""

import logging
import logging.handlers
import os
import sys

from .sanitize import RedactingFormatter

LOGGER_NAME = "nexus"
_FMT = "%(asctime)s | %(levelname)-8s | %(threadName)-10s | %(message)s"


def setup_logging(log_dir: str, retention_days: int = 7, console: bool = True) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    fmt = RedactingFormatter(_FMT, datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.TimedRotatingFileHandler(
        os.path.join(log_dir, "nexus_agent.log"), when="midnight", backupCount=retention_days,
        encoding="utf-8", delay=False,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
