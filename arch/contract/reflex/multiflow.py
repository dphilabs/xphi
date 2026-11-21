# xphi.arch.contract.reflex.multiflow
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Any

FLOW_TAG_RE = re.compile(r"@flow(?:\.([A-Za-z_]\w*))?:\s*(.+)")
REMAP_KEY = "remap"


def split_unit_ref(unit_ref: str) -> tuple[str, str]:
    """Split a registry unit reference `'unit:entry'` into `(base_unit, entry)` (`entry == ''` if single)."""
    base, sep, entry = unit_ref.partition(":")
    return (base.strip(), entry.strip()) if sep else (unit_ref.strip(), "")


def collect_header_flows(
    head: str,
    rewrite: Callable[[str], str] = lambda s: s,
) -> dict[str, Any] | None:
    """Collect `@flow:` or `@flow.<name>:` lines from a script header without line-start anchoring."""
    found = [(m.group(1), rewrite(m.group(2).strip())) for m in FLOW_TAG_RE.finditer(head)]
    if not found:
        return None
    named = {n: s for n, s in found if n}
    if named and len(named) != len(found):
        raise ValueError("'@flow:' and '@flow.<name>:' cannot be mixed in one header.")
    return {"flows": named} if named else {"flow": found[0][1]}


def is_flow_meta(meta: Any) -> bool:
    """Return True if `meta` represents a valid single-entry or multi-entry flow/unit definition."""
    return isinstance(meta, str) or (isinstance(meta, dict) and ("flow" in meta or "flows" in meta))


def expand_entries(name: str, meta: str | dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Yield `(registry_key, flow_dsl, desc)` tuples (`name` for single, `name:entry` for multi-flow)."""
    if isinstance(meta, str):
        yield name, meta, ""
        return
    desc = meta.get("desc", "")
    if isinstance(meta.get("flows"), dict):
        for entry, spec in meta["flows"].items():
            yield f"{name}:{entry}", spec, desc
    else:
        yield name, meta["flow"], desc


def split_remap(base: str) -> tuple[str, dict[str, str]]:
    """Split trailing `=> (new_var=out_key, ...)` from a step expression prior to `[` parsing."""
    head, sep, tail = base.partition("=>")
    if not sep:
        return base, {}
    body = tail.strip()
    if not (body.startswith("(") and body.endswith(")")):
        raise ValueError(f"Malformed output remap in '{base.strip()}'.")
    remap: dict[str, str] = {}
    for item in body[1:-1].split(","):
        if not item.strip():
            continue
        new_k, eq, old_k = item.partition("=")
        if not eq or not new_k.strip() or not old_k.strip():
            raise ValueError(f"Invalid remap pair '{item.strip()}' in '{base.strip()}'.")
        remap[new_k.strip()] = old_k.strip()
    return head.strip(), remap


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