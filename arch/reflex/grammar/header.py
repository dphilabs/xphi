# xphi.arch.reflex.grammar.header
"""
Zero-regex script header scanners for `@unit:`, `@flow[.entry]:`, `@desc:`, `@side_effect:`,
and quote-safe `py(ref@path)` / `exec(ref@path)` clause target rewriting.
"""
from __future__ import annotations

from xphi.arch.reflex.grammar.lexer import (
    ParsedHeaderTag,
    ParsedUnitHeader,
    is_ident,
    is_ident_part,
    is_ident_start,
)


def parse_header_flow_tags(header_text: str) -> list[ParsedHeaderTag]:
    """
    Scan docstring/comment header lines for `@flow:` and `@flow.<entry>:` tags without regex.
    Enforces grammar v1.1: no whitespace inside `@flow[.entry]:`, single-line non-empty DSL.
    """
    tags: list[ParsedHeaderTag] = []
    for lineno, raw_line in enumerate(header_text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        idx = line.find("@flow")
        if idx == -1:
            continue
        if idx > 0 and (is_ident_part(line[idx - 1]) or line[idx - 1] == "@"):
            continue

        rest = line[idx + len("@flow") :]
        colon_idx = rest.find(":")
        if colon_idx == -1:
            continue

        tag_suffix = rest[:colon_idx]
        if any(c.isspace() for c in tag_suffix):
            raise ValueError(
                f"Whitespace is not allowed inside '@flow' tag at line {lineno}: '{raw_line.strip()}'."
            )

        if tag_suffix == "":
            entry: str | None = None
        elif tag_suffix.startswith("."):
            entry_name = tag_suffix[1:]
            if not is_ident(entry_name):
                raise ValueError(f"Invalid '@flow.<entry>:' identifier '{entry_name}' at line {lineno}.")
            entry = entry_name
        else:
            continue

        dsl = rest[colon_idx + 1 :].strip()
        if not dsl:
            raise ValueError(f"Empty flow expression after '@flow' tag at line {lineno}.")
        tags.append(ParsedHeaderTag(entry=entry, dsl=dsl, lineno=lineno))

    return tags


def parse_unit_header_meta(header_text: str) -> ParsedUnitHeader | None:
    """
    Scan script header lines for `@unit:`, `@flow[.entry]:`, `@desc:`, and `@side_effect:` without regex.
    Returns `None` when the file does not declare both `@unit:` and at least one `@flow` tag.
    """
    unit_name: str | None = None
    desc: str | None = None
    side_effect: str | None = None

    for lineno, raw_line in enumerate(header_text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("#"):
            line = line.lstrip("#").strip()

        for prefix in ("@unit", "@desc", "@side_effect"):
            idx = line.find(prefix)
            if idx == -1:
                continue
            if idx > 0 and (is_ident_part(line[idx - 1]) or line[idx - 1] == "@"):
                continue
            rest = line[idx + len(prefix) :]
            if not rest.startswith(":"):
                continue
            val = rest[1:].strip()
            if prefix == "@unit":
                if not is_ident(val):
                    raise ValueError(f"Invalid '@unit:' identifier '{val}' at line {lineno}.")
                if unit_name is not None and unit_name != val:
                    raise ValueError(
                        f"Multiple '@unit:' declarations ('{unit_name}', '{val}') at line {lineno}."
                    )
                unit_name = val
            elif prefix == "@desc" and val:
                desc = val
            elif prefix == "@side_effect":
                if not is_ident(val):
                    raise ValueError(f"Invalid '@side_effect:' value '{val}' at line {lineno}.")
                side_effect = val

    if unit_name is None:
        return None
    flow_tags = parse_header_flow_tags(header_text)
    if not flow_tags:
        return None
    return ParsedUnitHeader(
        unit=unit_name,
        tags=tuple(flow_tags),
        desc=desc,
        side_effect=side_effect,
    )


def prefix_target_clause(dsl: str, ref_prefix: str) -> str:
    """
    Quote-safe replacement for `py(path)` / `exec(path)` -> `py(<ref_prefix>@path)`.
    Leaves string literals and already-prefixed `ref@path` targets untouched.
    """
    if not ref_prefix:
        return dsl

    out: list[str] = []
    depth = 0
    quote = ""
    escaped = False
    i, n = 0, len(dsl)

    while i < n:
        ch = dsl[i]
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
        if ch in "([{":
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch in ")]}":
            depth = max(0, depth - 1)
            out.append(ch)
            i += 1
            continue

        if depth == 0 and is_ident_start(ch):
            start = i
            while i < n and is_ident_part(dsl[i]):
                i += 1
            ident = dsl[start:i]
            if ident in ("py", "exec") and i < n and dsl[i] == "(":
                arg_start = i + 1
                while arg_start < n and dsl[arg_start].isspace():
                    arg_start += 1
                close_idx = dsl.find(")", arg_start)
                if close_idx != -1 and "@" not in dsl[arg_start:close_idx]:
                    out.append(f"{ident}({ref_prefix}@")
                    i = arg_start
                    continue
            out.append(ident)
            continue

        out.append(ch)
        i += 1

    return "".join(out)