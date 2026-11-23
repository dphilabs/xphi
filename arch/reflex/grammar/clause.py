# xphi.arch.reflex.grammar.clause
"""
LL recursive-descent parsers for `Shape`, `in/cfg/out` argument clauses, `auth(...)`,
and `mcp(server/tool)` target expressions.
"""
from __future__ import annotations

import ast
from typing import Any

from xphi.arch.reflex.grammar.lexer import (
    ParsedArgItem,
    ParsedAuth,
    ParsedShape,
    TokenStream,
    split_clause_items,
    split_top_level_assign,
)

_JSON_LITERAL_HINTS: dict[str, str] = {
    "true": "True",
    "false": "False",
    "null": "None",
}


def parse_shape_expr(raw_shape: str) -> ParsedShape:
    """Grammar: `shape ::= LOWER_IDENT ( '<' qualifier '>' )? ( '[' ']' )?`"""
    cleaned = raw_shape.strip()
    if not cleaned:
        raise ValueError("Unknown shape ''.")

    st = TokenStream(cleaned)
    base_tok = st.advance()
    if (
        base_tok.kind != "IDENT"
        or not base_tok.value.isascii()
        or not base_tok.value.isalpha()
        or not base_tok.value.islower()
    ):
        raise ValueError(f"Unknown shape '{cleaned}'.")

    ext: str | None = None
    if st.peek().kind == "LT":
        lt_tok = st.advance()
        gt_idx = cleaned.find(">", lt_tok.pos + 1)
        if gt_idx == -1:
            raise ValueError(f"Unknown shape '{cleaned}'.")
        ext_body = cleaned[lt_tok.pos + 1 : gt_idx]
        if not ext_body or any(c.isspace() for c in ext_body):
            raise ValueError(f"Unknown shape '{cleaned}'.")
        if not all((c.isascii() and c.isalnum()) or c in "_.+/-" for c in ext_body):
            raise ValueError(f"Unknown shape '{cleaned}'.")
        ext = ext_body
        while st.peek().kind != "EOF" and st.peek().pos <= gt_idx:
            st.advance()

    is_list = False
    if st.peek().kind == "LBRACK":
        lb = st.advance()
        rb = st.peek()
        if lb.pos > 0 and cleaned[lb.pos - 1].isspace():
            raise ValueError(f"Unknown shape '{cleaned}'.")
        if rb.kind != "RBRACK" or rb.pos != lb.pos + 1:
            raise ValueError(f"Unknown shape '{cleaned}'.")
        st.advance()
        is_list = True

    if st.peek().kind != "EOF":
        raise ValueError(f"Unknown shape '{cleaned}'.")

    return ParsedShape(base=base_tok.value, ext=ext, is_list=is_list)


def _eval_python_literal(raw_default: str, full_item: str) -> Any:
    cleaned = raw_default.strip()
    if not cleaned:
        raise ValueError(f"Missing default literal after '=' in '{full_item}'.")
    if cleaned in _JSON_LITERAL_HINTS:
        hint = _JSON_LITERAL_HINTS[cleaned]
        raise ValueError(
            f"JSON literal '{cleaned}' is not allowed in '{full_item}'; use Python literal '{hint}'."
        )
    try:
        return ast.literal_eval(cleaned)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f"Invalid Python literal '{cleaned}' in '{full_item}': {exc}") from exc


def _parse_key_head(st: TokenStream, head_str: str, full_item: str) -> tuple[str, bool, bool]:
    """
    LL(2) Key Grammar:
      key_expr     ::= IDENT ( ':' IDENT '.' dotted_ident | '.' dotted_ident )? '?'?
      dotted_ident ::= IDENT ( '.' IDENT )*
    """
    first = st.advance()
    if first.kind != "IDENT":
        raise ValueError(f"Invalid contract key '{head_str}' in '{full_item}'.")

    parts = [first.value]
    is_namespaced = False

    if st.peek().kind == "COLON":
        after_colon = st.peek(1)
        if after_colon.kind != "IDENT":
            raise ValueError(f"Invalid contract key '{head_str}' in '{full_item}'.")

        after_ident = st.peek(2)
        if after_ident.kind == "DOT":
            colon_tok = st.peek(0)
            if colon_tok.pos != first.pos + len(first.value) or after_colon.pos != colon_tok.pos + 1:
                raise ValueError(f"Invalid contract key '{head_str}' in '{full_item}'.")
            st.advance()  # ':'
            entry_tok = st.advance()  # entry IDENT
            parts.append(f":{entry_tok.value}")
            is_namespaced = True
        elif after_ident.kind == "COLON":
            raise ValueError(f"Invalid contract key '{head_str}' in '{full_item}'.")

    if is_namespaced and st.peek().kind != "DOT":
        raise ValueError(f"Invalid contract key '{head_str}' in '{full_item}'.")

    while st.peek().kind == "DOT":
        dot_tok = st.advance()
        seg = st.advance()
        if seg.kind != "IDENT" or seg.pos != dot_tok.pos + 1:
            raise ValueError(f"Invalid contract key '{head_str}' in '{full_item}'.")
        parts.append(f".{seg.value}")
        is_namespaced = True

    optional = False
    if st.peek().kind == "QMARK":
        st.advance()
        optional = True

    return "".join(parts), is_namespaced, optional


