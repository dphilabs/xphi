# xphi.arch.xor.fsm.parser
from __future__ import annotations

import ast
from collections import deque
from collections.abc import Callable
from functools import lru_cache
import fnmatch
import operator
import re
from typing import Any

from xphi.arch.contract.reflex.flow import SCHEMES
from xphi.arch.contract.reflex.fsm import (
    ASL_CHOICE_OP_MAP,
    apply_projection as _contract_projection,
    extract_json_path,
    parse_resource_uri,
    resolve_dynamic_params,
)

_PATH_TOKEN_RE = re.compile(r"\$\.([A-Za-z_][A-Za-z0-9_.]*)")
_SENTINEL = object()
_JSON_LITERALS: dict[str, Any] = {"true": True, "false": False, "null": None}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}

_EXTRA_TESTS: dict[str, Callable[[Any, Any], bool]] = {
    "IsNull": lambda v, exp: (v is None) is bool(exp),
    "IsBoolean": lambda v, exp: isinstance(v, bool) is bool(exp),
    "IsNumeric": lambda v, exp: (isinstance(v, (int, float)) and not isinstance(v, bool)) is bool(exp),
    "IsString": lambda v, exp: isinstance(v, str) is bool(exp),
    "StringMatches": lambda v, pat: isinstance(v, str) and fnmatch.fnmatchcase(v, pat),
}

VALID_STATE_TYPES = frozenset({"Task", "Choice", "Pass", "Wait", "Parallel", "Map", "Succeed", "Fail"})
VALID_URI_SCHEMES = frozenset(SCHEMES) | {"arn", "pass"}
_ASL_TEST_KEYS = (
    frozenset(ASL_CHOICE_OP_MAP)
    | frozenset(_EXTRA_TESTS)
    | {f"{k}Path" for k in ASL_CHOICE_OP_MAP}
    | {"IsPresent"}
)


class ASLValidationError(ValueError):
    pass


def resolve_path(ctx: dict[str, Any], path: str, default: Any = _SENTINEL) -> Any:
    if path == "$":
        return ctx
    if (val := extract_json_path(ctx, path)) is not None:
        return val
    leaf = path.removeprefix("$.").split(".")[-1]
    if isinstance(ctx, dict) and leaf in ctx and ctx[leaf] is None:
        return None
    if default is not _SENTINEL:
        return default
    raise KeyError(f"Context path not found: '{path}'")


def bind_parameters(
    params: dict[str, Any],
    ctx: dict[str, Any],
    *,
    _inherited_opt: set[str] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, str] | None]:
    ext = params.get("__fiber_extensions") if isinstance(params.get("__fiber_extensions"), dict) else {}
    proj = ext.get("Projection") if isinstance(ext.get("Projection"), dict) else {}
    each = ext.get("Each") if isinstance(ext.get("Each"), dict) else {}

    extract_keys = tuple(p.removeprefix("$.") for p in proj.get("extract_keys", ()))
    opt_keys = {p.removeprefix("$.") for p in proj.get("optional_in", ())} | (_inherited_opt or set())
    each_spec = (each["item"], each["over"].removeprefix("$.")) if "item" in each and "over" in each else None

    clean = {k: v for k, v in params.items() if k != "__fiber_extensions"}
    if not opt_keys and not any(isinstance(v, dict) for v in clean.values()):
        for k, v in clean.items():
            if k.endswith(".$") and isinstance(v, str):
                resolve_path(ctx, v)
        return resolve_dynamic_params(clean, ctx), extract_keys, each_spec

    bound: dict[str, Any] = {}
    for k, v in clean.items():
        if k.endswith(".$") and isinstance(v, str):
            dst = k[:-2]
            src_leaf = v.removeprefix("$.").split(".")[-1]
            is_opt = (dst in opt_keys) or (src_leaf in opt_keys)
            val = resolve_path(ctx, v, default=_SENTINEL) if is_opt else resolve_path(ctx, v)
            if val is not _SENTINEL:
                bound[dst] = val
        elif isinstance(v, dict):
            bound[k], _, _ = bind_parameters(v, ctx, _inherited_opt=opt_keys)
        else:
            bound[k] = v
    return bound, extract_keys, each_spec


