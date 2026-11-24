# xphi.arch.reflex.flow
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from inspect import signature
from typing import Any, TypeAlias, get_type_hints

from xphi.arch.reflex.grammar.lexer import ParsedShape, split_clause_items
from xphi.arch.reflex.grammar.clause import parse_arg_clause, parse_auth_token, parse_mcp_target_expr, parse_shape_expr
from xphi.arch.reflex.grammar.chain import split_remap, split_unit_ref
from xphi.arch.reflex.grammar.header import parse_header_flow_tags

NodeAction: TypeAlias = Callable[..., dict[str, Any] | None] | str

ACCOUNT_SLOT = "account"
REMAP_KEY = "remap"
SCHEMES: tuple[str, ...] = ("py", "exec", "mcp", "llm", "flow")
BASE_SHAPES: frozenset[str] = frozenset({"val", "text", "num", "bool", "obj", "file"})


# =====================================================================
# 1. Auth, Shape & Clause Argument Specifications
# =====================================================================

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
        node = parse_auth_token(token)
        return cls(provider=node.provider, scope=node.scope)

    @classmethod
    def parse_list(cls, body: str) -> tuple[AuthReq, ...]:
        if not body.strip():
            return ()
        try:
            tokens = split_clause_items(body)
        except ValueError as exc:
            raise ValueError(f"Empty auth token in '{body}'.") from exc
        return tuple(cls.parse(t) for t in tokens)

    @classmethod
    def for_mcp_target(cls, target: str) -> AuthReq:
        """Implicit requirement of `mcp(server/tool)`: the `server` connection."""
        server, _ = parse_mcp_target_expr(target)
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
    def from_parsed(cls, node: ParsedShape, raw_token: str = "") -> Shape:
        if node.base not in BASE_SHAPES:
            label = raw_token.strip() or node.base
            raise ValueError(f"Unknown shape '{label}'.")
        if node.ext and node.base != "file":
            raise ValueError(f"Shape qualifier '<{node.ext}>' is only allowed on 'file'.")
        return cls(base=node.base, ext=node.ext, is_list=node.is_list)

    @classmethod
    def parse(cls, token: str) -> Shape:
        cleaned = token.strip()
        node = parse_shape_expr(cleaned)
        return cls.from_parsed(node, raw_token=cleaned)

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


def parse_annotated_args(body: str) -> ArgSpec:
    """Convert LL-parsed `ParsedArgItem` CST nodes into a validated `ArgSpec`."""
    reqs: list[str] = []
    optional: list[str] = []
    defaults: dict[str, Any] = {}
    shapes: dict[str, Shape] = {}

    for item in parse_arg_clause(body):
        if item.shape is not None:
            ext = f"<{item.shape.ext}>" if item.shape.ext else ""
            raw_label = f"{item.shape.base}{ext}{'[]' if item.shape.is_list else ''}"
            shapes[item.key] = Shape.from_parsed(item.shape, raw_token=raw_label)

        if item.has_default:
            defaults[item.key] = item.default_value
            if item.optional:
                optional.append(item.key)
        elif item.optional:
            optional.append(item.key)
        else:
            reqs.append(item.key)

    return ArgSpec(reqs=tuple(reqs), defaults=defaults, optional=tuple(optional), shapes=shapes)


# =====================================================================
# 2. Multi-Entry Header Expansion & Output Remap Semantics
# =====================================================================

def collect_header_flows(
    head: str,
    rewrite: Callable[[str], str] = lambda s: s,
) -> dict[str, Any] | None:
    """Collect `@flow:` or `@flow.<name>:` tags from a unit script header with strict exclusivity."""
    tags = parse_header_flow_tags(head)
    if not tags:
        return None

    has_single = any(t.entry is None for t in tags)
    has_named = any(t.entry is not None for t in tags)
    if has_single and has_named:
        raise ValueError("'@flow:' and '@flow.<name>:' cannot be mixed in one header.")

    if has_single:
        if len(tags) > 1:
            raise ValueError("Multiple unnamed '@flow:' tags are not allowed; use '@flow.<name>:'.")
        return {"flow": rewrite(tags[0].dsl)}

    named: dict[str, str] = {}
    for t in tags:
        assert t.entry is not None
        if t.entry in named:
            raise ValueError(f"Duplicate '@flow.{t.entry}:' tag at line {t.lineno}.")
        named[t.entry] = rewrite(t.dsl)
    return {"flows": named}


