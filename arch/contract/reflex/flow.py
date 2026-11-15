# xphi.arch.contract.reflex.flow
from __future__ import annotations
import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from inspect import signature
from typing import Any, TypeAlias, get_type_hints

NodeAction: TypeAlias = Callable[..., dict[str, Any] | None] | str

ACCOUNT_SLOT = "account"
SCHEMES: tuple[str, ...] = ("py", "exec", "mcp", "llm", "flow")
BASE_SHAPES: frozenset[str] = frozenset({"val", "text", "num", "bool", "obj", "file"})

_SHAPE_RE = re.compile(r"^(?P<base>[a-z]+)(?:<(?P<ext>[A-Za-z0-9_.+-]+)>)?(?P<list>\[\])?$")
_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][\w.]*)(?P<opt>\?)?$")

@dataclass(frozen=True, slots=True)
class AuthReq:
    """User-app credential requirement metadata (`provider[:scope]`)."""
    provider: str
    scope: str | None = None

    @property
    def delegated(self) -> bool:
        """True when auth is handled by an MCP server connection."""
        return self.provider == "mcp"

    def is_satisfied_by(self, available: set[str]) -> bool:
        full = str(self)
        if full in available:
            return True
        return not self.delegated and self.provider in available

    @classmethod
    def parse(cls, token: str) -> AuthReq:
        provider, _, scope = token.strip().partition(":")
        if not provider:
            raise ValueError(f"Invalid auth requirement '{token.strip()}'.")
        return cls(provider=provider.strip(), scope=scope.strip() or None)

    @classmethod
    def parse_list(cls, body: str) -> tuple[AuthReq, ...]:
        tokens = [t.strip() for t in body.split(",") if t.strip()]
        return tuple(cls.parse(t) for t in tokens)

    @classmethod
    def for_mcp_target(cls, target: str) -> AuthReq:
        """Implicit requirement of `mcp(server/tool)`: the `server` connection."""
        server = target.strip().partition("/")[0]
        if not server:
            raise ValueError(f"Invalid mcp target '{target.strip()}', expected 'server/tool'.")
        return cls(provider="mcp", scope=server)

    def __str__(self) -> str:
        return f"{self.provider}:{self.scope}" if self.scope else self.provider

@dataclass(frozen=True, slots=True)
class Shape:
    """Optional key shape. Unannotated keys are `val` (v1 behavior)."""

    base: str = "val"
    ext: str | None = None
    is_list: bool = False

    @classmethod
    def parse(cls, token: str) -> Shape:
        m = _SHAPE_RE.match(token.strip())
        if not m or m["base"] not in BASE_SHAPES:
            raise ValueError(f"Unknown shape '{token.strip()}'.")
        if m["ext"] and m["base"] != "file":
            raise ValueError(f"Shape qualifier '<{m['ext']}>' is only allowed on 'file'.")
        return cls(base=m["base"], ext=m["ext"], is_list=bool(m["list"]))

    def element(self) -> Shape:
        return Shape(base=self.base, ext=self.ext, is_list=False)

    def collected(self) -> Shape:
        return Shape(base=self.base, ext=self.ext, is_list=True)

    def compat(self, consumer: Shape) -> str:
        """Producer(self) -> consumer: 'ok' | 'warn' | 'error'."""
        if (consumer.base == "val" and not consumer.is_list) or (self.base == "val" and not self.is_list):
            return "ok"
        if self.is_list != consumer.is_list:
            return "error"
        if "val" in (self.base, consumer.base):
            return "ok"
        if self.base != consumer.base:
            return "error"
        if consumer.ext is None or self.ext == consumer.ext:
            return "ok"
        return "warn" if self.ext is None else "error"

    def __str__(self) -> str:
        ext = f"<{self.ext}>" if self.ext else ""
        return f"{self.base}{ext}{'[]' if self.is_list else ''}"

@dataclass(frozen=True, slots=True)
class ArgSpec:
    """Parsed `in(...)` / `cfg(...)` / `out(...)` clause body."""

    reqs: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)
    optional: tuple[str, ...] = ()
    shapes: dict[str, Shape] = field(default_factory=dict)

    @property
    def all_keys(self) -> tuple[str, ...]:
        return self.reqs + self.optional

