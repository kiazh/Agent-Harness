"""Prometheus text exposition for the existing in-process collector."""

from __future__ import annotations

from machina.core.metrics import MetricsCollector, metrics


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def prometheus_text(collector: MetricsCollector = metrics) -> str:
    data = collector.get_all_metrics()
    lines = [
        "# HELP machina_operations_total Completed operations.",
        "# TYPE machina_operations_total counter",
    ]
    for name, value in sorted(data["counters"].items()):
        lines.append(f'machina_operations_total{{operation="{_label(name)}"}} {value}')
    lines += [
        "# HELP machina_errors_total Failed operations.",
        "# TYPE machina_errors_total counter",
    ]
    for name, value in sorted(data["errors"].items()):
        lines.append(f'machina_errors_total{{operation="{_label(name)}"}} {value}')
    lines += [
        "# HELP machina_duration_milliseconds Recent operation latency.",
        "# TYPE machina_duration_milliseconds summary",
    ]
    for name, stats in sorted(data["latency_totals"].items()):
        label = _label(name)
        lines.append(
            f'machina_duration_milliseconds_count{{operation="{label}"}} {int(stats["count"])}'
        )
        lines.append(f'machina_duration_milliseconds_sum{{operation="{label}"}} {stats["sum"]}')
    lines += [
        "# HELP machina_tokens_total Tokens recorded by agents.",
        "# TYPE machina_tokens_total counter",
    ]
    for kind, count in data["token_totals"].items():
        lines.append(f'machina_tokens_total{{kind="{kind}"}} {count}')
    return "\n".join(lines) + "\n"