def apply_projection(
    ctx: dict[str, Any], result: dict[str, Any] | None, extract_keys: tuple[str, ...] | list[str] = ()
) -> dict[str, Any]:
    if not isinstance(result, dict):
        return ctx
    if not extract_keys:
        ctx.update(result)
        return ctx
    updated, _ = _contract_projection(result, {"extract_keys": [k.removeprefix("$.") for k in extract_keys]}, ctx)
    ctx.update(updated)
    return ctx


@lru_cache(maxsize=256)
def parse_if_ast(expr: str) -> tuple[ast.Expression, dict[str, str]]:
    path_map: dict[str, str] = {}
    def _sub(m: re.Match[str]) -> str:
        k = f"__ctx_{len(path_map)}__"
        path_map[k] = f"$.{m.group(1)}"
        return k

    tree = ast.parse(_PATH_TOKEN_RE.sub(_sub, expr), mode="eval")
    for n in ast.walk(tree):
        if isinstance(n, (ast.Call, ast.Attribute, ast.Subscript, ast.Lambda, ast.ListComp, ast.DictComp)):
            raise ValueError(f"Unsafe syntax in choice expression: '{expr}'")
        if isinstance(n, ast.Name) and n.id not in path_map and n.id not in _JSON_LITERALS:
            raise ValueError(f"Unbound identifier '{n.id}' in '{expr}'")
    return tree, path_map


def eval_choice_if(expr: str, ctx: dict[str, Any]) -> bool:
    tree, path_map = parse_if_ast(expr)
    def _ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, (ast.Tuple, ast.List)): return [_ev(e) for e in n.elts]
        if isinstance(n, ast.Name):
            if n.id in _JSON_LITERALS:
                return _JSON_LITERALS[n.id]
            return extract_json_path(ctx, path_map[n.id])
        if isinstance(n, ast.UnaryOp):
            v = _ev(n.operand)
            return (not v) if isinstance(n.op, ast.Not) else (-v if isinstance(n.op, ast.USub) else +v)
        if isinstance(n, ast.BoolOp):
            return (all if isinstance(n.op, ast.And) else any)(_ev(v) for v in n.values)
        if isinstance(n, ast.Compare):
            left = _ev(n.left)
            return all(_CMP_OPS[type(op)](left, left := _ev(c)) for op, c in zip(n.ops, n.comparators))
        raise ValueError(f"Unsupported AST node '{type(n).__name__}'")
    return bool(_ev(tree.body))


def eval_choice_rule(rule: dict[str, Any] | str, ctx: dict[str, Any]) -> bool:
    if isinstance(rule, str): return eval_choice_if(rule, ctx)
    if "if" in rule: return eval_choice_if(rule["if"], ctx)
    if "And" in rule: return all(eval_choice_rule(r, ctx) for r in rule["And"])
    if "Or" in rule: return any(eval_choice_rule(r, ctx) for r in rule["Or"])
    if "Not" in rule: return not eval_choice_rule(rule["Not"], ctx)

    var = rule.get("Variable")
    if not isinstance(var, str): raise ValueError(f"Invalid Choice rule: {rule}")
    if "IsPresent" in rule:
        return (resolve_path(ctx, var, default=_SENTINEL) is not _SENTINEL) is bool(rule["IsPresent"])

    val = extract_json_path(ctx, var)
    for k, exp in rule.items():
        if k in ("Variable", "Next", "Comment", "Assign"): continue
        if k.endswith("Path"):
            exp, k = resolve_path(ctx, exp), k[:-4]
        if sym := ASL_CHOICE_OP_MAP.get(k):
            if k == "BooleanEquals" and not isinstance(val, bool):
                return False
            return eval_choice_if(f"{var} {sym} {exp!r}", {**ctx, var.removeprefix("$."): val})
        if k in _EXTRA_TESTS:
            return bool(_EXTRA_TESTS[k](val, exp))
    raise ValueError(f"Unsupported Choice operator: {rule}")


