# xphi.arch.reflex.unit.scanner
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import importlib
from pathlib import Path
import re
from types import ModuleType
from typing import Any

SCAN_ROOT_NAME = "reflexor"
SKIP_DIR_NAMES = frozenset({"__pycache__", "node_modules", "build", "dist", "site-packages"})

UNIT_TAG_RE = re.compile(r"^@unit:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*$", re.MULTILINE)
FLOW_TAG_RE = re.compile(r"^@flow(?:\.([a-zA-Z_][a-zA-Z0-9_]*))?:\s*(\S.*)$")
PY_TARGET_RE = re.compile(r"\bpy\(\s*([^:\s)]+):([a-zA-Z_][a-zA-Z0-9_.]*)\s*\)")
FLOW_IN_RE = re.compile(r"\bin\(([^)]*)\)")

class UnitVerificationError(AssertionError):
    """Raised when discovered @unit modules fail AST, symbol binding, or DAG checks."""

@dataclass(frozen=True, slots=True)
class DiscoveredUnit:
    unit: str
    module_name: str
    rel_path: str
    path: Path
    tree: ast.Module
    flow_tags: tuple[str, ...]


@dataclass(slots=True)
class UnitAstReport:
    unit: str
    module: str
    rel_path: str
    flow_tags: list[str] = field(default_factory=list)
    bound_symbols: list[str] = field(default_factory=list)
    imported_units: list[str] = field(default_factory=list)
    checked_functions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "module": self.module,
            "rel_path": self.rel_path,
            "flow_tags": self.flow_tags,
            "bound_symbols": self.bound_symbols,
            "imported_units": self.imported_units,
            "checked_functions": self.checked_functions,
            "warnings": self.warnings,
        }


def find_unit_root() -> Path:
    """Locate `reflexor/` from cwd first, then from this file's ancestors (fiber/ sibling layout)."""
    for base in (Path.cwd(), *Path(__file__).resolve().parents):
        candidate = base / SCAN_ROOT_NAME
        if candidate.is_dir():
            return candidate
    raise UnitVerificationError(f"Cannot locate '{SCAN_ROOT_NAME}/' root directory.")


def _iter_py_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for py_path in root.rglob("*.py"):
        rel_dirs = py_path.relative_to(root).parts[:-1]
        if any(d.startswith(".") or d in SKIP_DIR_NAMES for d in rel_dirs):
            continue
        out.append(py_path)
    return sorted(out)


def discover_tagged_units(unit_root: Path | None = None) -> dict[str, DiscoveredUnit]:
    """Scan all *.py under `reflexor/` via AST and index modules whose docstring declares `@unit: <name>`."""
    root = (unit_root or find_unit_root()).resolve()
    repo_root = root.parent
    index: dict[str, DiscoveredUnit] = {}

    for py_path in _iter_py_files(root):
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        except SyntaxError as exc:
            raise UnitVerificationError(f"SyntaxError in '{py_path}': {exc}") from exc

        doc = ast.get_docstring(tree)
        if not doc:
            continue
        m = UNIT_TAG_RE.search(doc)
        if not m:
            continue

        unit_name = m.group(1)
        rel = py_path.relative_to(repo_root)
        rel_parts = rel.with_suffix("").parts
        mod_name = ".".join(rel_parts[:-1] if rel_parts[-1] == "__init__" else rel_parts)
        flows = tuple(line.strip() for line in doc.splitlines() if line.strip().startswith("@flow"))

        if unit_name in index:
            raise UnitVerificationError(
                f"Duplicate @unit '{unit_name}' in '{py_path}' and '{index[unit_name].path}'."
            )
        index[unit_name] = DiscoveredUnit(
            unit=unit_name,
            module_name=mod_name,
            rel_path=rel.as_posix(),
            path=py_path,
            tree=tree,
            flow_tags=flows,
        )

    return index


def load_unit_module(index: dict[str, DiscoveredUnit], unit_name: str) -> ModuleType:
    if unit_name not in index:
        raise KeyError(f"@unit '{unit_name}' not found under {SCAN_ROOT_NAME}/. Discovered: {sorted(index)}")
    return importlib.import_module(index[unit_name].module_name)


