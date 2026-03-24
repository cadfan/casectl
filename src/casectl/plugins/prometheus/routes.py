"""FastAPI routes for the prometheus plugin.

Mounted at ``/api/plugins/prometheus`` by the plugin host.
Exposes metrics in Prometheus text exposition format.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level reference — set by the plugin during setup.
# ---------------------------------------------------------------------------

_get_metrics: Any = None  # callable returning latest metrics dict or None


def configure(get_metrics: Any) -> None:
    """Wire the metrics accessor into the route module.

    Called by :class:`PrometheusPlugin` during ``setup()``.
    """
    global _get_metrics  # noqa: PLW0603
    _get_metrics = get_metrics


# ---------------------------------------------------------------------------
# Prometheus text format helpers
# ---------------------------------------------------------------------------


def _format_gauge(name: str, help_text: str, value: float, labels: dict[str, str] | None = None) -> str:
    """Format a single Prometheus gauge metric in text exposition format.

    Parameters
    ----------
    name:
        Metric name (e.g. ``casectl_cpu_temp_celsius``).
    help_text:
        HELP line description.
    value:
        The gauge value.
    labels:
        Optional label key-value pairs.

    Returns
    -------
    str
        Complete HELP + TYPE + sample lines.
    """
    lines: list[str] = [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
    ]

    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f"{name}{{{label_str}}} {value}")
    else:
        lines.append(f"{name} {value}")

    return "\n".join(lines)


def _build_metrics_text(metrics: dict[str, Any]) -> str:
    """Build the complete Prometheus exposition text from a metrics dict.

    Parameters
    ----------
    metrics:
        A dict matching the SystemMetrics schema.

    Returns
    -------
    str
        Full Prometheus text exposition output.
    """
    sections: list[str] = []

    # CPU temperature.
    cpu_temp = float(metrics.get("cpu_temp", 0.0))
    sections.append(_format_gauge(
        "casectl_cpu_temp_celsius",
        "CPU die temperature in degrees Celsius.",
        cpu_temp,
    ))

    # Case temperature.
    case_temp = float(metrics.get("case_temp", 0.0))
    sections.append(_format_gauge(
        "casectl_case_temp_celsius",
        "Case / ambient temperature in degrees Celsius.",
        case_temp,
    ))

    # CPU usage (0-1 ratio).
    cpu_pct = float(metrics.get("cpu_percent", 0.0))
    sections.append(_format_gauge(
        "casectl_cpu_usage_ratio",
        "CPU utilisation as a ratio (0.0 to 1.0).",
        round(cpu_pct / 100.0, 6),
    ))

    # Memory usage (0-1 ratio).
    mem_pct = float(metrics.get("memory_percent", 0.0))
    sections.append(_format_gauge(
        "casectl_memory_usage_ratio",
        "Memory utilisation as a ratio (0.0 to 1.0).",
        round(mem_pct / 100.0, 6),
    ))

    # Disk usage (0-1 ratio).
    disk_pct = float(metrics.get("disk_percent", 0.0))
    sections.append(_format_gauge(
        "casectl_disk_usage_ratio",
        "Root disk utilisation as a ratio (0.0 to 1.0).",
        round(disk_pct / 100.0, 6),
    ))

    # Fan duty per channel (0-1 ratio, from 0-255 hardware range).
    fan_duty = metrics.get("fan_duty", [0, 0, 0])
    if not isinstance(fan_duty, list):
        fan_duty = [0, 0, 0]

    # HELP and TYPE lines only once for the metric family.
    duty_lines: list[str] = [
        "# HELP casectl_fan_duty_ratio Fan PWM duty cycle as a ratio (0.0 to 1.0).",
        "# TYPE casectl_fan_duty_ratio gauge",
    ]
    for i in range(3):
        duty_val = float(fan_duty[i]) / 255.0 if i < len(fan_duty) else 0.0
        duty_lines.append(
            f'casectl_fan_duty_ratio{{channel="{i}"}} {round(duty_val, 6)}'
        )
    sections.append("\n".join(duty_lines))

    # Fan RPM per channel.
    motor_speed = metrics.get("motor_speed", [0, 0, 0])
    if not isinstance(motor_speed, list):
        motor_speed = [0, 0, 0]

    rpm_lines: list[str] = [
        "# HELP casectl_fan_rpm Fan speed in revolutions per minute.",
        "# TYPE casectl_fan_rpm gauge",
    ]
    for i in range(3):
        rpm_val = float(motor_speed[i]) if i < len(motor_speed) else 0.0
        rpm_lines.append(
            f'casectl_fan_rpm{{channel="{i}"}} {rpm_val}'
        )
    sections.append("\n".join(rpm_lines))

    return "\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/metrics")
async def prometheus_metrics() -> PlainTextResponse:
    """Return system metrics in Prometheus text exposition format.

    Content-Type is ``text/plain; version=0.0.4; charset=utf-8`` per the
    Prometheus specification.
    """
    metrics: dict[str, Any] | None = None
    if _get_metrics is not None:
        metrics = _get_metrics()

    if metrics is None:
        # Return empty metrics rather than an error — Prometheus expects
        # a successful response even when no data is available yet.
        return PlainTextResponse(
            content="# No metrics collected yet.\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    text = _build_metrics_text(metrics)
    return PlainTextResponse(
        content=text,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
