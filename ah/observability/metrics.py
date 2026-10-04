"""Prometheus text exposition for the existing in-process collector."""

from __future__ import annotations

from ah.core.metrics import MetricsCollector, metrics


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def prometheus_text(collector: MetricsCollector = metrics) -> str:
    data = collector.get_all_metrics()
    lines = [
        "# HELP ah_operations_total Completed operations.",
        "# TYPE ah_operations_total counter",
    ]
    for name, value in sorted(data["counters"].items()):
        lines.append(f'ah_operations_total{{operation="{_label(name)}"}} {value}')
    lines += [
        "# HELP ah_errors_total Failed operations.",
        "# TYPE ah_errors_total counter",
    ]
    for name, value in sorted(data["errors"].items()):
        lines.append(f'ah_errors_total{{operation="{_label(name)}"}} {value}')
    lines += [
        "# HELP ah_duration_milliseconds Recent operation latency.",
        "# TYPE ah_duration_milliseconds summary",
    ]
    for name, stats in sorted(data["latency_totals"].items()):
        label = _label(name)
        lines.append(f'ah_duration_milliseconds_count{{operation="{label}"}} {int(stats["count"])}')
        lines.append(f'ah_duration_milliseconds_sum{{operation="{label}"}} {stats["sum"]}')
    lines += ["# HELP ah_tokens_total Tokens recorded by agents.", "# TYPE ah_tokens_total counter"]
    for kind, count in data["token_totals"].items():
        lines.append(f'ah_tokens_total{{kind="{kind}"}} {count}')
    return "\n".join(lines) + "\n"
