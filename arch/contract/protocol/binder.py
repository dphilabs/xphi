# xphi.arch.contract.protocol.binder
from __future__ import annotations

import datetime as _dt
import decimal
import enum
import inspect
import json
import re
import types
import uuid
from pathlib import PurePath
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union, get_args, get_origin, get_type_hints

_PARAM_DOC_RE = re.compile(
    r"^(?:[:@]param\s+|-\s*|\*\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\([^)]*\))?\s*:\s*(.+)$"
)
_ARGS_HEADERS = frozenset({"args:", "arguments:", "parameters:", "params:"})
# Headers that close an Args block (Google / NumPy style).
_SECTION_HEADERS = frozenset({
    "returns:", "return:", "yields:", "raises:", "examples:", "example:", "notes:", "note:", "see also:",
})
_SPHINX_PREFIXES = (":", "@", "-", "*")
_META_PARAMS = frozenset({"meta", "_meta"})
_UNION_ORIGINS = (Union, types.UnionType)  # typing.Optional[X] and PEP 604 `X | None`
_ARRAY_ORIGINS = (list, tuple, set, frozenset)
_TRUE = frozenset({"true", "1", "yes", "y", "on"})
_FALSE = frozenset({"false", "0", "no", "n", "off"})

class ToolError(Exception):
    """Content-level MCP error (returned as isError: True)."""
    pass

def tool(func: Optional[Callable] = None, *, name: Optional[str] = None) -> Callable:
    """Mark a method as an MCP tool. Supports `@tool` and `@tool(name="...")`."""
    def decorator(fn: Callable) -> Callable:
        setattr(fn, "__mcp_tool_name__", name or fn.__name__.removeprefix("tool_"))
        return fn
    return decorator(func) if func is not None else decorator


# --------------------------------------------------------------------------- docstring

def _parse_docstring(doc: str) -> tuple[str, Dict[str, str]]:
    """Extract (summary, {param: description}) from a Google / Sphinx / NumPy-ish docstring."""
    if not doc:
        return "", {}

    summary_lines: List[str] = []
    param_descs: Dict[str, str] = {}
    state = "summary"  # summary | args | other
    last: Optional[str] = None
    last_indent = 0

    for raw_line in doc.strip().splitlines():
        line = raw_line.strip()
        if not line:
            last = None  # blank line ends a continuation
            continue

        lowered = line.lower()
        if lowered in _ARGS_HEADERS:
            state, last = "args", None
            continue
        if lowered in _SECTION_HEADERS:
            state, last = "other", None
            continue

        indent = len(raw_line) - len(raw_line.lstrip())

        # Indented line under a param: continuation of its description.
        if last is not None and indent > last_indent:
            param_descs[last] += " " + line
            continue

        match = _PARAM_DOC_RE.match(line)
        if match and (state == "args" or line.startswith(_SPHINX_PREFIXES)):
            last, last_indent = match.group(1), indent
            param_descs[last] = match.group(2).strip()
        elif state == "args" and last is not None:
            param_descs[last] += " " + line  # lenient unindented continuation
        elif state == "summary" and not param_descs:
            summary_lines.append(line)

    return " ".join(summary_lines), param_descs

