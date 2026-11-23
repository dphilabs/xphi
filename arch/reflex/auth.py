# xphi.arch.reflex.auth
from __future__ import annotations
from collections.abc import Iterator

from xphi.arch.contract.registry.flow import FlowRegistry
from xphi.arch.reflex.flow import ACCOUNT_SLOT, AuthReq, FlowContract, UnitContract

def unit_auth(unit: UnitContract) -> tuple[AuthReq, ...]:
    """Return declared requirements + derived `mcp:<server>` (ignoring bare `auth(mcp)`)."""
    reqs = [a for a in unit.auth if not (a.delegated and a.scope is None)]
    if unit.scheme == "mcp":
        reqs.append(AuthReq.for_mcp_target(unit.target))
    return tuple(dict.fromkeys(reqs))

def unit_providers(unit: UnitContract) -> tuple[str, ...]:
    """Return unique non-delegated (user-app) provider names required by `unit`."""
    return tuple(dict.fromkeys(a.provider for a in unit_auth(unit) if not a.delegated))

def allowed_account_slots(unit: UnitContract) -> set[str]:
    """Return valid account cfg slot names (`account` and `account_<provider>`) for a unit."""
    return {ACCOUNT_SLOT} | {f"{ACCOUNT_SLOT}_{p}" for p in unit_providers(unit)}

def iter_flow_units(
    reg: FlowRegistry,
    flow_name: str,
) -> Iterator[tuple[FlowContract, str, UnitContract]]:
    """Yield `(owner_flow, step_name, unit)` across steps, `!heal` targets, `each` bodies, and sub-flows."""
    flow = reg.flows[flow_name]
    for step in flow.steps:
        if step.gate_expr is not None:
            continue
        for name in filter(None, (step.name, step.heal_target)):
            if name in reg.units:
                yield flow, name, reg.units[name]
            elif name in reg.flows:
                yield from iter_flow_units(reg, name)

def flow_auth(reg: FlowRegistry, flow_name: str) -> set[str]:
    """Compute union of all credential requirement strings needed to run `flow_name`."""
    return {str(a) for _, _, u in iter_flow_units(reg, flow_name) for a in unit_auth(u)}

def missing_credentials(requirements: set[str], available: set[str]) -> set[str]:
    """Check which required credentials are not satisfied by `available`."""
    return {req for req in requirements if not AuthReq.parse(req).is_satisfied_by(available)}

def resolve_accounts(reg: FlowRegistry, flow_name: str, connected: dict[str, list[str]],) -> tuple[dict[str, str], list[str]]:
    selected: dict[str, str] = {}
    open_slots: list[str] = []

    for owner, step_name, unit in iter_flow_units(reg, flow_name):
        providers = unit_providers(unit)
        step_cfg = owner.cfg_for(step_name)

        for provider in providers:
            slot = ACCOUNT_SLOT if len(providers) == 1 else f"{ACCOUNT_SLOT}_{provider}"
            key = f"{owner.name}:{step_name}.{slot}"
            accounts = connected.get(provider, [])
            chosen = step_cfg.get(slot)

            if chosen is not None:
                if chosen not in accounts:
                    raise ValueError(f"{key}: account '{chosen}' is not connected for '{provider}'.")
                selected[key] = chosen
            elif len(accounts) == 1:
                selected[key] = accounts[0]
            elif len(accounts) > 1:
                open_slots.append(key)

    return selected, open_slots