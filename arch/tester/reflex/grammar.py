# xphi.arch.tester.reflex.grammar
"""Fixture-driven L0 contract & LL-grammar verifier for `xphi.arch.contract.reflex.{grammar.*,flow,compiler}`."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from xphi.arch.reflex import compiler as reflex_compiler_mod
from xphi.arch.reflex import flow as reflex_flow_mod
from xphi.arch.reflex.compiler import inject_gate_paths
from xphi.arch.reflex.flow import (
    AuthReq,
    FlowContract,
    Shape,
    StepSpec,
    collect_header_flows,
    expand_entries,
    parse_annotated_args,
    remap_out_keys,
    rename_result,
)
from xphi.arch.reflex.grammar import (
    chain as grammar_chain_mod,
    clause as grammar_clause_mod,
    header as grammar_header_mod,
    lexer as grammar_lexer_mod,
)
from xphi.arch.reflex.grammar.chain import split_chain, split_remap
from xphi.arch.reflex.grammar.header import (
    parse_unit_header_meta,
    prefix_target_clause,
)

from xphi.arch.reflex.harness.suite import (
    diff_snapshot,
    expect_raises,
    resolve_fixture_dir,
    run_fixture_suite,
    verify_module_contracts,
)
from xphi.arch.reflex.unit.scanner import UnitAstReport, UnitVerificationError

class ShapeContractVerificationError(UnitVerificationError):
    """Raised when L0 grammar, Shape, ArgSpec, AuthReq, or multiflow contract checks fail."""

def _assert_snap(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if errs := diff_snapshot(actual, expected):
        raise ShapeContractVerificationError("; ".join(errs))

# Structural Case Runners (Dispatched by case key shape, no `kind`)
def _run_shape_parse(case: dict[str, Any]) -> str:
    raw = case["shape"]
    if err_name := case.get("expect_error"):
        return expect_raises(
            err_name,
            [raw],
            Shape.parse,
            match=case.get("error_match"),
            error_cls=ShapeContractVerificationError,
        )

    s = Shape.parse(raw)
    if case.get("roundtrip_list") and s.collected().element() != s:
        raise ShapeContractVerificationError(f"element/collected roundtrip failed for {s!r}")

    _assert_snap(
        {"base": s.base, "ext": s.ext, "is_list": s.is_list, "canonical": str(s)},
        case["expect"],
    )
    return f"shape={s}"

def _run_shape_compat(case: dict[str, Any]) -> str:
    verdict = Shape.parse(case["producer"]).compat(Shape.parse(case["consumer"]))
    if verdict != case["expect"]:
        raise ShapeContractVerificationError(
            f"{case['producer']!r} -> {case['consumer']!r}: expected {case['expect']!r}, got {verdict!r}"
        )
    return f"{case['producer']} -> {case['consumer']} == {verdict}"


def _run_arg_clause(case: dict[str, Any]) -> str:
    raw = case["clause"]
    if err_name := case.get("expect_error"):
        return expect_raises(
            err_name,
            [raw],
            parse_annotated_args,
            match=case.get("error_match"),
            error_cls=ShapeContractVerificationError,
        )

    spec = parse_annotated_args(raw)
    _assert_snap(
        {
            "reqs": list(spec.reqs),
            "optional": list(spec.optional),
            "defaults": spec.defaults,
            "shapes": {k: str(v) for k, v in spec.shapes.items()},
        },
        case["expect"],
    )
    return f"keys={list(spec.all_keys) or list(spec.defaults)}"


def _run_cfg_scoping(case: dict[str, Any]) -> str:
    cfg_spec = parse_annotated_args(case["cfg_clause"])
    steps = tuple(StepSpec(name=s) for s in case["steps"])
    flow = FlowContract.with_specs("cfg_scope_test", steps=steps, cfg_values=cfg_spec.defaults)
    actual = {s: flow.cfg_for(s) for s in case["steps"]}
    _assert_snap(actual, case["expect"])
    return f"scoped {len(actual)} steps"


def _run_cred_case(case: dict[str, Any]) -> str:
    err_name = case.get("expect_error")
    match = case.get("error_match")

    if "auth_list" in case:
        if err_name:
            return expect_raises(
                err_name,
                [case["auth_list"]],
                AuthReq.parse_list,
                match=match,
                error_cls=ShapeContractVerificationError,
            )
        parsed = [str(a) for a in AuthReq.parse_list(case["auth_list"])]
        if parsed != case["expect"]:
            raise ShapeContractVerificationError(f"expected {case['expect']!r}, got {parsed!r}")
        return f"list={parsed}"

    fn = AuthReq.for_mcp_target if "mcp_target" in case else AuthReq.parse
    raw = case["mcp_target"] if "mcp_target" in case else case["auth"]
    if err_name:
        return expect_raises(err_name, [raw], fn, match=match, error_cls=ShapeContractVerificationError)

    req = fn(raw)
    avail = set(case.get("available") or ())
    _assert_snap(
        {
            "provider": req.provider,
            "scope": req.scope,
            "delegated": req.delegated,
            "satisfied": req.is_satisfied_by(avail),
        },
        case["expect"],
    )
    return f"{req} (delegated={req.delegated})"


def _run_multiflow_case(case: dict[str, Any]) -> str:
    err_name = case.get("expect_error")
    match = case.get("error_match")

    if "header" in case:
        if err_name:
            return expect_raises(
                err_name,
                [case["header"]],
                collect_header_flows,
                match=match,
                error_cls=ShapeContractVerificationError,
            )
        got = collect_header_flows(case["header"])
        if got != case["expect"]:
            raise ShapeContractVerificationError(f"expected {case['expect']!r}, got {got!r}")
        return "header->None" if got is None else f"header->{sorted(got)}"

    if "step_remap" in case:
        if err_name:
            return expect_raises(
                err_name,
                [case["step_remap"]],
                split_remap,
                match=match,
                error_cls=ShapeContractVerificationError,
            )
        head, remap = split_remap(case["step_remap"])
        _assert_snap({"head": head, "remap": remap}, case["expect"])
        return f"remap={remap}"

    if "out_keys" in case:
        out_keys = tuple(case["out_keys"])
        remap = dict(case["remap"])

        def _apply_remap(r: dict[str, str]) -> tuple[str, ...]:
            return remap_out_keys(out_keys, r)

        if err_name:
            return expect_raises(
                err_name,
                [remap],
                _apply_remap,
                match=match,
                error_cls=ShapeContractVerificationError,
            )
        remapped = list(_apply_remap(remap))
        renamed = rename_result(case.get("raw_out") or {}, remap)
        _assert_snap({"remapped_keys": remapped, "renamed_out": renamed}, case["expect"])
        return f"out_keys->{remapped}"

    exp_spec = case["expand"]

    def _expand(meta: Any) -> list[list[str]]:
        return [list(t) for t in expand_entries(exp_spec["name"], meta)]

    if err_name:
        return expect_raises(
            err_name,
            [exp_spec["meta"]],
            _expand,
            match=match,
            error_cls=ShapeContractVerificationError,
        )
    got_entries = _expand(exp_spec["meta"])
    if got_entries != case["expect"]:
        raise ShapeContractVerificationError(f"expected {case['expect']!r}, got {got_entries!r}")
    return f"expanded {len(got_entries)} entries"


def _run_chain_or_header_ext(case: dict[str, Any]) -> str:
    err_name = case.get("expect_error")
    match = case.get("error_match")

    if "chain" in case:
        if err_name:
            return expect_raises(
                err_name, [case["chain"]], split_chain, match=match, error_cls=ShapeContractVerificationError
            )
        segs = split_chain(case["chain"])
        if segs != case["expect"]:
            raise ShapeContractVerificationError(f"expected {case['expect']!r}, got {segs!r}")
        return f"segments={len(segs)}"

    if "unit_header" in case:
        if err_name:
            return expect_raises(
                err_name,
                [case["unit_header"]],
                parse_unit_header_meta,
                match=match,
                error_cls=ShapeContractVerificationError,
            )
        parsed = parse_unit_header_meta(case["unit_header"])
        actual = (
            None
            if parsed is None
            else {
                "unit": parsed.unit,
                "desc": parsed.desc,
                "side_effect": parsed.side_effect,
                "entries": [t.entry for t in parsed.tags],
            }
        )
        if actual != case["expect"]:
            raise ShapeContractVerificationError(f"expected {case['expect']!r}, got {actual!r}")
        return "unit_header->None" if actual is None else f"unit={actual['unit']}"

    if "prefix_clause" in case:
        rewritten = prefix_target_clause(case["prefix_clause"], case["ref_prefix"])
        if rewritten != case["expect"]:
            raise ShapeContractVerificationError(f"expected {case['expect']!r}, got {rewritten!r}")
        return f"rewritten={rewritten}"

    rewritten_gate = inject_gate_paths(case["gate_inject"], set(case.get("ctx_pool") or ()))
    if rewritten_gate != case["expect"]:
        raise ShapeContractVerificationError(f"expected {case['expect']!r}, got {rewritten_gate!r}")
    return f"gate={rewritten_gate}"


def _dispatch_contract_case(case: dict[str, Any], _ctx: dict[str, Any]) -> str:
    if "producer" in case:
        return _run_shape_compat(case)
    if "shape" in case:
        return _run_shape_parse(case)
    if "clause" in case:
        return _run_arg_clause(case)
    if "cfg_clause" in case:
        return _run_cfg_scoping(case)
    if any(k in case for k in ("auth", "auth_list", "mcp_target")):
        return _run_cred_case(case)
    if any(k in case for k in ("header", "step_remap", "out_keys", "expand")):
        return _run_multiflow_case(case)
    if any(k in case for k in ("chain", "unit_header", "prefix_clause", "gate_inject")):
        return _run_chain_or_header_ext(case)
    raise ShapeContractVerificationError(f"Unrecognized contract fixture keys: {sorted(case)}")

# Suite Orchestrator
class ShapeContractTester:
    """AST boundary + fixture-driven L0 tester for `reflex.grammar.*`, `reflex.flow`, and `reflex.compiler`."""

    @classmethod
    def verify_units(cls) -> dict[str, UnitAstReport]:
        return verify_module_contracts(
            (
                ("lexer", grammar_lexer_mod, set()),
                ("clause", grammar_clause_mod, {"lexer"}),
                ("chain", grammar_chain_mod, {"lexer"}),
                ("header", grammar_header_mod, {"lexer"}),
                ("flow", reflex_flow_mod, {"lexer", "clause", "chain", "header"}),
                ("compiler", reflex_compiler_mod, {"flow"}),
            ),
            error_cls=ShapeContractVerificationError,
        )

    @classmethod
    def run_fixtures(
        cls,
        fixture_dir: str | Path | None = None,
        *,
        case_filter: str | None = None,
    ) -> dict[str, str]:
        root = resolve_fixture_dir("contract", override_dir=fixture_dir, anchor_file=__file__)
        return run_fixture_suite(
            root,
            _dispatch_contract_case,
            case_filter=case_filter,
            error_cls=ShapeContractVerificationError,
        )

    @classmethod
    def run_all(
        cls,
        fixture_dir: str | Path | None = None,
        *,
        case_filter: str | None = None,
    ) -> dict[str, Any]:
        reports = cls.verify_units()
        scenarios = cls.run_fixtures(fixture_dir, case_filter=case_filter)
        return {
            "modules": {name: rep.to_summary() for name, rep in reports.items()},
            "total_scenarios": len(scenarios),
            "scenarios": scenarios,
        }

if __name__ == "__main__":
    flt = sys.argv[1] if len(sys.argv) > 1 else None
    sys.stdout.write(json.dumps(ShapeContractTester.run_all(case_filter=flt), indent=2, ensure_ascii=False) + "\n")