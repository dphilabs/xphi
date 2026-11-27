# xphi.arch.tester.protocol.binder
## @lineage: xphi.node.tester.protocol.binder
from __future__ import annotations

import datetime as dt
import decimal
import importlib.util
import inspect
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
import uuid

from xphi.arch.contract.protocol import binder as B
from xphi.arch.reflex.harness.suite import (
    CaseRunner,
    diff_snapshot,
    expect_raises,
    resolve_fixture_dir,
    run_fixture_suite,
)

class BinderContractError(AssertionError):
    """Raised when ToolBinder schema synthesis, argument coercion, result wrapping or docstring parsing drifts."""

_TYPE_NS: dict[str, Any] = {
    "Any": Any, "Optional": Optional, "Union": Union, "Literal": Literal,
    "List": List, "Dict": Dict, "Tuple": Tuple,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set, "frozenset": frozenset,
    "None": None, "Path": Path,
}
_EXTRA_ERRORS: dict[str, type[BaseException]] = {"ToolError": B.ToolError}
_KW_ONLY = inspect.Parameter.KEYWORD_ONLY

def _decode(value: Any) -> Any:
    """Decode tagged JSON values (e.g. {"$date": "2026-11-25"}) into runtime Python types."""
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        ((tag, raw),) = value.items()
        if (decoder := _TAGS.get(tag)) is not None:
            return decoder(raw)
    return {k: _decode(v) for k, v in value.items()}


_TAGS: dict[str, Any] = {
    "$date": dt.date.fromisoformat,
    "$datetime": dt.datetime.fromisoformat,
    "$time": dt.time.fromisoformat,
    "$decimal": decimal.Decimal,
    "$uuid": uuid.UUID,
    "$path": PurePosixPath,
    "$bytes": bytes.fromhex,
    "$set": lambda raw: set(_decode(raw)),
    "$tuple": lambda raw: tuple(_decode(raw)),
}

