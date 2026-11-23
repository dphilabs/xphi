# xphi.arch.reflex.grammar.lexer
"""
Parser-agnostic CST nodes, ASCII identifier predicates, balanced-delimiter scanners,
and token stream primitives shared across `reflex.grammar.*` submodules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CLOSERS: dict[str, str] = {"(": ")", "[": "]", "{": "}"}

_SINGLE_CHAR_TOKENS: dict[str, str] = {
    ":": "COLON",
    ".": "DOT",
    "?": "QMARK",
    "[": "LBRACK",
    "]": "RBRACK",
    "<": "LT",
    ">": "GT",
    "/": "SLASH",
    "+": "PLUS",
    "-": "MINUS",
}

# Parser-Agnostic CST Nodes
@dataclass(frozen=True, slots=True)
class ParsedShape:
    base: str
    ext: str | None = None
    is_list: bool = False

@dataclass(frozen=True, slots=True)
class ParsedArgItem:
    key: str
    is_namespaced: bool
    optional: bool
    shape: ParsedShape | None
    has_default: bool
    default_value: Any = None

@dataclass(frozen=True, slots=True)
class ParsedAuth:
    provider: str
    scope: str | None = None

@dataclass(frozen=True, slots=True)
class ParsedHeaderTag:
    entry: str | None  # None for `@flow:`, entry name for `@flow.<entry>:`
    dsl: str
    lineno: int

@dataclass(frozen=True, slots=True)
class ParsedUnitHeader:
    unit: str
    tags: tuple[ParsedHeaderTag, ...]
    desc: str | None = None
    side_effect: str | None = None

@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # IDENT | COLON | DOT | QMARK | LBRACK | RBRACK | LT | GT | SLASH | PLUS | MINUS | OTHER | EOF
    value: str
    pos: int

# ASCII Predicates, Balanced Scanners & TokenStream
def is_ident_start(ch: str) -> bool:
    return ch == "_" or ("a" <= ch <= "z") or ("A" <= ch <= "Z")

def is_ident_part(ch: str) -> bool:
    return is_ident_start(ch) or ("0" <= ch <= "9")

def is_ident(text: str) -> bool:
    return bool(text) and is_ident_start(text[0]) and all(is_ident_part(c) for c in text)

def split_clause_items(body: str) -> list[str]:
    """Split a clause body on top-level commas while verifying balanced quotes and brackets."""
    if not body.strip():
        return []

    items: list[str] = []
    buf: list[str] = []
    stack: list[str] = []
    quote = ""
    escaped = False

    for ch in body:
        if quote:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue

        if ch in "'\"":
            quote = ch
        elif ch in CLOSERS:
            stack.append(CLOSERS[ch])
        elif ch in ")]}":
            if not stack or stack[-1] != ch:
                raise ValueError(f"Unbalanced bracket '{ch}' in clause '{body}'.")
            stack.pop()
        elif ch == "," and not stack:
            piece = "".join(buf).strip()
            if not piece:
                raise ValueError(f"Empty argument slot in clause '{body}'.")
            items.append(piece)
            buf = []
            continue
        buf.append(ch)

    if quote:
        raise ValueError(f"Unterminated string literal ({quote}) in clause '{body}'.")
    if stack:
        raise ValueError(f"Unclosed bracket (expected '{stack[-1]}') in clause '{body}'.")

    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    elif items:
        raise ValueError(f"Trailing comma in clause '{body}'.")
    return items


def split_top_level_assign(item: str) -> tuple[str, bool, str]:
    """Split `head = default_expr` on the first top-level `=` outside quotes and brackets."""
    depth = 0
    quote = ""
    escaped = False
    for idx, ch in enumerate(item):
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
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "=" and depth == 0:
            return item[:idx], True, item[idx + 1 :]
    return item, False, ""


def tokenize_head(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if is_ident_start(ch):
            start = i
            while i < n and is_ident_part(text[i]):
                i += 1
            tokens.append(Token("IDENT", text[start:i], start))
            continue
        if ch in _SINGLE_CHAR_TOKENS:
            tokens.append(Token(_SINGLE_CHAR_TOKENS[ch], ch, i))
            i += 1
            continue
        start = i
        while i < n and not text[i].isspace() and text[i] not in _SINGLE_CHAR_TOKENS:
            i += 1
        tokens.append(Token("OTHER", text[start:i], start))

    tokens.append(Token("EOF", "", n))
    return tokens


class TokenStream:
    __slots__ = ("raw", "tokens", "idx")

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.tokens = tokenize_head(raw)
        self.idx = 0

    def peek(self, offset: int = 0) -> Token:
        pos = min(self.idx + offset, len(self.tokens) - 1)
        return self.tokens[pos]

    def advance(self) -> Token:
        tok = self.tokens[self.idx]
        if self.idx < len(self.tokens) - 1:
            self.idx += 1
        return tok