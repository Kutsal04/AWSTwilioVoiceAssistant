import io
import json
import logging

from app.logging import JsonFormatter
from app.metrics import (
    METRIC_NAMESPACE,
    emit_barge_in_count,
    emit_call_count,
    emit_error_count,
    emit_metric,
    emit_turn_response_latency,
    emf_payload,
)


def test_emf_payload_shape() -> None:
    payload = emf_payload("CallCount", 1, {"persona_id": "warm_clinical_followup"})

    assert payload["CallCount"] == 1
    assert payload["persona_id"] == "warm_clinical_followup"
    directive = payload["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == METRIC_NAMESPACE
    assert directive["Dimensions"] == [["persona_id"]]
    assert directive["Metrics"] == [{"Name": "CallCount", "Unit": "Count"}]


def test_json_formatter_emits_emf_payload_as_top_level_json() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("phase12.metrics")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    emit_metric("ErrorCount", 1, {"error_kind": "test_error"}, logger=logger)

    payload = json.loads(stream.getvalue())
    assert payload["ErrorCount"] == 1
    assert payload["error_kind"] == "test_error"
    assert "_aws" in payload
    assert "message" not in payload


def test_named_metric_helpers_emit_expected_dimensions() -> None:
    call_count = emit_call_count("appointment_reminder")
    latency = emit_turn_response_latency("appointment_reminder", 123.4)
    error = emit_error_count("TwilioMediaProtocolError")
    barge_in = emit_barge_in_count("appointment_reminder")

    assert call_count["persona_id"] == "appointment_reminder"
    assert call_count["CallCount"] == 1
    assert latency["TurnResponseLatencyMs"] == 123.4
    assert latency["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Unit"] == "Milliseconds"
    assert error["error_kind"] == "TwilioMediaProtocolError"
    assert error["ErrorCount"] == 1
    assert barge_in["persona_id"] == "appointment_reminder"
    assert barge_in["BargeInCount"] == 1
