# xphi.arch.reflex.grammar.chain
"""
Quote- and bracket-aware lexical scanners for pipeline chains (`->`),
multi-entry unit references (`unit:entry`), and step output remaps (`=> (...)`).
"""
from __future__ import annotations

from xphi.arch.reflex.grammar.lexer import (
    CLOSERS,
    is_ident,
    split_clause_items,
    split_top_level_assign,
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
        elif ch in CLOSERS:
            stack.append(CLOSERS[ch])
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


def split_unit_ref(unit_ref: str, *, strict: bool = False) -> tuple[str, str]:
    """
    Split `'unit'` or `'unit:entry'` into `(base_unit, entry)`.
    When `strict=True`, enforces `IDENT ( ':' IDENT )?` with no extra colons or inner spaces.
    """
    cleaned = unit_ref.strip()
    base, sep, entry = cleaned.partition(":")
    base_s, entry_s = base.strip(), entry.strip()
    if strict:
        if not is_ident(base_s):
            raise ValueError(f"Invalid unit reference '{cleaned}'.")
        if sep and (":" in entry_s or not is_ident(entry_s) or cleaned != f"{base_s}:{entry_s}"):
            raise ValueError(f"Invalid multi-entry unit reference '{cleaned}'.")
    return (base_s, entry_s) if sep else (base_s, "")


def split_remap(step_expr: str) -> tuple[str, dict[str, str]]:
    """
    Extract `=> (new_var=out_key, ...)` from a step expression using lexical scanning.
    Supports optional trailing recovery clause `(~>unit)` or `(!retry)` after `=> (...)`.
    """
    depth = 0
    quote = ""
    escaped = False
    arrow_idx = -1
    n = len(step_expr)

    i = 0
    while i < n:
        ch = step_expr[i]
        if quote:
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
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "=" and i + 1 < n and step_expr[i + 1] == ">":
            if arrow_idx != -1:
                raise ValueError(f"Multiple '=>' remap clauses in '{step_expr.strip()}'.")
            arrow_idx = i
            i += 2
            continue
        i += 1

    if arrow_idx == -1:
        return step_expr, {}

    prefix = step_expr[:arrow_idx].strip()
    after_arrow = step_expr[arrow_idx + 2 :].lstrip()
    if not prefix or not after_arrow.startswith("("):
        raise ValueError(f"Malformed output remap in '{step_expr.strip()}'.")

    p_depth = 0
    close_idx = -1
    for j, ch in enumerate(after_arrow):
        if ch == "(":
            p_depth += 1
        elif ch == ")":
            p_depth -= 1
            if p_depth == 0:
                close_idx = j
                break
    if close_idx == -1:
        raise ValueError(f"Malformed output remap in '{step_expr.strip()}'.")

    inner = after_arrow[1:close_idx]
    trailing = after_arrow[close_idx + 1 :].strip()
    # if trailing and not trailing.startswith("(!"):
    if trailing and not trailing.startswith(("(!", "(~>")):
        raise ValueError(f"Unexpected trailing token '{trailing}' after remap in '{step_expr.strip()}'.")
    if not inner.strip():
        raise ValueError(f"Empty output remap '=> ()' in '{step_expr.strip()}'.")

    remap: dict[str, str] = {}
    for pair in split_clause_items(inner):
        lhs, eq, rhs = split_top_level_assign(pair)
        new_k, old_k = lhs.strip(), rhs.strip()
        if not eq or not (is_ident(new_k) and is_ident(old_k)):
            raise ValueError(f"Invalid remap pair '{pair}' in '{step_expr.strip()}'.")
        if new_k in remap:
            raise ValueError(f"Duplicate remap target '{new_k}' in '{step_expr.strip()}'.")
        remap[new_k] = old_k

    recombined_head = f"{prefix}{trailing}" if trailing else prefix
    return recombined_head, remap