def _validate_choice_rule(state: str, rule: Any, errors: list[str], *, top: bool = True) -> str | None:
    if not isinstance(rule, dict):
        errors.append(f"[{state}] Choice rule must be a dict.")
        return None
    nxt = rule.get("Next") if top and isinstance(rule.get("Next"), str) and rule["Next"] else None
    if top and not nxt: errors.append(f"[{state}] Choice rule missing 'Next'.")
    elif not top and "Next" in rule: errors.append(f"[{state}] Nested Choice rule cannot contain 'Next'.")

    if "if" in rule:
        try:
            if not isinstance(rule["if"], str): raise ValueError("must be str")
            parse_if_ast(rule["if"])
        except Exception as e: errors.append(f"[{state}] Invalid 'if': {e}")
    elif "And" in rule or "Or" in rule:
        sub = rule.get("And") or rule.get("Or")
        if not isinstance(sub, list) or not sub: errors.append(f"[{state}] Empty And/Or list.")
        else:
            for s in sub: _validate_choice_rule(state, s, errors, top=False)
    elif "Not" in rule:
        _validate_choice_rule(state, rule["Not"], errors, top=False)
    else:
        var, ops = rule.get("Variable"), [k for k in rule if k not in ("Variable", "Next", "Comment", "Assign")]
        if not isinstance(var, str) or not var.startswith("$") or len(ops) != 1 or ops[0] not in _ASL_TEST_KEYS:
            errors.append(f"[{state}] Invalid Choice rule: {rule}")
    return nxt


def verify_asl(asl: dict[str, Any]) -> list[str]:
    if not isinstance(asl, dict) or not isinstance(asl.get("States"), dict) or not asl["States"]:
        return ["ASL must contain a non-empty 'States' dict."]
    states, start = asl["States"], asl.get("StartAt")
    if not isinstance(start, str) or start not in states:
        return [f"Missing or undefined 'StartAt': {start!r}"]

    errors, edges_map = [], {}
    for name, node in states.items():
        if not isinstance(node, dict) or (stype := node.get("Type")) not in VALID_STATE_TYPES:
            errors.append(f"[{name}] Invalid state Type.")
            continue
        edges: list[str] = []
        if stype in ("Succeed", "Fail"):
            if "Next" in node or "End" in node: errors.append(f"[{name}] Terminal cannot have Next/End.")
        elif stype == "Choice":
            if "Next" in node or "End" in node or not isinstance(node.get("Choices"), list) or not node["Choices"]:
                errors.append(f"[{name}] Invalid Choice state.")
            for r in node.get("Choices") or ():
                if nxt := _validate_choice_rule(name, r, errors): edges.append(nxt)
            if "Default" in node:
                if isinstance(node["Default"], str) and node["Default"]: edges.append(node["Default"])
                else: errors.append(f"[{name}] Invalid Default.")
        else:
            nxt_val = node.get("Next")
            is_valid_next = (
                (isinstance(nxt_val, str) and bool(nxt_val))
                or (isinstance(nxt_val, list) and bool(nxt_val) and all(isinstance(x, str) and x for x in nxt_val))
            )
            has_end = node.get("End") is True
            if is_valid_next == has_end: errors.append(f"[{name}] Specify exactly one of 'Next' or 'End: true'.")
            elif isinstance(nxt_val, str): edges.append(nxt_val)
            elif isinstance(nxt_val, list): edges.extend(nxt_val)

            if stype == "Task":
                res = node.get("Resource", "")
                scheme = "arn" if isinstance(res, str) and res.startswith("arn:") else parse_resource_uri(res).scheme
                if not isinstance(res, str) or ("://" not in res and not res.startswith("arn:")) or scheme not in VALID_URI_SCHEMES:
                    errors.append(f"[{name}] Invalid Resource URI: {res!r}")
                if "Catch" in node:
                    c = node["Catch"]
                    if isinstance(c, str) and c: edges.append(c)
                    elif isinstance(c, list) and c and all(isinstance(x, dict) and isinstance(x.get("Next"), str) for x in c):
                        edges.extend(x["Next"] for x in c)
                    else: errors.append(f"[{name}] Invalid Catch.")
        edges_map[name] = edges

    visited, has_term, q = set(), False, deque([start])
    while q:
        if (curr := q.popleft()) in visited: continue
        visited.add(curr)
        if states[curr].get("Type") in ("Succeed", "Fail") or states[curr].get("End") is True: has_term = True
        for nxt in edges_map.get(curr, ()):
            if nxt not in states: errors.append(f"[{curr}] Undefined target '{nxt}'.")
            elif nxt not in visited: q.append(nxt)
    if orphans := (set(states) - visited): errors.append(f"Orphan states: {sorted(orphans)}")
    if not has_term: errors.append("No reachable terminal state.")
    return errors