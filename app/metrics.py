from typing import Any


def metric_payload(name: str, value: int | float, dimensions: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "metric_name": name,
        "value": value,
        "dimensions": dimensions or {},
    }

