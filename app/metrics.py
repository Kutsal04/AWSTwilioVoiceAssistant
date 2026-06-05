import logging
from datetime import datetime, timezone
from typing import Any


METRIC_NAMESPACE = "AWSTwilioVoiceAssistant"
METRIC_LOGGER_NAME = "app.metrics"


def metric_payload(
    name: str,
    value: int | float,
    dimensions: dict[str, str] | None = None,
    unit: str = "Count",
) -> dict[str, Any]:
    return {
        "metric_name": name,
        "value": value,
        "dimensions": dimensions or {},
        "unit": unit,
    }


def emf_payload(
    name: str,
    value: int | float,
    dimensions: dict[str, str] | None = None,
    unit: str = "Count",
    namespace: str = METRIC_NAMESPACE,
) -> dict[str, Any]:
    metric_dimensions = dimensions or {}
    return {
        "_aws": {
            "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(metric_dimensions.keys())],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
        **metric_dimensions,
    }


def emit_metric(
    name: str,
    value: int | float,
    dimensions: dict[str, str] | None = None,
    unit: str = "Count",
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    payload = emf_payload(name=name, value=value, dimensions=dimensions, unit=unit)
    (logger or logging.getLogger(METRIC_LOGGER_NAME)).info(name, extra={"emf_payload": payload})
    return payload


def emit_call_count(persona_id: str) -> dict[str, Any]:
    return emit_metric("CallCount", 1, {"persona_id": persona_id})


def emit_turn_response_latency(persona_id: str, latency_ms: int | float) -> dict[str, Any]:
    return emit_metric("TurnResponseLatencyMs", latency_ms, {"persona_id": persona_id}, unit="Milliseconds")


def emit_error_count(error_kind: str) -> dict[str, Any]:
    return emit_metric("ErrorCount", 1, {"error_kind": error_kind})


def emit_barge_in_count(persona_id: str) -> dict[str, Any]:
    return emit_metric("BargeInCount", 1, {"persona_id": persona_id})


def emit_audio_frame_dropped(direction: str, persona_id: str, dropped_frames: int) -> dict[str, Any]:
    return emit_metric("AudioFrameDropped", dropped_frames, {"direction": direction, "persona_id": persona_id})
