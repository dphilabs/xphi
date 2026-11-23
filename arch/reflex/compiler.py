# xphi.arch.reflex.compiler
"""
ASL Intermediate Representation (IR) compiler for Reflexor `FlowContract` chains.
Lowers high-level `FlowContract` / `StepSpec` graphs into executable ASL state machines
with collision-free state IDs and quote-safe gate expression path injection.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xphi.arch.reflex.flow import (
    REMAP_KEY,
    FlowContract,
    StepSpec,
    UnitContract,
    remap_out_keys,
)

if TYPE_CHECKING:
    from xphi.arch.contract.registry.flow import FlowRegistry


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _is_ident_part(ch: str) -> bool:
    return _is_ident_start(ch) or ("0" <= ch <= "9")


def inject_gate_paths(expr: str, ctx_pool: set[str]) -> str:
    """
    Prefix bare context identifiers in `expr` with `$.` using lexical scanning.
    Leaves string literals (`'...'`, `"..."`), pre-prefixed `$.var`, and `.attr` untouched.
    """
    if not expr or not ctx_pool:
        return expr

    out: list[str] = []
    i = 0
    n = len(expr)
    quote = ""
    escaped = False

    while i < n:
        ch = expr[i]
        if quote:
            out.append(ch)
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
            out.append(ch)
            i += 1
            continue

        # Keep existing JSONPath references (`$.foo`) intact
        if ch == "$" and i + 1 < n and expr[i + 1] == ".":
            out.append("$.")
            i += 2
            while i < n and (_is_ident_part(expr[i]) or expr[i] == "."):
                out.append(expr[i])
                i += 1
            continue

        if _is_ident_start(ch):
            start = i
            while i < n and _is_ident_part(expr[i]):
                i += 1
            ident = expr[start:i]
            prev_ch = expr[start - 1] if start > 0 else ""
            if prev_ch != "." and ident in ctx_pool:
                out.append(f"$.{ident}")
            else:
                out.append(ident)
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _unique_state_id(base: str, used: set[str]) -> str:
    """Preserve `base` on first occurrence; suffix `__2`, `__3`, ... on subsequent collisions."""
    if base not in used:
        used.add(base)
        return base
    seq = 2
    while f"{base}__{seq}" in used:
        seq += 1
    uid = f"{base}__{seq}"
    used.add(uid)
    return uid


def _allocate_state_ids(steps: tuple[StepSpec, ...]) -> list[str]:
    used: set[str] = {"success_end", "fail_end"}
    return [_unique_state_id(step.name, used) for step in steps]


def build_asl_task(
    step: StepSpec,
    owner: FlowContract,
    registry: FlowRegistry,
    ctx_pool: set[str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Build a single ASL `Task` state dictionary and its effective emitted context keys."""
    contract: UnitContract | FlowContract = registry.get(step.name)
    params: dict[str, Any] = {}

    local_pool = set(ctx_pool)
    if step.is_each and step.each_item:
        if step.each_over not in ctx_pool:
            raise KeyError(
                f"Contract violation at '{step.name}': each_over '{step.each_over}' "
                f"not in context {sorted(ctx_pool)}."
            )
        local_pool.add(step.each_item)

    # Config defaults & flow-level cfg overrides (atomic units)
    if isinstance(contract, UnitContract):
        params.update(contract.cfg_defaults)
        params.update(owner.cfg_for(step.name))

    # Data defaults (overridden dynamically when source key exists in upstream context)
    for k, def_val in contract.in_defaults.items():
        src = step.source_of(k)
        if src in local_pool:
            params[f"{k}.$"] = f"$.{src}"
        else:
            params[k] = def_val

    # Optional & required data inputs
    for k in contract.in_optional:
        src = step.source_of(k)
        if src in local_pool:
            params[f"{k}.$"] = f"$.{src}"

    for k in contract.in_reqs:
        src = step.source_of(k)
        if src not in local_pool and k not in contract.in_defaults:
            raise KeyError(
                f"Contract violation at '{step.name}': required input '{k}' (<- '{src}') "
                f"not provided by upstream context {sorted(local_pool)}."
            )
        if src in local_pool:
            params[f"{k}.$"] = f"$.{src}"

    out_keys = remap_out_keys(contract.out_keys, step.remap)

    # Fiber runtime extensions (Projection, Optional inputs, Output remap, Each fan-out)
    fiber_ext: dict[str, Any] = {}
    if out_keys or contract.in_optional or step.remap:
        proj: dict[str, Any] = {}
        if out_keys:
            proj["extract_keys"] = [f"$.{k}" for k in out_keys]
        if contract.in_optional:
            proj["optional_in"] = list(contract.in_optional)
        if step.remap:
            proj[REMAP_KEY] = dict(step.remap)
        fiber_ext["Projection"] = proj

    if step.is_each and step.each_item and step.each_over:
        fiber_ext["Each"] = {"item": step.each_item, "over": f"$.{step.each_over}"}

    if fiber_ext:
        params["__fiber_extensions"] = fiber_ext

    node: dict[str, Any] = {"Type": "Task", "Resource": contract.resource_uri}
    if params:
        node["Parameters"] = params
    return node, out_keys


