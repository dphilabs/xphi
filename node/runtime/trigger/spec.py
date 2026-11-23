# xphi.node.runtime.trigger.spec
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

from xphi.node.runtime.trigger.cron import compile_cron, resolve_tz

TriggerKind = Literal["manual", "cron", "event", "poll"]
MissedPolicy = Literal["skip", "run_once"]

@dataclass(frozen=True, slots=True)
class TriggerSpec:
    kind: TriggerKind = "manual"
    # Cron fields
    cron_expr: str | None = None
    tz: str = "UTC"
    missed_policy: MissedPolicy = "skip"
    grace_sec: int = 300
    # Event / Poll fields
    source: str | None = None
    dedup_path: str | None = None
    map_in: dict[str, str] = field(default_factory=dict)

    def validate(self, flow_name: str = "") -> None:
        prefix = f"[{flow_name}] " if flow_name else ""
        if self.kind not in ("manual", "cron", "event", "poll"):
            raise ValueError(f"{prefix}Unsupported trigger kind: {self.kind!r}")

        if self.kind == "cron":
            if not self.cron_expr or not self.cron_expr.strip():
                raise ValueError(f"{prefix}Cron trigger requires a non-empty 'cron_expr'.")
            try:
                compile_cron(self.cron_expr)
            except ValueError as exc:
                raise ValueError(f"{prefix}Invalid cron_expr {self.cron_expr!r}: {exc}") from exc

            try:
                resolve_tz(self.tz, strict=True)
            except ValueError as exc:
                raise ValueError(f"{prefix}{exc}") from exc

            if self.grace_sec < 0:
                raise ValueError(f"{prefix}Cron 'grace_sec' must be >= 0, got {self.grace_sec}.")

        elif self.kind == "event":
            if not (self.source and self.source.strip()):
                raise ValueError(f"{prefix}Event trigger requires a non-empty 'source' identifier.")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.kind == "cron":
            out.update({
                "cron_expr": self.cron_expr,
                "tz": self.tz,
                "missed_policy": self.missed_policy,
                "grace_sec": self.grace_sec,
            })
        elif self.kind in ("event", "poll"):
            if self.source is not None:
                out["source"] = self.source
            if self.dedup_path is not None:
                out["dedup_path"] = self.dedup_path
            if self.map_in:
                out["map_in"] = dict(self.map_in)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TriggerSpec:
        policy = data.get("missed_policy", "skip")
        return cls(
            kind=data.get("kind", "manual"),
            cron_expr=data.get("cron_expr"),
            tz=data.get("tz", "UTC"),
            missed_policy="run_once" if policy == "run_once" else "skip",
            grace_sec=int(data.get("grace_sec", 300)),
            source=data.get("source"),
            dedup_path=data.get("dedup_path"),
            map_in=dict(data.get("map_in") or {}),
        )

    @classmethod
    def from_raw(cls, raw: str | dict[str, Any] | TriggerSpec) -> TriggerSpec:
        """약식 문자열('manual', 'poll', '0 9 * * *', '@daily') 또는 dict를 정규 TriggerSpec으로 변환."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, dict):
            return cls.from_dict(raw)
        text = raw.strip()
        if " " in text or text.startswith("@"):
            return cls(kind="cron", cron_expr=text)
        return cls(kind=text)  # type: ignore[arg-type]


DEFAULT_TRIGGERS: tuple[TriggerSpec, ...] = (TriggerSpec(kind="manual"),)