def _split_top_level(body: str) -> list[str]:
    """Split on commas outside quotes and brackets."""
    parts, buf = [], []
    depth, quote, escaped = 0, "", False
    for ch in body:
        if quote:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf).strip())
    return [p for p in parts if p]

def parse_annotated_args(body: str) -> ArgSpec:
    """Parse `key[?][: shape][=default]` items, including namespaced `step.slot=default`."""
    reqs, optional = [], []
    defaults: dict[str, Any] = {}
    shapes: dict[str, Shape] = {}

    for part in _split_top_level(body):
        head, has_default, raw_default = part.partition("=")
        key_part, _, shape_part = head.partition(":")
        m = _KEY_RE.match(key_part.strip())
        if not m:
            raise ValueError(f"Invalid contract key '{key_part.strip()}' in '{part}'.")
        key = m["key"]
        if shape_part.strip():
            shapes[key] = Shape.parse(shape_part)
        if has_default:
            defaults[key] = ast.literal_eval(raw_default.strip())
        elif m["opt"]:
            optional.append(key)
        else:
            reqs.append(key)

    return ArgSpec(reqs=tuple(reqs), defaults=defaults, optional=tuple(optional), shapes=shapes)

# Python Signature Introspection Helper
def infer_input_keys(fn: Any, schema: type[Any] | None = None) -> tuple[str, ...]:
    """Extract expected input keys from a TypedDict/dataclass schema or Python callable parameters."""
    if schema is not None:
        try:
            if hints := get_type_hints(schema):
                return tuple(hints.keys())
        except TypeError:
            pass

    if not callable(fn):
        return ()

    try:
        sig = signature(fn)
        params = list(sig.parameters.values())
        if len(params) == 1 and params[0].name in ("state", "ctx", "input", "payload"):
            hints = get_type_hints(fn)
            if first_hint := hints.get(params[0].name):
                if isinstance(first_hint, type) and (sub_hints := get_type_hints(first_hint)):
                    return tuple(sub_hints.keys())
            return ()
        return tuple(
            p.name for p in params
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        )
    except (ValueError, TypeError):
        return ()

@dataclass(frozen=True, slots=True)
class UnitContract:
    name: str
    scheme: str
    target: str
    in_reqs: tuple[str, ...] = ()
    in_defaults: dict[str, Any] = field(default_factory=dict)
    out_keys: tuple[str, ...] = ()
    desc: str = ""
    in_optional: tuple[str, ...] = ()
    cfg_reqs: tuple[str, ...] = ()
    cfg_defaults: dict[str, Any] = field(default_factory=dict)
    auth: tuple[AuthReq, ...] = ()
    in_shapes: dict[str, Shape] = field(default_factory=dict)
    cfg_shapes: dict[str, Shape] = field(default_factory=dict)
    out_shapes: dict[str, Shape] = field(default_factory=dict)
    out_optional: tuple[str, ...] = ()

    @property
    def resource_uri(self) -> str:
        return f"{self.scheme}://{self.target}"

    def in_shape(self, key: str) -> Shape:
        return self.in_shapes.get(key, Shape())

    def out_shape(self, key: str) -> Shape:
        return self.out_shapes.get(key, Shape())

    @classmethod
    def with_specs(
        cls,
        name: str,
        scheme: str,
        target: str,
        desc: str = "",
        *,
        ins: ArgSpec | None = None,
        cfgs: ArgSpec | None = None,
        outs: ArgSpec | None = None,
        auth: tuple[AuthReq, ...] = (),
    ) -> UnitContract:
        ins = ins or ArgSpec()
        cfgs = cfgs or ArgSpec()
        outs = outs or ArgSpec()
        return cls(
            name=name,
            scheme=scheme,
            target=target,
            desc=desc,
            in_reqs=ins.reqs,
            in_defaults=ins.defaults,
            in_optional=ins.optional,
            in_shapes=ins.shapes,
            cfg_reqs=cfgs.reqs,
            cfg_defaults=cfgs.defaults,
            cfg_shapes=cfgs.shapes,
            out_keys=outs.all_keys,
            out_optional=outs.optional,
            out_shapes=outs.shapes,
            auth=auth,
        )

    @classmethod
    def from_callable(
        cls,
        name: str,
        fn: Callable[..., dict[str, Any] | None],
        *,
        in_reqs: tuple[str, ...] | list[str] = (),
        in_defaults: dict[str, Any] | None = None,
        out_keys: tuple[str, ...] | list[str] = (),
        schema: type[Any] | None = None,
        desc: str = "",
    ) -> UnitContract:
        resolved_reqs = tuple(in_reqs) if in_reqs else infer_input_keys(fn, schema)
        return cls(
            name=name,
            scheme="py",
            target=f"{fn.__module__}:{fn.__qualname__}",
            in_reqs=resolved_reqs,
            in_defaults=dict(in_defaults or {}),
            out_keys=tuple(out_keys),
            desc=desc or (fn.__doc__ or "").strip(),
        )