def _unwrap_optional(hint: Any) -> Any:
    """Optional[X] / X | None -> X; anything else unchanged."""
    if get_origin(hint) in _UNION_ORIGINS:
        non_none = [a for a in get_args(hint) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return hint

def _literal_schema(values: List[Any]) -> Dict[str, Any]:
    if values and all(isinstance(v, bool) for v in values):
        return {"type": "boolean", "enum": values}
    if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return {"type": "integer", "enum": values}
    if values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return {"type": "number", "enum": values}
    if all(isinstance(v, str) for v in values):
        return {"type": "string", "enum": values}
    return {"enum": values}  # mixed literal: no single JSON type

def _array_item_type(origin: Any, args: Tuple[Any, ...]) -> Any:
    if not args:
        return Any
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return args[0]  # Tuple[X, ...]
        return args[0] if len(set(args)) == 1 else Any  # heterogeneous tuple
    return args[0]

def _is_model(py_type: Any) -> bool:
    """Pydantic v2 model class (duck-typed; pydantic stays optional)."""
    return isinstance(py_type, type) and hasattr(py_type, "model_json_schema") and hasattr(py_type, "model_validate")

def _hoist_defs(fragment: Any, root_defs: Dict[str, Any]) -> None:
    """Move nested `$defs` to the inputSchema root so `#/$defs/...` refs resolve."""
    if isinstance(fragment, dict):
        defs = fragment.pop("$defs", None)
        if defs:
            root_defs.update(defs)
        for value in fragment.values():
            _hoist_defs(value, root_defs)
    elif isinstance(fragment, list):
        for value in fragment:
            _hoist_defs(value, root_defs)

def _py_type_to_json_schema(py_type: Any) -> Dict[str, Any]:
    """Convert a Python type hint to a JSON Schema fragment."""
    if py_type is inspect.Parameter.empty or py_type is Any:
        return {"type": "string"}  # legacy default for untyped params

    if _is_model(py_type):
        return py_type.model_json_schema()

    origin = get_origin(py_type)
    args = get_args(py_type)

    if origin in _UNION_ORIGINS:
        non_none = [a for a in args if a is not type(None)]
        return _py_type_to_json_schema(non_none[0]) if len(non_none) == 1 else {"type": "string"}

    if origin is Literal:
        return _literal_schema(list(args))

    if py_type in _ARRAY_ORIGINS or origin in _ARRAY_ORIGINS:
        item = _array_item_type(origin, args)
        schema: Dict[str, Any] = {"type": "array"}
        if item is not Any:
            schema["items"] = _py_type_to_json_schema(item)
        if (origin or py_type) in (set, frozenset):
            schema["uniqueItems"] = True
        return schema

    if origin is dict or py_type is dict:
        return {"type": "object"}

    return dict({
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
    }.get(py_type, {"type": "string"}))

def _json_default(value: Any) -> Any:
    """json.dumps fallback for values common in tool results (DB rows, paths, ids)."""
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return list(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (decimal.Decimal, uuid.UUID, PurePath)):
        return str(value)  # str keeps Decimal precision
    return str(value)

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)

def _short_repr(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."

def _coerce(param_name: str, hint: Any, val: Any) -> Any:
    """Coerce a JSON argument to a scalar hint. Non-scalar hints pass through untouched."""
    if val is None:
        return None
    hint = _unwrap_optional(hint)
    if _is_model(hint):
        # pydantic ValidationError subclasses ValueError -> invalid-argument path, detail preserved.
        return val if isinstance(val, hint) else hint.model_validate(val)
    try:
        if hint is bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, int) and val in (0, 1):
                return bool(val)
            if isinstance(val, str):
                lowered = val.strip().lower()
                if lowered in _TRUE:
                    return True
                if lowered in _FALSE:
                    return False
            raise ValueError
        if hint is int:
            if isinstance(val, bool):
                return val  # legacy: bools are left as-is
            if isinstance(val, float):
                if not val.is_integer():
                    raise ValueError  # reject silent truncation
                return int(val)
            if isinstance(val, str):
                text = val.strip()
                try:
                    return int(text)
                except ValueError:
                    as_float = float(text)  # accept "5.0"
                    if not as_float.is_integer():
                        raise
                    return int(as_float)
            return int(val)
        if hint is float:
            if isinstance(val, bool):
                return val  # legacy: bools are left as-is
            return float(val)
        if hint is str:
            if isinstance(val, (dict, list)):
                return _dumps(val)  # JSON, not Python repr
            return str(val)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid value for parameter '{param_name}': expected {getattr(hint, '__name__', hint)}, "
            f"got {_short_repr(val)}"
        ) from None
    return val