def _collect_ast_callable_params(tree: ast.Module) -> dict[str, tuple[set[str], bool]]:
    """Map top-level `func` and `Class.method` -> (param_names, has_var_kw)."""
    symbols: dict[str, tuple[set[str], bool]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = {a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
            symbols[node.name] = (params - {"self", "cls"}, node.args.kwarg is not None)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = {a.arg for a in (*item.args.posonlyargs, *item.args.args, *item.args.kwonlyargs)}
                    symbols[f"{node.name}.{item.name}"] = (params - {"self", "cls"}, item.args.kwarg is not None)
    return symbols


def _parse_flow_in_names(flow_line: str) -> list[str]:
    m = FLOW_IN_RE.search(flow_line)
    if not m or not m.group(1).strip():
        return []
    names: list[str] = []
    for raw_item in m.group(1).split(","):
        head = raw_item.split(":", 1)[0].split("=", 1)[0].strip().rstrip("?")
        if head:
            names.append(head)
    return names


def analyze_unit_ast(
    target: DiscoveredUnit,
    index: dict[str, DiscoveredUnit],
    *,
    allowed_peer_units: set[str] | None = None,
    require_flow_tags: bool = True,
) -> UnitAstReport:
    rep = UnitAstReport(
        unit=target.unit,
        module=target.module_name,
        rel_path=target.rel_path,
        flow_tags=list(target.flow_tags),
    )
    mod_to_unit = {d.module_name: u for u, d in index.items()}
    callable_map = _collect_ast_callable_params(target.tree)

    # 1. __future__.annotations 검사
    future_names = {
        alias.name
        for node in target.tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
        for alias in node.names
    }
    if "annotations" not in future_names:
        rep.errors.append("Missing 'from __future__ import annotations'.")

    # 2. @flow 문법 + py(path:Symbol) 바인딩 + in(...) 인자 정합성 검사
    if not target.flow_tags and require_flow_tags:
        rep.errors.append(f"@unit '{target.unit}' declares no @flow tags.")

    for tag in target.flow_tags:
        if not FLOW_TAG_RE.match(tag):
            rep.errors.append(f"Malformed flow.grammar.v1.1 tag: {tag}")
            continue

        py_match = PY_TARGET_RE.search(tag)
        if py_match:
            decl_path, qual_sym = py_match.group(1), py_match.group(2)
            rep.bound_symbols.append(qual_sym)
            if decl_path != target.rel_path:
                rep.warnings.append(f"@flow path drift: declared '{decl_path}' != actual '{target.rel_path}'.")
            if qual_sym not in callable_map:
                rep.errors.append(f"@flow target '{qual_sym}' not found in '{target.rel_path}' AST.")
            else:
                ast_params, has_kw = callable_map[qual_sym]
                in_names = _parse_flow_in_names(tag)
                missing_in = [n for n in in_names if n not in ast_params and not has_kw]
                if missing_in:
                    rep.errors.append(
                        f"@flow '{qual_sym}' declares in({missing_in}) missing from Python signature {sorted(ast_params)}."
                    )

    # 3. 런타임 @unit 간 의존성 수집 및 허용 경계 검사 (TYPE_CHECKING 제외)
    runtime_mods: list[str] = []
    for node in target.tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and sub.module:
                runtime_mods.append(sub.module)
            elif isinstance(sub, ast.Import):
                runtime_mods.extend(a.name for a in sub.names)

    seen_peers: dict[str, None] = {}
    for mod_name in runtime_mods:
        peer = mod_to_unit.get(mod_name)
        if peer and peer != target.unit:
            seen_peers[peer] = None
            if allowed_peer_units is not None and peer not in allowed_peer_units:
                rep.errors.append(
                    f"Forbidden @unit dependency: '{target.unit}' -> '{peer}' (allowed: {sorted(allowed_peer_units)})."
                )
    rep.imported_units = list(seen_peers.keys())

    # 4. 함수 어노테이션 & 데이터클래스 slots=True 검사
    for node in ast.walk(target.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rep.checked_functions.append(node.name)
            if node.returns is None:
                rep.errors.append(f"Function '{node.name}' missing return type annotation.")
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if arg.arg not in ("self", "cls") and arg.annotation is None:
                    rep.errors.append(f"Function '{node.name}' arg '{arg.arg}' missing annotation.")

        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") == "dataclass":
                    kw_map = {kw.arg: kw.value for kw in dec.keywords}
                    slot_node = kw_map.get("slots")
                    if not (isinstance(slot_node, ast.Constant) and slot_node.value is True):
                        rep.errors.append(f"Dataclass '{node.name}' must declare slots=True.")

    return rep

def detect_unit_cycles(dep_graph: dict[str, list[str]]) -> list[list[str]]:
    """Detect circular dependencies across discovered `@unit` modules."""
    state: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=done
    stack: list[str] = []
    cycles: list[list[str]] = []

    def _dfs(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in dep_graph.get(node, ()):
            st = state.get(nxt, 0)
            if st == 1:
                idx = stack.index(nxt)
                cycles.append([*stack[idx:], nxt])
            elif st == 0:
                _dfs(nxt)
        stack.pop()
        state[node] = 2

    for u in sorted(dep_graph):
        if state.get(u, 0) == 0:
            _dfs(u)
    return cycles