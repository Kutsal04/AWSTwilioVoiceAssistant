import json
import logging
from datetime import datetime, timezone
from typing import Any


ALLOWED_LOG_FIELD_NAMES = {
    "call_sid",
    "direction",
    "dropped_frames",
    "duration_ms",
    "error_kind",
    "event_name",
    "latency_ms",
    "media_frames",
    "persona_id",
    "queue_depth",
    "session_id",
    "state",
    "status",
    "stream_sid",
    "turn_index",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = event
        fields = getattr(record, "fields", None)
        if fields:
            payload["fields"] = fields
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def sanitize_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if key in ALLOWED_LOG_FIELD_NAMES}


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    sanitized_fields = sanitize_log_fields(fields)
    logger.log(level, event, extra={"event": event, "fields": sanitized_fields})