def compile_to_asl(
    target: str | FlowContract | dict[str, Any],
    registry: FlowRegistry,
) -> dict[str, Any]:
    """Compile a registered flow name or `FlowContract` (including Sub-Flows) into ASL IR."""
    if isinstance(target, dict) and "States" in target:
        return target

    if isinstance(target, FlowContract):
        flow = target
    else:
        flow_name = target if isinstance(target, str) else str(target["name"])
        if flow_name not in registry.flows:
            raise KeyError(f"Unregistered workflow contract: '{flow_name}'")
        flow = registry.flows[flow_name]

    if not flow.steps:
        raise ValueError(f"Workflow '{flow.name}' has no steps to compile.")

    state_ids = _allocate_state_ids(flow.steps)
    used_ids: set[str] = set(state_ids) | {"success_end", "fail_end"}

    states: dict[str, Any] = {}
    ctx_pool: set[str] = set(flow.in_reqs) | set(flow.in_defaults.keys()) | set(flow.in_optional)

    for i, step in enumerate(flow.steps):
        state_id = state_ids[i]
        next_id = state_ids[i + 1] if i + 1 < len(flow.steps) else "success_end"

        if step.gate_expr is not None:
            expr = inject_gate_paths(step.gate_expr, ctx_pool)
            states[state_id] = {
                "Type": "Choice",
                "Choices": [{"if": expr, "Next": next_id}],
                "Default": "fail_end",
            }
            continue

        node, outs = build_asl_task(step, flow, registry, ctx_pool)
        node["Next"] = next_id

        if step.heal_target:
            heal_id = _unique_state_id(step.heal_target, used_ids)
            node["Catch"] = heal_id
            states[state_id] = node

            heal_step = StepSpec(name=step.heal_target)
            heal_node, heal_outs = build_asl_task(heal_step, flow, registry, ctx_pool)
            if step.heal_mode == "retry":
                heal_node["Next"] = state_id
            else:
                heal_node["Next"] = next_id
                ctx_pool.update(heal_outs)
            heal_node["Catch"] = "fail_end"
            states[heal_id] = heal_node
        else:
            node["Catch"] = "fail_end"
            states[state_id] = node

        ctx_pool.update(outs)

    missing_outs = set(flow.out_keys) - ctx_pool
    if missing_outs:
        raise KeyError(
            f"Workflow '{flow.name}' exit contract violation: "
            f"declared out({', '.join(sorted(missing_outs))}) never produced."
        )

    states["success_end"] = {"Type": "Succeed"}
    states["fail_end"] = {"Type": "Fail"}
    return {"Version": "1.0", "StartAt": state_ids[0], "States": states}