def _encode(value: Any) -> Any:
    """Runtime value -> JSON-comparable snapshot value."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value

def _is_json(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False

def _eval_type(expr: str, ns: dict[str, Any]) -> Any:
    return eval(expr, {"__builtins__": {}}, ns)  # noqa: S307 - restricted namespace, repo-owned fixtures

def _missing_requirements(case: dict[str, Any]) -> list[str]:
    return [mod for mod in case.get("requires", ()) if importlib.util.find_spec(mod) is None]

def _file_namespace(ctx: dict[str, Any]) -> dict[str, Any]:
    """Type namespace for one fixture file: base types + pydantic models declared under "models"."""
    ns = dict(_TYPE_NS)
    if not (spec := ctx.get("models")) or importlib.util.find_spec("pydantic") is None:
        return ns
    from pydantic import Field, create_model

    for model_name, fields in spec.items():
        defs = {}
        for field_name, field in fields.items():
            default = _decode(field["default"]) if "default" in field else ...
            defs[field_name] = (_eval_type(field["type"], ns), Field(default, **field.get("constraints", {})))
        ns[model_name] = create_model(model_name, **defs)
    return ns

def _make_method(spec: dict[str, Any], ns: dict[str, Any]) -> Any:
    """Build a tool method from a JSON signature spec (no fixture code is executed)."""
    params = [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    annotations: dict[str, Any] = {}
    for p in spec.get("params", ()):
        default = _decode(p["default"]) if "default" in p else inspect.Parameter.empty
        params.append(inspect.Parameter(p["name"], _KW_ONLY, default=default))
        if "type" in p:
            annotations[p["name"]] = _eval_type(p["type"], ns)

    def method(self: Any, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    method.__name__ = spec.get("attr", spec["name"])
    method.__doc__ = spec.get("doc")
    method.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    method.__annotations__ = annotations
    if spec.get("style", "decorator") == "decorator":
        B.tool(method, name=spec.get("tool_name"))
    return method

def _raising_property(name: str) -> property:
    def getter(self: Any) -> Any:
        raise RuntimeError(f"property '{name}' evaluated at bind time")
    return property(getter)

def _make_target(tools: list[dict[str, Any]], ns: dict[str, Any], raising_properties: Any = ()) -> Any:
    attrs: dict[str, Any] = {spec.get("attr", spec["name"]): _make_method(spec, ns) for spec in tools}
    attrs.update({name: _raising_property(name) for name in raising_properties})
    return type("FixtureWorker", (), attrs)()

def _assert_snapshot(snap: dict[str, Any], case: dict[str, Any], comparators: dict[str, Any] | None = None) -> None:
    if errs := diff_snapshot(snap, case.get("expect") or {}, custom_comparators=comparators):
        raise BinderContractError("; ".join(errs))

def _text_contains(state: dict[str, Any], needles: Any) -> str | None:
    text = state.get("text") or ""
    missing = [n for n in ([needles] if isinstance(needles, str) else needles) if n not in text]
    return f"text_contains: missing {missing!r} in {text!r}" if missing else None

def _text_excludes(state: dict[str, Any], needles: Any) -> str | None:
    text = state.get("text") or ""
    found = [n for n in ([needles] if isinstance(needles, str) else needles) if n in text]
    return f"text_excludes: found {found!r} in {text!r}" if found else None

def _run_schema(case: dict[str, Any], ctx: dict[str, Any]) -> str:
    if missing := _missing_requirements(case):
        return f"skipped (missing: {', '.join(missing)})"
    target = _make_target(case["tools"], _file_namespace(ctx), case.get("raising_properties", ()))

    if err_name := case.get("expect_bind_error"):
        return expect_raises(err_name, [target], B.ToolBinder, match=case.get("error_match"),
                             error_cls=BinderContractError)

    binder = B.ToolBinder(target)
    schemas = {s["name"]: s for s in binder.tool_schemas}
    snap = {
        "names": sorted(schemas),
        "descriptions": {n: s.get("description") for n, s in schemas.items()},
        "required": {n: s["inputSchema"]["required"] for n, s in schemas.items()},
        "properties": {n: s["inputSchema"]["properties"] for n, s in schemas.items()},
        # Exact key sets per property: diff_snapshot compares nested mappings as subsets.
        "property_keys": {
            n: {p: sorted(v) for p, v in s["inputSchema"]["properties"].items()} for n, s in schemas.items()
        },
        "defs": {n: sorted(s["inputSchema"].get("$defs", {})) for n, s in schemas.items()},
        "json_serializable": _is_json(binder.tool_schemas),
    }
    _assert_snapshot(snap, case)
    return f"{len(schemas)} tool(s)"


def _run_coerce(case: dict[str, Any], ctx: dict[str, Any]) -> str:
    if missing := _missing_requirements(case):
        return f"skipped (missing: {', '.join(missing)})"
    name = case.get("param", "value")
    param: dict[str, Any] = {"name": name, "type": case["hint"]}
    if "default" in case:
        param["default"] = case["default"]

    binder = B.ToolBinder(_make_target([{"name": "probe", "params": [param]}], _file_namespace(ctx)))
    fn = binder.bound_tools["probe"]
    args = {name: _decode(case["value"])} if "value" in case else {}

    def call(arguments: dict[str, Any]) -> dict[str, Any]:
        return binder._prepare_call_kwargs(fn, arguments, case.get("meta") or {})

    if err_name := case.get("expect_error"):
        return expect_raises(err_name, [args], call, match=case.get("error_match"),
                             extra_errors=_EXTRA_ERRORS, error_cls=BinderContractError)

    value = call(args)[name]
    snap = {"value": _encode(value), "type": type(value).__name__}  # "type" separates True from 1
    _assert_snapshot(snap, case)
    return f"{case['hint']} <- {case.get('value', '<default>')!r}"

def _run_wrap(case: dict[str, Any], ctx: dict[str, Any]) -> str:
    result = _decode(case["result"])
    wrapped = B.ToolBinder._wrap_mcp_result(result, is_error=case.get("is_error", False))
    content = wrapped.get("content") or []
    text = content[0].get("text") if content else None
    try:
        parsed = json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        parsed = None
    snap = {
        "passthrough": wrapped is result,
        "is_error": wrapped.get("isError"),
        "text": text,
        "parsed": parsed,
    }
    _assert_snapshot(snap, case, {"text_contains": _text_contains, "text_excludes": _text_excludes})
    return "passthrough" if snap["passthrough"] else f"{len(text or '')} chars"

def _run_docstring(case: dict[str, Any], ctx: dict[str, Any]) -> str:
    summary, params = B._parse_docstring(case["doc"])
    snap = {"summary": summary, "params": params, "param_names": sorted(params)}
    _assert_snapshot(snap, case)
    return f"{len(params)} param(s)"

class BinderTester:
    """ToolBinder contract verifier: public API surface + JSON fixture scenarios."""

    FIXTURE_SUBDIR = "mcp/binder"
    RUNNERS: dict[str, CaseRunner] = {
        "schema": _run_schema,
        "coerce": _run_coerce,
        "wrap": _run_wrap,
        "docstring": _run_docstring,
    }

    @classmethod
    def verify_api(cls) -> dict[str, str]:
        """Backward-compatible public surface that workers and the base protocol rely on."""
        failures: list[str] = []

        def check(label: str, cond: bool) -> None:
            if not cond:
                failures.append(label)

        check("ToolError subclasses Exception", issubclass(B.ToolError, Exception))

        @B.tool
        def tool_plain(self: Any) -> None: ...

        @B.tool(name="renamed")
        def other(self: Any) -> None: ...

        check("@tool strips legacy prefix", getattr(tool_plain, "__mcp_tool_name__", None) == "plain")
        check("@tool(name=...) sets explicit name", getattr(other, "__mcp_tool_name__", None) == "renamed")

        empty = B.ToolBinder(type("Empty", (), {})())
        check("bound_tools / tool_schemas attributes", empty.bound_tools == {} and empty.tool_schemas == [])
        for attr in ("_build_tool_schema", "_prepare_call_kwargs"):
            check(f"ToolBinder.{attr} exists", callable(getattr(empty, attr, None)))
        check("_wrap_mcp_result is a staticmethod",
              isinstance(inspect.getattr_static(B.ToolBinder, "_wrap_mcp_result"), staticmethod))
        check("_wrap_mcp_result(result, is_error) signature",
              list(inspect.signature(B.ToolBinder._wrap_mcp_result).parameters) == ["result", "is_error"])

        if failures:
            raise BinderContractError("Binder API verification failed:\n  - " + "\n  - ".join(failures))
        return {"api": "ok"}

    @classmethod
    def run_fixtures(
        cls,
        *,
        fixture_dir: str | Path | None = None,
        case_filter: str | None = None,
    ) -> dict[str, str]:
        root = resolve_fixture_dir(cls.FIXTURE_SUBDIR, override_dir=fixture_dir, anchor_file=__file__)
        return run_fixture_suite(root, cls.RUNNERS, case_filter=case_filter, error_cls=BinderContractError)

    @classmethod
    def run_all(
        cls,
        *,
        fixture_dir: str | Path | None = None,
        case_filter: str | None = None,
    ) -> dict[str, Any]:
        api = cls.verify_api()
        scenarios = cls.run_fixtures(fixture_dir=fixture_dir, case_filter=case_filter)
        return {"api": api, "scenarios": scenarios}

if __name__ == "__main__":
    case_filter = sys.argv[1] if len(sys.argv) > 1 else None
    sys.stdout.write(json.dumps(BinderTester.run_all(case_filter=case_filter), indent=2, ensure_ascii=False) + "\n")