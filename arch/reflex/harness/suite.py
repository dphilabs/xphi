# xphi.arch.reflex.harness.suite
from __future__ import annotations

import ast
import builtins
from collections.abc import Callable, Mapping, Sequence
import inspect
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from xphi.arch.reflex.unit.scanner import (
    DiscoveredUnit,
    UnitAstReport,
    UnitVerificationError,
    analyze_unit_ast,
    detect_unit_cycles,
    find_unit_root,
)

CaseRunner = Callable[[dict[str, Any], dict[str, Any]], str]
SnapshotComparator = Callable[[Mapping[str, Any], Any], str | None]

_SPECIAL_MATCH_KEYS = frozenset({"error_contains", "warning_contains"})
_BASE_EXCEPTIONS: dict[str, type[BaseException]] = {
    "UnitVerificationError": UnitVerificationError,
}


def _json_normalize(value: Any) -> Any:
    """Python 런타임의 tuple/set/frozenset을 JSON 픽스처 비교용 list/dict로 정규화."""
    if isinstance(value, Mapping):
        return {k: _json_normalize(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_json_normalize(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_json_normalize(v) for v in value]
    return value


def unit_from_module(unit: str, module: ModuleType) -> DiscoveredUnit:
    """런타임에 import된 파이썬 모듈을 AST 분석용 `DiscoveredUnit`으로 래핑."""
    src_file = inspect.getsourcefile(module)
    if not src_file:
        raise UnitVerificationError(f"Cannot locate source for module '{module.__name__}'.")
    path = Path(src_file).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    doc = ast.get_docstring(tree) or ""
    flows = tuple(line.strip() for line in doc.splitlines() if line.strip().startswith("@flow"))
    suffix = "/__init__.py" if path.name == "__init__.py" else ".py"
    return DiscoveredUnit(
        unit=unit,
        module_name=module.__name__,
        rel_path=module.__name__.replace(".", "/") + suffix,
        path=path,
        tree=tree,
        flow_tags=flows,
    )


def verify_module_contracts(
    specs: Sequence[tuple[str, ModuleType, set[str]]],
    *,
    require_flow_tags: bool = False,
    error_cls: type[UnitVerificationError] = UnitVerificationError,
) -> dict[str, UnitAstReport]:
    """지정된 코어 모듈들의 AST 규칙(`slots=True`, 어노테이션), 허용 의존성 경계, 순환참조를 일괄 검증."""
    index = {name: unit_from_module(name, mod) for name, mod, _ in specs}
    reports = {
        name: analyze_unit_ast(index[name], index, allowed_peer_units=peers, require_flow_tags=require_flow_tags)
        for name, _, peers in specs
    }

    errors = [f"[{name}] {err}" for name, rep in reports.items() for err in rep.errors]
    for cycle in detect_unit_cycles({name: rep.imported_units for name, rep in reports.items()}):
        errors.append(f"[cycle] {' -> '.join(cycle)}")

    if errors:
        raise error_cls("AST verification failed:\n  - " + "\n  - ".join(errors))
    return reports


def diff_snapshot(
    state: Mapping[str, Any],
    expect: Mapping[str, Any],
    *,
    prefix: str = "",
    strict_keys: bool = True,
    custom_comparators: Mapping[str, SnapshotComparator] | None = None,
) -> list[str]:
    errs: list[str] = []
    head = f"{prefix}." if prefix else ""
    comparators = custom_comparators or {}

    for key, exp in expect.items():
        if key in comparators:
            if msg := comparators[key](state, exp):
                errs.append(f"{head}{msg}")
            continue

        if key in _SPECIAL_MATCH_KEYS:
            field_name = "errors" if key == "error_contains" else "warnings"
            haystack = state.get(field_name) or ()
            needles = [exp] if isinstance(exp, str) else list(exp)
            for needle in needles:
                if not any(needle in str(item) for item in haystack):
                    errs.append(f"{head}{key}: expected substring {needle!r} in {list(haystack)!r}")
            continue

        if strict_keys and key not in state:
            errs.append(f"{head}{key}: unknown snapshot key (available: {sorted(state)})")
            continue

        act = state.get(key)
        if isinstance(exp, Mapping) and isinstance(act, Mapping):
            sub_prefix = f"{prefix}.{key}" if prefix else key
            errs.extend(diff_snapshot(act, exp, prefix=sub_prefix, strict_keys=strict_keys))
            continue

        norm_act = _json_normalize(act)
        norm_exp = _json_normalize(exp)
        if norm_act != norm_exp:
            errs.append(f"{head}{key}: expected {norm_exp!r}, got {norm_act!r}")

    return errs


def _resolve_exception_type(
    err_name: str,
    *,
    extra_errors: Mapping[str, type[BaseException]] | None,
    error_cls: type[AssertionError],
) -> type[BaseException]:
    registry: dict[str, type[BaseException]] = {
        **_BASE_EXCEPTIONS,
        error_cls.__name__: error_cls,
        **(extra_errors or {}),
    }
    err_type = registry.get(err_name) or getattr(builtins, err_name, None)
    if not (isinstance(err_type, type) and issubclass(err_type, BaseException)):
        raise error_cls(f"Unknown exception type: {err_name!r}")
    return err_type


def expect_raises(
    err_name: str,
    items: Sequence[Any],
    fn: Callable[[Any], Any],
    *,
    match: str | None = None,
    extra_errors: Mapping[str, type[BaseException]] | None = None,
    error_cls: type[AssertionError] = UnitVerificationError,
) -> str:
    """주어진 입력 항목들이 모두 지정된 예외(`builtins` 또는 도메인 커스텀 예외)를 발생시키는지 검증."""
    err_type = _resolve_exception_type(err_name, extra_errors=extra_errors, error_cls=error_cls)

    failures: list[str] = []
    for item in items:
        try:
            fn(item)
        except err_type as exc:
            if match and match not in str(exc):
                failures.append(f"expected {err_name} matching {match!r} for {item!r}, got {str(exc)!r}")
            continue
        failures.append(f"expected {err_name} for {item!r}")

    if failures:
        raise error_cls("; ".join(failures))
    return f"{len(items)} rejected ({err_name})"


def resolve_fixture_dir(
    subdir: str,
    *,
    override_dir: str | Path | None = None,
    anchor_file: str | Path | None = None,
) -> Path:
    """명시적 경로 -> 테스터 주변(`fixtures/<subdir>`) -> 상위 `reflexor/fixtures/<subdir>` -> `find_unit_root()` 순으로 탐색."""
    if override_dir is not None:
        return Path(override_dir).resolve()

    parts = ("fixtures", subdir)
    if anchor_file is not None:
        here = Path(anchor_file).resolve().parent
        for base in (here, *here.parents):
            if (cand := base.joinpath(*parts)).is_dir():
                return cand
            if (ref_dir := base / "reflexor").is_dir() and (sub := ref_dir.joinpath(*parts)).is_dir():
                return sub
    return find_unit_root().joinpath(*parts)


def run_fixture_suite(
    fixture_dir: Path,
    runners: CaseRunner | Mapping[str, CaseRunner],
    *,
    case_filter: str | None = None,
    error_cls: type[AssertionError] = UnitVerificationError,
) -> dict[str, str]:
    files = sorted(p for p in fixture_dir.rglob("*.json") if not p.name.startswith("."))
    if not files:
        raise error_cls(f"No fixtures found in '{fixture_dir}'.")

    scenarios: dict[str, str] = {}
    failures: list[str] = []

    for path in files:
        rel_parts = path.relative_to(fixture_dir).with_suffix("").parts
        rel_key = ".".join(rel_parts)  # 예: 'mcp.unwrap' 또는 'mcp'

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{rel_key}.json: invalid JSON at line {exc.lineno}: {exc.msg}")
            continue

        base_defaults = data.get("defaults") or {}
        if "suites" not in data:
            sections: list[tuple[str, str, Any]] = [(rel_key, path.stem, data)]
        elif isinstance(raw_suites := data["suites"], Mapping):
            parent = ".".join(rel_parts[:-1])
            sections = [(f"{parent}.{k}" if parent else k, k, sec) for k, sec in raw_suites.items()]
        else:
            failures.append(f"{rel_key}.json: 'suites' must be a mapping")
            continue

        for sec_key, short_key, sec in sections:
            runner = runners if callable(runners) else (runners.get(sec_key) or runners.get(short_key))
            if runner is None:
                expected = sorted(runners) if isinstance(runners, Mapping) else []
                failures.append(f"{rel_key}.json: no runner bound for {sec_key!r} (expected: {expected})")
                continue

            if isinstance(sec, Mapping):
                defaults = base_defaults if sec is data else {**base_defaults, **(sec.get("defaults") or {})}
                cases = sec.get("cases", ())
                ctx = data if sec is data else {**data, **{k: v for k, v in sec.items() if k not in ("cases", "defaults")}}
            else:
                defaults, cases, ctx = base_defaults, sec, data

            for idx, raw_case in enumerate(cases):
                case = {**defaults, **raw_case}
                case_id = f"{sec_key}.{case.get('id', f'case_{idx}')}"
                if case_filter and case_filter not in case_id:
                    continue
                try:
                    scenarios[case_id] = runner(case, ctx)
                except Exception as exc:
                    failures.append(f"{case_id}: {type(exc).__name__}: {exc}")

    if failures:
        raise error_cls(f"{len(failures)} fixture case(s) failed:\n  - " + "\n  - ".join(failures))
    return scenarios