def parse_arg_clause(body: str) -> list[ParsedArgItem]:
    """Parse an entire `in(...)` / `cfg(...)` / `out(...)` clause body into `ParsedArgItem` nodes."""
    parsed_items: list[ParsedArgItem] = []
    seen_keys: set[str] = set()

    for item in split_clause_items(body):
        raw_head, has_default, raw_default = split_top_level_assign(item)
        head_str = raw_head.strip()
        if not head_str:
            raise ValueError(f"Invalid contract key '' in '{item}'.")

        st = TokenStream(head_str)
        key, is_namespaced, optional = _parse_key_head(st, head_str, item)

        if is_namespaced and optional:
            raise ValueError(f"Namespaced config key '{key}' cannot be marked optional ('?').")

        shape_node: ParsedShape | None = None
        if st.peek().kind == "COLON":
            colon_tok = st.advance()
            raw_shape_slice = head_str[colon_tok.pos + 1 :].strip()
            if not raw_shape_slice:
                raise ValueError(f"Invalid contract key '{head_str}' in '{item}'.")
            shape_node = parse_shape_expr(raw_shape_slice)
        elif st.peek().kind != "EOF":
            raise ValueError(f"Invalid contract key '{head_str}' in '{item}'.")

        if key in seen_keys:
            raise ValueError(f"Duplicate contract key '{key}' in '{body}'.")
        seen_keys.add(key)

        default_val = _eval_python_literal(raw_default, item) if has_default else None
        parsed_items.append(
            ParsedArgItem(
                key=key,
                is_namespaced=is_namespaced,
                optional=optional,
                shape=shape_node,
                has_default=has_default,
                default_value=default_val,
            )
        )

    return parsed_items


def parse_auth_token(token: str) -> ParsedAuth:
    """Grammar: `auth_req ::= LOWER_PROVIDER ( ':' SCOPE_CHARS )?`"""
    cleaned = token.strip()
    provider, has_colon, scope = cleaned.partition(":")
    if (
        not provider
        or not ("a" <= provider[0] <= "z")
        or not all(("a" <= c <= "z") or ("0" <= c <= "9") or c in "_-" for c in provider)
    ):
        raise ValueError(f"Invalid auth provider '{provider.strip()}' in '{cleaned}'.")

    if not has_colon:
        return ParsedAuth(provider=provider, scope=None)

    if (
        not scope
        or any(c.isspace() for c in scope)
        or not all((c.isascii() and c.isalnum()) or c in "_.:/-" for c in scope)
    ):
        raise ValueError(f"Invalid auth scope '{scope}' in '{cleaned}'.")
    return ParsedAuth(provider=provider, scope=scope)


def parse_mcp_target_expr(target: str) -> tuple[str, str]:
    """Grammar: `mcp_target ::= SERVER '/' TOOL`"""
    cleaned = target.strip()
    server, has_slash, tool = cleaned.partition("/")
    valid_server = bool(server) and all((c.isascii() and c.isalnum()) or c in "_.-" for c in server)
    valid_tool = (
        bool(tool)
        and not any(c.isspace() for c in tool)
        and all((c.isascii() and c.isalnum()) or c in "_.:/-" for c in tool)
    )
    if not (has_slash and valid_server and valid_tool):
        raise ValueError(f"Invalid mcp target '{cleaned}', expected 'server/tool'.")
    return server, tool