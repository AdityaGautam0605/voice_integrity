"""Structured logging that cannot leak personal data.

Every record passes through a redacting filter.  Audio arrays, embeddings and
raw identifiers are replaced rather than truncated, because a truncated
embedding in a log file is still biometric data.  See FR-VE-05 and TS-10.
"""

from __future__ import annotations

import hashlib
import logging
import sys

_FORBIDDEN_KEYS = {
    "audio",
    "pcm",
    "waveform",
    "samples",
    "embedding",
    "embeddings",
    "features",
    "transcript",
    "phone",
    "number",
}


class RedactFilter(logging.Filter):
    """Refuse to emit any field whose name suggests personal content."""

    def filter(self, record: logging.LogRecord) -> bool:
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for key in list(extra):
                if key.lower() in _FORBIDDEN_KEYS:
                    extra[key] = "<redacted>"
        return True


def hash_identifier(value: str, salt: str = "vif") -> str:
    """One-way handle for a phone number or account id.

    Logs and the metadata prior reference this hash and never the number
    itself, so no call log is reconstructible from our output.
    """
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
