# xphi.arch.contract.registry.flow
"""
In-memory `UnitContract` / `FlowContract` registry and quote-safe chain DSL parser.
ASL IR lowering lives separately in `xphi.arch.contract.reflex.compiler`.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from xphi.arch.reflex.flow import (
    SCHEMES,
    ArgSpec,
    AuthReq,
    FlowContract,
    Shape,
    StepSpec,
    UnitContract,
    expand_entries,
    parse_annotated_args,
    split_remap,
    split_unit_ref,
)
from xphi.arch.reflex.grammar.clause import split_clause_items

_CLOSERS: dict[str, str] = {"(": ")", "[": "]", "{": "}"}

# Quote & Bracket-Aware Chain Scanners
def _is_ident(text: str) -> bool:
    return (
        bool(text)
        and (text[0] == "_" or ("a" <= text[0] <= "z") or ("A" <= text[0] <= "Z"))
        and all(c == "_" or ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") for c in text)
    )

def split_chain(spec_str: str) -> list[str]:
    """Split a DSL chain on top-level `->` outside quotes and balanced brackets."""
    if not spec_str.strip():
        return []

    segments: list[str] = []
    buf: list[str] = []
    stack: list[str] = []
    quote = ""
    escaped = False
    n = len(spec_str)
    i = 0

    while i < n:
        ch = spec_str[i]
        if quote:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            i += 1
            continue

        if ch in "'\"":
            quote = ch
        elif ch in _CLOSERS:
            stack.append(_CLOSERS[ch])
        elif ch in ")]}":
            if not stack or stack[-1] != ch:
                raise ValueError(f"Unbalanced bracket '{ch}' in chain '{spec_str}'.")
            stack.pop()
        elif not stack and ch == "-" and i + 1 < n and spec_str[i + 1] == ">":
            seg = "".join(buf).strip()
            if not seg:
                raise ValueError(f"Empty chain segment around '->' in '{spec_str}'.")
            segments.append(seg)
            buf = []
            i += 2
            continue

        buf.append(ch)
        i += 1

    if quote:
        raise ValueError(f"Unterminated string literal ({quote}) in chain '{spec_str}'.")
    if stack:
        raise ValueError(f"Unclosed bracket (expected '{stack[-1]}') in chain '{spec_str}'.")

    tail = "".join(buf).strip()
    if not tail:
        raise ValueError(f"Trailing '->' in chain '{spec_str}'.")
    segments.append(tail)
    return segments


def _split_clause(token: str) -> tuple[str, str]:
    cleaned = token.strip()
    op, paren, rest = cleaned.partition("(")
    op_s = op.strip()
    if not paren or not rest.endswith(")") or not _is_ident(op_s):
        raise ValueError(f"Malformed clause '{cleaned}'.")
    return op_s, rest[:-1].strip()


def _extract_trailing_recovery(expr: str) -> tuple[str, str, str | None]:
    """Extract top-level trailing `(!unit)` or `(~>unit)` outside quotes and `[...]`."""
    cleaned = expr.strip()
    if not cleaned.endswith(")"):
        return cleaned, "retry", None

    depth = 0
    quote = ""
    escaped = False
    open_idx = -1
    for i, ch in enumerate(cleaned):
        if quote:
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
            # if depth == 0 and ch == "(" and i + 1 < len(cleaned) and cleaned[i + 1] == "!":
            if depth == 0 and ch == "(" and cleaned.startswith(("(!", "(~>"), i):
                open_idx = i
            depth += 1
        elif ch in ")]}":
            depth -= 1

    if open_idx == -1:
        return cleaned, "retry", None

    head = cleaned[:open_idx].strip()

    # inner = cleaned[open_idx + 2 : -1].strip()
    # heal_mode = "fallback" if inner.startswith(">") else "retry"
    # target = inner[1:].strip() if heal_mode == "fallback" else inner
    is_tilde = cleaned.startswith("(~>", open_idx)
    inner = cleaned[open_idx + (3 if is_tilde else 2) : -1].strip()
    heal_mode = "alt" if (is_tilde or inner.startswith(">")) else "retry"
    target = inner[1:].strip() if (not is_tilde and heal_mode == "alt") else inner

    if not head or not target:
        raise ValueError(f"Malformed recovery clause in '{cleaned}'.")
    split_unit_ref(target, strict=True)
    return head, heal_mode, target


def _parse_input_bindings(base: str, full_token: str) -> tuple[str, dict[str, str]]:
    if "[" not in base:
        if "]" in base:
            raise ValueError(f"Unbalanced ']' in step '{full_token}'.")
        split_unit_ref(base, strict=True)
        return base.strip(), {}

    name_part, _, rest = base.partition("[")
    rest_s = rest.strip()
    if not rest_s.endswith("]"):
        raise ValueError(f"Malformed input binding in step '{full_token}'.")

    unit_name = name_part.strip()
    split_unit_ref(unit_name, strict=True)
    inner = rest_s[:-1]
    if not inner.strip():
        return unit_name, {}

    bindings: dict[str, str] = {}
    for pair in split_clause_items(inner):
        dst, eq, src = pair.partition("=")
        dst_s, src_s = dst.strip(), src.strip()
        if not eq or not (_is_ident(dst_s) and _is_ident(src_s)):
            raise ValueError(f"Invalid input binding '{pair}' in step '{full_token}'.")
        if dst_s in bindings:
            raise ValueError(f"Duplicate input binding '{dst_s}' in step '{full_token}'.")
        bindings[dst_s] = src_s
    return unit_name, bindings


# =====================================================================
# 2. Unit & Flow Chain DSL Parsers
# =====================================================================

def parse_unit_dsl(name: str, spec_str: str, desc: str = "") -> UnitContract:
    ins, cfgs, outs = ArgSpec(), ArgSpec(), ArgSpec()
    auth: tuple[AuthReq, ...] = ()
    scheme, target = "", ""

    for token in split_chain(spec_str):
        op, body = _split_clause(token)
        if op == "in":
            ins = parse_annotated_args(body)
        elif op == "cfg":
            cfgs = parse_annotated_args(body)
        elif op == "auth":
            auth = AuthReq.parse_list(body)
        elif op == "out":
            outs = parse_annotated_args(body)
        elif op in SCHEMES:
            if scheme:
                raise ValueError(f"Unit '{name}' declares multiple execution targets.")
            scheme, target = op, body
        else:
            raise ValueError(f"Unknown unit clause '{op}(...)' in unit '{name}'.")

    if not scheme or not target:
        raise ValueError(f"Unit '{name}' is missing an execution target ({'/'.join(SCHEMES)}).")

    return UnitContract.with_specs(
        name.strip(), scheme, target, desc, ins=ins, cfgs=cfgs, outs=outs, auth=auth
    )


def parse_step_dsl(token: str) -> StepSpec:
    t = token.strip()
    if t.startswith("gate(") and t.endswith(")"):
        inner = t[5:-1].strip()
        if not inner:
            raise ValueError(f"Empty gate expression in '{t}'.")
        # Split on first colon only if LHS is a bare identifier (`gate(name: expr)`)
        lhs, sep, rhs = inner.partition(":")
        if sep and _is_ident(lhs.strip()):
            if not rhs.strip():
                raise ValueError(f"Empty gate condition after '{lhs.strip()}:' in '{t}'.")
            return StepSpec(name=lhs.strip(), gate_expr=rhs.strip())
        return StepSpec(name="evaluate_gate", gate_expr=inner)

    if t.startswith("each(") and t.endswith(")"):
        head, sep, inner = t[5:-1].partition(":")
        item, in_kw, over = head.partition(" in ")
        item_s, over_s, inner_s = item.strip(), over.strip(), inner.strip()
        if not (sep and in_kw and _is_ident(item_s) and _is_ident(over_s) and inner_s):
            raise ValueError(f"Malformed each() step '{t}', expected 'each(item in list: step)'.")
        sub_step = parse_step_dsl(inner_s)
        if sub_step.gate_expr is not None or sub_step.is_each:
            raise ValueError(f"Invalid nested construct inside '{t}'.")
        return replace(sub_step, each_item=item_s, each_over=over_s)

    after_remap, remap = split_remap(t)
    after_heal, heal_mode, heal_target = _extract_trailing_recovery(after_remap)
    unit_name, bindings = _parse_input_bindings(after_heal, t)

    return StepSpec(
        name=unit_name,
        heal_target=heal_target,
        heal_mode=heal_mode,
        bindings=bindings,
        remap=remap,
    )


def parse_flow_dsl(name: str, flow_str: str, desc: str = "") -> FlowContract:
    tokens = split_chain(flow_str)
    ins, outs = ArgSpec(), ArgSpec()
    cfg_values: dict[str, Any] = {}
    steps: list[StepSpec] = []

    for idx, tok in enumerate(tokens):
        if tok.startswith("in(") and tok.endswith(")"):
            if steps:
                raise ValueError(f"Flow '{name}': 'in(...)' must appear before execution steps.")
            ins = parse_annotated_args(_split_clause(tok)[1])
        elif tok.startswith("cfg(") and tok.endswith(")"):
            if steps:
                raise ValueError(f"Flow '{name}': 'cfg(...)' must appear before execution steps.")
            cfg_spec = parse_annotated_args(_split_clause(tok)[1])
            if cfg_spec.reqs or cfg_spec.optional:
                raise ValueError(
                    f"Flow '{name}': flow-level 'cfg(...)' must assign concrete literal values ('unit.slot=val')."
                )
            cfg_values.update(cfg_spec.defaults)
        elif tok.startswith("out(") and tok.endswith(")"):
            if idx != len(tokens) - 1:
                raise ValueError(f"Flow '{name}': 'out(...)' must be the final clause.")
            outs = parse_annotated_args(_split_clause(tok)[1])
        else:
            steps.append(parse_step_dsl(tok))

    if not steps:
        raise ValueError(f"Flow '{name}' must contain at least one execution step.")

    return FlowContract.with_specs(
        name.strip(), tuple(steps), desc, ins=ins, outs=outs, cfg_values=cfg_values
    )


# =====================================================================
# 3. Contract Registry & Lazy Legacy Sample Loader
# =====================================================================

def sample_flow_path() -> Path:
    """Resolve legacy `compats/flow/sample.json` lazily without import-time side effects."""
    from xphi.node.space.bind.resolver import resolve_path

    return resolve_path("compats") / "flow" / "sample.json"


class FlowRegistry:
    """In-memory catalog of atomic `UnitContract` and composite `FlowContract` specs."""

    def __init__(self) -> None:
        self.units: dict[str, UnitContract] = {}
        self.flows: dict[str, FlowContract] = {}

    def get(self, name: str) -> UnitContract | FlowContract:
        if name in self.units:
            return self.units[name]
        if name in self.flows:
            return self.flows[name]
        raise KeyError(f"Unregistered unit or flow contract: '{name}'")

    def register_unit(self, name: str, spec: str, desc: str = "") -> UnitContract:
        unit = parse_unit_dsl(name, spec, desc)
        self.units[unit.name] = unit
        return unit

    def register_flow(self, name: str, spec: str, desc: str = "") -> FlowContract:
        flow = parse_flow_dsl(name, spec, desc)
        self.flows[flow.name] = flow
        return flow

    def load_dict(self, data: dict[str, Any]) -> FlowRegistry:
        for kind, register_fn in (("units", self.register_unit), ("flows", self.register_flow)):
            for name, meta in data.get(kind, {}).items():
                for key, spec, desc in expand_entries(name, meta):
                    register_fn(key, spec, desc)
        return self

    def load_json(self, path: str | Path | None = None) -> FlowRegistry:
        target = Path(path) if path is not None else sample_flow_path()
        raw = json.loads(target.read_text(encoding="utf-8"))
        return self.load_dict(raw)

    def resolve_io(self, name: str) -> tuple[str, tuple[str, ...], dict[str, Any], tuple[str, ...]]:
        """Return `(resource_uri, in_reqs, in_defaults, out_keys)` for a Unit or Sub-Flow."""
        c = self.get(name)
        return c.resource_uri, c.in_reqs, c.in_defaults, c.out_keys

    def resolve_full_io(
        self, name: str
    ) -> tuple[
        tuple[str, ...],
        dict[str, Any],
        tuple[str, ...],
        dict[str, Shape],
        tuple[str, ...],
        dict[str, Shape],
        tuple[str, ...],
    ]:
        """Return `(in_reqs, in_defaults, in_optional, in_shapes, out_keys, out_shapes, out_optional)`."""
        c = self.get(name)
        return (
            c.in_reqs,
            c.in_defaults,
            c.in_optional,
            c.in_shapes,
            c.out_keys,
            c.out_shapes,
            c.out_optional,
        )


def load_sample_registry(path: str | Path | None = None) -> FlowRegistry:
    return FlowRegistry().load_json(path)


def __getattr__(attr: str) -> Any:
    if attr == "SAMPLE_PATH":
        return sample_flow_path()
    if attr == "FLOW_DSL_ROOT":
        return sample_flow_path().parent
    raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")