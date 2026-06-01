import io
import json
import logging

from app.logging import JsonFormatter, log_event, sanitize_log_fields


def test_json_formatter_emits_structured_payload() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("phase1.json")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(logger, logging.INFO, "session_started", session_id="abc123", persona_id="warm_clinical_followup")

    payload = json.loads(stream.getvalue())

    assert payload["level"] == "INFO"
    assert payload["logger"] == "phase1.json"
    assert payload["message"] == "session_started"
    assert payload["event"] == "session_started"
    assert payload["fields"] == {
        "session_id": "abc123",
        "persona_id": "warm_clinical_followup",
    }
    assert "timestamp" in payload


def test_log_fields_keep_only_allowed_operational_fields() -> None:
    fields = sanitize_log_fields(
        {
            "session_id": "abc123",
            "persona_id": "warm_clinical_followup",
            "transcript_text": "sensitive caller content",
            "payload": {"text": "nested sensitive content"},
        }
    )

    assert fields == {
        "session_id": "abc123",
        "persona_id": "warm_clinical_followup",
    }


def test_log_event_drops_disallowed_fields_without_raising() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())

    logger = logging.getLogger("phase1.privacy")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(logger, logging.INFO, "event_with_content", session_id="abc123", transcript_text="do not log")

    payload = json.loads(stream.getvalue())

    assert payload["fields"] == {"session_id": "abc123"}
