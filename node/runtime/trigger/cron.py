# xphi.node.runtime.trigger.cron
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MAX_SEARCH_MINUTES = 366 * 24 * 60  # 윤년 포함 최대 1년 탐색

_MACROS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTH_NAMES = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}


def resolve_tz(tz_name: str | None, *, strict: bool = False) -> timezone | ZoneInfo:
    if not tz_name or tz_name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        if strict:
            raise ValueError(f"Unknown IANA timezone {tz_name!r}.") from exc
        return timezone.utc


def _replace_names(expr: str, mapping: dict[str, int]) -> str:
    upper = expr.upper()
    for name, num in mapping.items():
        upper = upper.replace(name, str(num))
    return upper


def _parse_field(
    field_str: str,
    min_val: int,
    max_val: int,
    *,
    names: dict[str, int] | None = None,
    is_dow: bool = False,
) -> tuple[frozenset[int], bool, bool]:
    """Returns `(allowed_values, is_wildcard, has_last_day_flag)`."""
    raw = _replace_names(field_str.strip(), names) if names else field_str.strip().upper()
    if not raw:
        raise ValueError("Empty cron field.")
    if raw == "*":
        return frozenset(range(min_val, max_val + 1)), True, False

    values: set[int] = set()
    has_last = False
    max_bound = 7 if is_dow else max_val

    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty cron field segment in '{field_str}'")
        if part == "L" and not is_dow:
            has_last = True
            continue

        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"Cron step must be > 0 in '{field_str}'")

        if part == "*":
            start, end = min_val, max_val
        elif "-" in part:
            s_str, e_str = part.split("-", 1)
            start, end = int(s_str), int(e_str)
        else:
            val = int(part)
            if step == 1:
                if not (min_val <= val <= max_bound):
                    raise ValueError(f"Cron value {val} out of range [{min_val}, {max_bound}] in '{field_str}'")
                values.add(0 if (is_dow and val == 7) else val)
                continue
            start, end = val, max_val

        if start < min_val or end > max_bound or start > end:
            raise ValueError(f"Invalid cron range '{part}' in '{field_str}'")

        for v in range(start, end + 1, step):
            values.add(0 if (is_dow and v == 7) else v)

    return frozenset(values), False, has_last


@dataclass(frozen=True, slots=True)
class CompiledCron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_is_star: bool
    dow_is_star: bool
    dom_has_last: bool

    def _match_day(self, year: int, month: int, day: int, cron_dow: int) -> bool:
        last_dom = calendar.monthrange(year, month)[1]
        dom_match = (day in self.doms) or (self.dom_has_last and day == last_dom)
        dow_match = cron_dow in self.dows

        if not self.dom_is_star and not self.dow_is_star:
            return dom_match or dow_match
        return dom_match and dow_match

    def matches_dt(self, dt: datetime) -> bool:
        cron_dow = (dt.weekday() + 1) % 7
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self._match_day(dt.year, dt.month, dt.day, cron_dow)
        )

    def latest_slot_at(self, now_epoch: int, tz_name: str = "UTC", *, lookback_minutes: int = 60) -> int | None:
        tz = resolve_tz(tz_name, strict=True)
        aligned_epoch = (int(now_epoch) // 60) * 60
        base_dt = datetime.fromtimestamp(aligned_epoch, tz=tz)

        for offset in range(max(0, lookback_minutes) + 1):
            cand_dt = base_dt - timedelta(minutes=offset)
            if self.matches_dt(cand_dt):
                return aligned_epoch - (offset * 60)
        return None

    def next_slot_after(self, now_epoch: float, tz_name: str = "UTC") -> float:
        """`now_epoch` 이후 가장 빠른 실행 시각(epoch)을 월/일/시 단위 점프로 고속 탐색."""
        tz = resolve_tz(tz_name, strict=False)
        cursor = datetime.fromtimestamp(now_epoch, tz=tz).replace(second=0, microsecond=0) + timedelta(minutes=1)

        visited = 0
        while visited < _MAX_SEARCH_MINUTES:
            if cursor.month not in self.months:
                next_year = cursor.year + (1 if cursor.month == 12 else 0)
                next_month = 1 if cursor.month == 12 else cursor.month + 1
                nxt = cursor.replace(year=next_year, month=next_month, day=1, hour=0, minute=0)
                visited += max(1, int((nxt - cursor).total_seconds() // 60))
                cursor = nxt
                continue

            cron_dow = (cursor.weekday() + 1) % 7
            if not self._match_day(cursor.year, cursor.month, cursor.day, cron_dow):
                nxt = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
                visited += max(1, int((nxt - cursor).total_seconds() // 60))
                cursor = nxt
                continue

            if cursor.hour not in self.hours:
                nxt = (cursor + timedelta(hours=1)).replace(minute=0)
                visited += max(1, int((nxt - cursor).total_seconds() // 60))
                cursor = nxt
                continue

            if cursor.minute not in self.minutes:
                cursor += timedelta(minutes=1)
                visited += 1
                continue

            return cursor.timestamp()

        raise ValueError(f"No reachable cron slot within 1 year for {self.expr!r}.")


@lru_cache(maxsize=256)
def compile_cron(expr: str) -> CompiledCron:
    norm = _MACROS.get(expr.strip().lower(), expr.strip())
    fields = norm.split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5-field cron expression or @macro, got {expr!r}")

    minutes, _, _ = _parse_field(fields[0], 0, 59)
    hours, _, _ = _parse_field(fields[1], 0, 23)
    doms, dom_star, dom_last = _parse_field(fields[2], 1, 31)
    months, _, _ = _parse_field(fields[3], 1, 12, names=_MONTH_NAMES)
    dows, dow_star, _ = _parse_field(fields[4], 0, 6, names=_DOW_NAMES, is_dow=True)

    return CompiledCron(
        expr=expr,
        minutes=minutes,
        hours=hours,
        doms=doms,
        months=months,
        dows=dows,
        dom_is_star=dom_star,
        dow_is_star=dow_star,
        dom_has_last=dom_last,
    )

def compute_next_cron_epoch(cron_expr: str, *, now_epoch: float, tz_name: str = "UTC") -> float:
    return compile_cron(cron_expr).next_slot_after(now_epoch, tz_name)