@dataclass(frozen=True, slots=True)
class StepSpec:
    """Single step token inside a FlowContract chain."""
    name: str
    heal_target: str | None = None
    heal_mode: str = "retry"  # 'retry' (!unit) | 'fallback' (!>unit)
    gate_expr: str | None = None
    bindings: dict[str, str] = field(default_factory=dict)  # dst -> src
    each_item: str | None = None
    each_over: str | None = None

    @property
    def is_each(self) -> bool:
        return self.each_item is not None and self.each_over is not None

    def source_of(self, dst: str) -> str:
        return self.bindings.get(dst, dst)

@dataclass(frozen=True, slots=True)
class FlowContract:
    """Composable workflow block exposing the same I/O and shape boundary as UnitContract."""

    name: str
    steps: tuple[StepSpec, ...]
    in_reqs: tuple[str, ...] = ()
    in_defaults: dict[str, Any] = field(default_factory=dict)
    out_keys: tuple[str, ...] = ()
    desc: str = ""
    in_optional: tuple[str, ...] = ()
    in_shapes: dict[str, Shape] = field(default_factory=dict)
    out_shapes: dict[str, Shape] = field(default_factory=dict)
    out_optional: tuple[str, ...] = ()
    cfg_values: dict[str, Any] = field(default_factory=dict)

    @property
    def resource_uri(self) -> str:
        return f"flow://{self.name}"

    def cfg_for(self, step_name: str) -> dict[str, Any]:
        prefix = f"{step_name}."
        return {k[len(prefix):]: v for k, v in self.cfg_values.items() if k.startswith(prefix)}

    @classmethod
    def with_specs(
        cls,
        name: str,
        steps: tuple[StepSpec, ...],
        desc: str = "",
        *,
        ins: ArgSpec | None = None,
        outs: ArgSpec | None = None,
        cfg_values: dict[str, Any] | None = None,
    ) -> FlowContract:
        ins = ins or ArgSpec()
        outs = outs or ArgSpec()
        return cls(
            name=name,
            steps=steps,
            desc=desc,
            in_reqs=ins.reqs,
            in_defaults=ins.defaults,
            in_optional=ins.optional,
            in_shapes=ins.shapes,
            out_keys=outs.all_keys,
            out_optional=outs.optional,
            out_shapes=outs.shapes,
            cfg_values=cfg_values or {},
        )

# Compiled Execution Nodes (Stage 2 Primitives)
@dataclass(frozen=True, slots=True)
class CompiledTask:
    name: str
    scheme: str
    target: str
    params: dict[str, Any]
    extract_keys: tuple[str, ...]
    next_state: str
    catch_state: str | None

@dataclass(frozen=True, slots=True)
class CompiledChoice:
    name: str
    rules: tuple[tuple[dict[str, Any], str], ...]
    default_state: str

@dataclass(frozen=True, slots=True)
class CompiledTerminal:
    name: str
    succeeded: bool

CompiledNode: TypeAlias = CompiledTask | CompiledChoice | CompiledTerminal