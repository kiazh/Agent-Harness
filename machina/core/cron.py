"""Small UTC five-field cron parser for scheduled jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["next_cron_time"]


def _field(text: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, separator, step_text = part.partition("/")
        try:
            step = int(step_text) if separator else 1
            if step < 1:
                raise ValueError
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                first, last = base.split("-", 1)
                start, end = int(first), int(last)
            else:
                start = int(base)
                # In cron, N/step means N through the end of the field.
                end = maximum if separator else start
        except ValueError:
            raise ValueError(f"invalid cron field {text!r}") from None
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron field {text!r} is out of range")
        values.update(range(start, end + 1, step))
    return values


def next_cron_time(expression: str, after: datetime) -> datetime:
    """Return the first matching minute strictly after *after*, using UTC.

    Supports numbers, ranges, comma lists, and steps. Day-of-month and
    day-of-week use standard cron OR semantics when both are restricted
    (both fields not exactly "*"); otherwise AND. Only a candidate with
    ``candidate > after_utc`` is returned (strictly-after).
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression must have five fields")
    minute, hour, day, month, weekday = (
        _field(fields[0], 0, 59),
        _field(fields[1], 0, 23),
        _field(fields[2], 1, 31),
        _field(fields[3], 1, 12),
        # Both 0 and 7 denote Sunday in standard five-field cron.
        {value % 7 for value in _field(fields[4], 0, 7)},
    )
    if after.tzinfo is None:
        raise ValueError("cron scheduling requires a timezone-aware timestamp")
    after_utc = after.astimezone(UTC)
    first_day = after_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    # Eight years covers leap-day schedules even across a non-leap century.
    for offset in range(8 * 366 + 1):
        current = first_day + timedelta(days=offset)
        if current.month not in month:
            continue
        day_matches = current.day in day
        weekday_matches = (current.weekday() + 1) % 7 in weekday
        # Cron's OR rule depends on whether each field is exactly "*",
        # not on substring containment: "*/2" contains "*" but is still
        # restricted. Only when both DOM and DOW are restricted (neither
        # field is exactly "*") do they combine with OR; otherwise AND.
        dom_star = fields[2] == "*"
        dow_star = fields[4] == "*"
        if not dom_star and not dow_star:
            date_matches = day_matches or weekday_matches
        else:
            date_matches = day_matches and weekday_matches
        if not date_matches:
            continue
        for hour_value in sorted(hour):
            for minute_value in sorted(minute):
                candidate = current.replace(hour=hour_value, minute=minute_value)
                if candidate > after_utc:
                    return candidate
    raise ValueError("cron expression has no occurrence in the next eight years")