def is_flow_meta(meta: Any) -> bool:
    """Return True if `meta` represents a valid single-entry or multi-entry flow/unit definition."""
    if isinstance(meta, str):
        return bool(meta.strip())
    if not isinstance(meta, dict):
        return False
    has_single = isinstance(meta.get("flow"), str) and bool(meta["flow"].strip())
    has_multi = isinstance(meta.get("flows"), dict) and bool(meta["flows"])
    return has_single or has_multi


def expand_entries(name: str, meta: str | dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Yield `(registry_key, flow_dsl, desc)` tuples (`name` for single, `name:entry` for multi-flow)."""
    base_name, existing_entry = split_unit_ref(name, strict=True)
    if isinstance(meta, str):
        full_key = f"{base_name}:{existing_entry}" if existing_entry else base_name
        yield full_key, meta.strip(), ""
        return

    if "flow" in meta and "flows" in meta:
        raise ValueError(f"Unit/flow '{name.strip()}' cannot declare both 'flow' and 'flows'.")

    desc = str(meta.get("desc", ""))
    if isinstance(meta.get("flows"), dict):
        if existing_entry:
            raise ValueError(f"Multi-entry dict cannot be nested under already-qualified name '{name}'.")
        if not meta["flows"]:
            raise ValueError(f"Unit '{base_name}' declares an empty 'flows' mapping.")
        for entry, spec in meta["flows"].items():
            ref_base, ref_entry = split_unit_ref(f"{base_name}:{entry}", strict=True)
            yield f"{ref_base}:{ref_entry}", str(spec).strip(), desc
    elif "flow" in meta:
        full_key = f"{base_name}:{existing_entry}" if existing_entry else base_name
        yield full_key, str(meta["flow"]).strip(), desc
    else:
        raise ValueError(f"Invalid flow metadata for '{name}': expected 'flow' or 'flows'.")


def remap_out_keys(out_keys: tuple[str, ...], remap: dict[str, str]) -> tuple[str, ...]:
    """Replace remapped `out_keys` with their new context variable names while preserving order."""
    if not remap:
        return out_keys
    if missing := set(remap.values()) - set(out_keys):
        raise KeyError(f"Output remap refers to undeclared out keys: {sorted(missing)}")
    replaced_olds = set(remap.values())
    kept = [k for k in out_keys if k not in replaced_olds]
    added = [new_k for new_k in remap if new_k not in kept]
    return tuple([*kept, *added])


def remap_of(params: dict[str, Any]) -> dict[str, str]:
    """Extract output `remap` mapping from ASL `__fiber_extensions.Projection`."""
    ext = params.get("__fiber_extensions")
    proj = ext.get("Projection") if isinstance(ext, dict) else None
    return dict(proj.get(REMAP_KEY) or {}) if isinstance(proj, dict) else {}


def rename_result(out: Any, remap: dict[str, str]) -> Any:
    """Rename raw unit output keys per `remap` (`new_var -> out_key`) while preserving untouched keys."""
    if not remap or not isinstance(out, dict):
        return out
    replaced_olds = set(remap.values())
    res = {k: v for k, v in out.items() if k not in replaced_olds}
    for new_k, old_k in remap.items():
        if old_k in out:
            res[new_k] = out[old_k]
    return res


# =====================================================================
# 3. Contract Models & Signature Introspection
# =====================================================================

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
            p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
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
    heal_mode: str = "retry"  # 'retry' (!unit) | 'alt' (~>unit)
    gate_expr: str | None = None
    bindings: dict[str, str] = field(default_factory=dict)  # dst -> src
    each_item: str | None = None
    each_over: str | None = None
    remap: dict[str, str] = field(default_factory=dict)  # new_var -> out_key

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
        resolved: dict[str, Any] = {}
        if ":" in step_name:
            base_prefix = f"{step_name.split(':', 1)[0]}."
            resolved.update(
                {k[len(base_prefix) :]: v for k, v in self.cfg_values.items() if k.startswith(base_prefix)}
            )
        exact_prefix = f"{step_name}."
        resolved.update(
            {k[len(exact_prefix) :]: v for k, v in self.cfg_values.items() if k.startswith(exact_prefix)}
        )
        return resolved

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