# xphi.arch.model.content.intake
## @lineage: fiber.node.runbook.inspect.intake
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from xphi.arch.model.content.artifact import ArtifactRef, ArtifactStore
from xphi.node.runtime.reflex.manifest import ReflexManifest, SlotPolicy


@dataclass(frozen=True, slots=True)
class ContentSchema:
    name: str
    required_columns: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)
    non_empty_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContentReport:
    valid: bool
    normalized_rows: list[dict[str, Any]]
    matched_columns: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()
    alias_applied: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def parse_source_uri(uri: str) -> tuple[str | None, str | None]:
    """URI에서 정규화된 스킴('file' -> 'local')과 말단 파일명을 추출."""
    if "://" not in uri:
        return None, None
    raw_scheme, rest = uri.split("://", 1)
    scheme = "local" if raw_scheme == "file" else raw_scheme
    tail = rest.rstrip("/").rsplit("/", 1)[-1] or None
    return scheme, tail


class SlotVerifier:
    """Pre-run slot input resolver and tabular `ContentSchema` validator."""

    def __init__(
        self,
        artifacts: ArtifactStore | None = None,
        *,
        schemas: dict[str, ContentSchema] | None = None,
    ) -> None:
        self.artifacts = artifacts or ArtifactStore()
        self._schemas: dict[str, ContentSchema] = dict(schemas or {})

    def register_schema(self, schema: ContentSchema) -> None:
        self._schemas[schema.name] = schema

    def inspect_tabular(self, rows: list[dict[str, Any]], schema_name: str) -> ContentReport:
        schema = self._schemas.get(schema_name)
        if schema is None:
            return ContentReport(valid=True, normalized_rows=rows)

        applied_aliases: dict[str, str] = {}
        normalized: list[dict[str, Any]] = []
        observed_cols: set[str] = set()

        for row in rows:
            new_row: dict[str, Any] = {}
            for col, val in row.items():
                canonical = schema.aliases.get(col, col)
                if canonical != col:
                    applied_aliases[col] = canonical
                new_row[canonical] = val
                observed_cols.add(canonical)
            normalized.append(new_row)

        matched = tuple(c for c in schema.required_columns if c in observed_cols)
        missing = tuple(c for c in schema.required_columns if c not in observed_cols)
        if missing and rows:
            return ContentReport(
                valid=False,
                normalized_rows=normalized,
                matched_columns=matched,
                missing_columns=missing,
                alias_applied=applied_aliases,
                warnings=(f"필수 컬럼 누락: {', '.join(missing)}",),
            )

        check_cols = schema.non_empty_columns or schema.required_columns
        warns = tuple(
            f"빈 {col} {cnt}"
            for col in check_cols
            if (cnt := sum(1 for r in normalized if r.get(col) in (None, ""))) > 0
        )
        return ContentReport(
            valid=True,
            normalized_rows=normalized,
            matched_columns=matched,
            alias_applied=applied_aliases,
            warnings=warns,
        )

    @staticmethod
    def _resolve_slot_input(pol: SlotPolicy, incoming: dict[str, Any]) -> tuple[Any, str, str | None, list[str]]:
        if pol.mode == "fixed":
            if pol.fixed_value is None:
                return None, f"fixed://{pol.name}", None, [f"Fixed slot '{pol.name}' has no value."]
            return pol.fixed_value, f"fixed://{pol.name}", None, []

        raw_val = incoming.get(pol.name, pol.source_uri if pol.mode == "source" else None)
        if raw_val is None:
            return None, f"mem://{pol.name}", None, []

        if isinstance(raw_val, dict) and "uri" in raw_val:
            uri, fname = str(raw_val["uri"]), raw_val.get("filename")
            data = raw_val.get("rows", raw_val.get("data", raw_val))
        elif isinstance(raw_val, str) and "://" in raw_val:
            uri, fname, data = raw_val, None, raw_val
        else:
            return raw_val, f"mem://{pol.name}", None, []

        scheme, parsed_name = parse_source_uri(uri)
        allowed = set(pol.allowed_schemes)
        if scheme is not None and scheme not in allowed and raw_val is not pol.fixed_value:
            return None, uri, None, [f"Slot '{pol.name}' scheme '{scheme}://' not in allowed {sorted(allowed)}."]

        if scheme == "local" and isinstance(data, str) and data.startswith("file:///"):
            local_path = Path(data.removeprefix("file://"))
            if local_path.is_file() and local_path.suffix == ".json":
                data = json.loads(local_path.read_text(encoding="utf-8"))

        return data, uri, (fname or parsed_name), []

    def verify(
        self,
        raw_inputs: dict[str, Any] | None,
        manifest: ReflexManifest,
    ) -> tuple[dict[str, Any], dict[str, ArtifactRef], bool, list[str]]:
        incoming = dict(raw_inputs or {})
        resolved: dict[str, Any] = {k: v for k, v in incoming.items() if k not in manifest.slots}
        io_refs: dict[str, ArtifactRef] = {}
        warnings: list[str] = []

        for name, pol in manifest.slots.items():
            payload, uri, fname, errs = self._resolve_slot_input(pol, incoming)
            if errs:
                return {}, {}, False, errs
            if payload is None:
                continue

            if pol.contract_ref and isinstance(payload, list) and all(isinstance(r, dict) for r in payload):
                rep = self.inspect_tabular(payload, pol.contract_ref)
                if not rep.valid:
                    return {}, {}, False, list(rep.warnings)
                payload = rep.normalized_rows
                warnings.extend(rep.warnings)

            resolved[name] = payload
            io_refs[name] = self.artifacts.put(payload, source_uri=uri, filename=fname)

        return resolved, io_refs, True, warnings