class ToolBinder:
    """Scan `@tool` methods (or legacy `tool_<name>` methods) and build schemas / dispatch kwargs."""

    def __init__(self, target: Any) -> None:
        self.bound_tools: Dict[str, Callable] = {}
        self.tool_schemas: List[Dict[str, Any]] = []
        self._specs: Dict[Any, Tuple[inspect.Signature, Dict[str, Any]]] = {}

        for attr_name in dir(target):
            if attr_name.startswith("_"):
                continue
            # Skip properties without evaluating them (avoids side effects / errors at bind time).
            if isinstance(inspect.getattr_static(target, attr_name, None), property):
                continue
            try:
                member = getattr(target, attr_name, None)
            except Exception:
                continue
            if not callable(member):
                continue

            explicit_name = getattr(member, "__mcp_tool_name__", None)
            if explicit_name:
                tool_name = explicit_name
            elif attr_name.startswith("tool_"):
                tool_name = attr_name[5:]
            else:
                continue

            # First match in dir() order wins (unchanged legacy behavior).
            if tool_name not in self.bound_tools:
                self.bound_tools[tool_name] = member
                self.tool_schemas.append(self._build_tool_schema(tool_name, member))

    def _spec(self, func: Callable) -> Tuple[inspect.Signature, Dict[str, Any]]:
        """Cached (signature, resolved type hints) per underlying function."""
        key = getattr(func, "__func__", func)
        spec = self._specs.get(key)
        if spec is None:
            try:
                type_hints = get_type_hints(func)
            except Exception:
                type_hints = {}
            spec = (inspect.signature(func), type_hints)
            self._specs[key] = spec
        return spec

    def _build_tool_schema(self, tool_name: str, func: Callable) -> Dict[str, Any]:
        sig, type_hints = self._spec(func)
        summary, param_descs = _parse_docstring(inspect.getdoc(func) or "")
        properties: Dict[str, Any] = {}
        required: List[str] = []
        root_defs: Dict[str, Any] = {}

        for param_name, param in sig.parameters.items():
            if param_name == "self" or param_name in _META_PARAMS:
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue

            prop_schema = _py_type_to_json_schema(type_hints.get(param_name, param.annotation))
            _hoist_defs(prop_schema, root_defs)

            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            elif param.default is not None:
                # Normalize defaults (tuple, Path, Enum, ...) so the schema stays JSON-serializable.
                prop_schema["default"] = json.loads(_dumps(param.default))

            if param_name in param_descs:
                prop_schema["description"] = param_descs[param_name]

            properties[param_name] = prop_schema

        schema_entry: Dict[str, Any] = {
            "name": tool_name,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }
        if root_defs:
            schema_entry["inputSchema"]["$defs"] = root_defs
        if summary:
            schema_entry["description"] = summary

        return schema_entry

    def _prepare_call_kwargs(
        self, func: Callable, arguments: Dict[str, Any], meta: Dict[str, Any]
    ) -> Dict[str, Any]:
        sig, type_hints = self._spec(func)
        call_kwargs: Dict[str, Any] = {}

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            if param_name in _META_PARAMS:
                call_kwargs[param_name] = meta
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue

            if param_name in arguments:
                hint = type_hints.get(param_name, param.annotation)
                call_kwargs[param_name] = _coerce(param_name, hint, arguments[param_name])
            elif param.default is not inspect.Parameter.empty:
                call_kwargs[param_name] = param.default
            else:
                raise ValueError(f"Missing required parameter: '{param_name}'")

        return call_kwargs

    @staticmethod
    def _wrap_mcp_result(result: Any, is_error: bool = False) -> Dict[str, Any]:
        if isinstance(result, dict) and "content" in result and isinstance(result["content"], list):
            return result
        text = result if isinstance(result, str) else _dumps(result)
        return {"content": [{"type": "text", "text": text}], "isError": is_error}