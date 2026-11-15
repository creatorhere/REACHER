import json
import logging
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    FIELDS = ("job_id", "batch_id", "stage", "attempt", "elapsed_seconds", "error_type")

    def format(self, record):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in self.FIELDS:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        return json.dumps(entry, ensure_ascii=False)


def configure():
    logger = logging.getLogger("app")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
