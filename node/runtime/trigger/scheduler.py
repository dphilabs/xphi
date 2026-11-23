# xphi.node.runtime.trigger.scheduler
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time

from xphi.node.runtime.reflex.manifest import ReflexManifest
from xphi.node.runtime.trigger.spec import TriggerSpec
from xphi.node.runtime.trigger.cron import compute_next_cron_epoch, resolve_tz

_DAILY_MACROS = frozenset({"@daily", "@midnight"})

@dataclass(frozen=True, slots=True)
class ScheduleVerdict:
    auto_runnable: bool
    manual_only: bool
    blocking_runtime_slots: tuple[str, ...]
    next_run_epoch: float | None
    display_label: str
    reason: str = ""


def parse_iso_epoch(value: str) -> float:
    """ISO-8601 문자열('Z' / '+09:00' / naive)을 UTC 기준 epoch(float)으로 변환."""
    dt = datetime.fromisoformat(value)
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)).timestamp()


def format_utc_iso(epoch: float | None) -> str | None:
    """epoch(float)을 정규 UTC ISO-8601('YYYY-MM-DDTHH:MM:SSZ') 문자열로 포맷."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_schedule_label(cron_expr: str | None, next_epoch: float | None, tz_name: str, fallback_kind: str) -> str:
    """L1 목록용 스케줄 라벨 포맷 (예: '⏱ 매일 23:00', '⏱ 12/01 09:00')."""
    if cron_expr:
        norm = cron_expr.strip()
        if norm.lower() in _DAILY_MACROS:
            return "⏱ 매일 00:00"
        parts = norm.split()
        if len(parts) == 5 and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
            if parts[0].isdigit() and parts[1].isdigit():
                return f"⏱ 매일 {int(parts[1]):02d}:{int(parts[0]):02d}"
    if next_epoch is not None:
        dt = datetime.fromtimestamp(next_epoch, tz=resolve_tz(tz_name, strict=False))
        return f"⏱ {dt.strftime('%m/%d %H:%M')}"
    return f"⏱ {fallback_kind}"


class SchedulePlanner:
    def evaluate(
        self,
        manifest: ReflexManifest,
        triggers: tuple[TriggerSpec, ...],
        *,
        now_epoch: float | None = None,
    ) -> ScheduleVerdict:
        now = now_epoch if now_epoch is not None else time.time()
        rt_slots = manifest.runtime_slot_names()
        auto_triggers = [t for t in triggers if t.kind != "manual"]

        if rt_slots:
            slot_list = ", ".join(rt_slots)
            return ScheduleVerdict(
                auto_runnable=False,
                manual_only=True,
                blocking_runtime_slots=rt_slots,
                next_run_epoch=None,
                display_label="수동 전용",
                reason=f"자동 실행: 불가 ⓘ '실행 시' 슬롯 {len(rt_slots)}개 ({slot_list})",
            )

        if not auto_triggers:
            return ScheduleVerdict(
                auto_runnable=False,
                manual_only=True,
                blocking_runtime_slots=(),
                next_run_epoch=None,
                display_label="수동 전용",
                reason="자동 실행 트리거 미설정 (manual 전용)",
            )

        earliest_epoch: float | None = None
        chosen_trigger = auto_triggers[0]

        for trig in auto_triggers:
            if trig.cron_expr:
                tz_name = trig.tz or "UTC"
                candidate = compute_next_cron_epoch(trig.cron_expr, now_epoch=now, tz_name=tz_name)
                if earliest_epoch is None or candidate < earliest_epoch:
                    earliest_epoch = candidate
                    chosen_trigger = trig

        return ScheduleVerdict(
            auto_runnable=True,
            manual_only=False,
            blocking_runtime_slots=(),
            next_run_epoch=earliest_epoch,
            display_label=_format_schedule_label(
                chosen_trigger.cron_expr,
                earliest_epoch,
                chosen_trigger.tz or "UTC",
                chosen_trigger.kind,
            ),
            reason="자동 실행 가능 ('실행 시' 슬롯 0개)",
        )