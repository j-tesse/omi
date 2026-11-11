"""Strict JSON schema -> whitespace-free EBNF grammar for vLLM's `structured_outputs.grammar`.

Why: with `response_format: json_schema`, vLLM's xgrammar grammar allows arbitrary
whitespace between tokens. When Gemma wants to close an object before every
(strict = all-required) property is written, the only legal move left is more
whitespace, and it emits "\\n  \\n  ..." until max_tokens. vLLM can forbid that
only server-wide (`--structured-outputs-config.disable_any_whitespace`, which would
mean restarting the shared vLLM), and logit_bias is rejected with speculative
decoding. So the proxy compiles the schema itself into a compact grammar
(separators "," and ":", no whitespace), which removes the loop: after a value
the grammar only allows "," + the next key or the closing bracket.

Scope: the subset OpenAI strict mode allows (and openai/langchain's pydantic
conversion produces): object (properties, all required, additionalProperties
false), array (items, minItems/maxItems; unbounded arrays are capped at
LLM_COMPACT_JSON_MAX_ITEMS), string (minLength/maxLength, format
date-time/date/time), number / integer (a [0, 1] range, or minimum >= 0),
boolean, null, enum, const, anyOf / oneOf, $ref into $defs / definitions,
single-element allOf. Other value constraints (pattern, other numeric ranges)
are not enforced by the grammar; the client's pydantic validation still checks
them. Anything outside the subset raises Unsupported and the request is passed
through unchanged.
"""

from __future__ import annotations

import json
import os
import re


# Arrays without maxItems get this cap: Gemma can otherwise keep appending items
# (seen with ScreenFrameJudgement.labels) until max_tokens. 0 = no cap.
DEFAULT_MAX_ITEMS = int(os.getenv("LLM_COMPACT_JSON_MAX_ITEMS", "100") or 0)


class Unsupported(ValueError):
    pass


_PRIMITIVES = {
    "string": 'string',
    "number": 'number',
    "integer": 'integer',
    "boolean": 'boolean',
    "null": 'null',
}

_BASE = r'''
string ::= "\"" char* "\""
char ::= [^"\\\0-\x1f] | "\\" escape
escape ::= ["\\/bfnrt] | "u" [A-Fa-f0-9] [A-Fa-f0-9] [A-Fa-f0-9] [A-Fa-f0-9]
integer ::= "-"? ("0" | [1-9] [0-9]*)
number ::= integer ("." [0-9]+)? ([eE] [-+]? [0-9]+)?
boolean ::= "true" | "false"
null ::= "null"
digits ::= [0-9]+
unit_interval ::= ("0" ("." digits)? | "1" ("." "0"+)?) ([eE] "-" digits)?
nonneg_integer ::= "0" | [1-9] [0-9]*
nonneg_number ::= nonneg_integer ("." digits)? ([eE] [-+]? digits)?
date ::= [0-9] [0-9] [0-9] [0-9] "-" [0-1] [0-9] "-" [0-3] [0-9]
time ::= [0-2] [0-9] ":" [0-5] [0-9] ":" [0-6] [0-9] ("." digits)? ("Z" | [+-] [0-2] [0-9] ":" [0-5] [0-9])
date_time ::= date "T" time
'''

_IGNORED = {
    "title", "description", "default", "examples", "format", "pattern", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems",
    "maxItems", "uniqueItems", "$schema", "$id", "strict", "readOnly", "writeOnly", "deprecated",
}


def _lit(value) -> str:
    """EBNF literal for a JSON value (compact serialization)."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Compiler:
    def __init__(self, root: dict):
        self.root = root
        self.rules: dict[str, str] = {}
        self.refs: dict[str, str] = {}
        self.n = 0

    def rule(self, body: str, hint: str = "r") -> str:
        self.n += 1
        name = f"{re.sub(r'[^a-z0-9]', '_', hint.lower())[:24] or 'r'}_{self.n}"
        self.rules[name] = body
        return name

    def resolve(self, ref: str) -> dict:
        if not ref.startswith("#/"):
            raise Unsupported(f"external $ref {ref}")
        node = self.root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def compile(self, s, hint: str = "v") -> str:
        """Return an EBNF expression matching schema `s`."""
        if s is True or s == {}:
            raise Unsupported("unconstrained value")
        if not isinstance(s, dict):
            raise Unsupported(f"schema {s!r}")
        unknown = set(s) - _IGNORED - {
            "type", "properties", "required", "additionalProperties", "items", "enum", "const",
            "anyOf", "oneOf", "allOf", "$ref", "$defs", "definitions",
        }
        if unknown:
            raise Unsupported(f"keywords {sorted(unknown)}")
        if "$ref" in s:
            ref = s["$ref"]
            if ref not in self.refs:
                name = f"ref_{len(self.refs) + 1}"
                self.refs[ref] = name
                self.rules[name] = "PENDING"
                self.rules[name] = self.compile(self.resolve(ref), ref.rsplit("/", 1)[-1])
            return self.refs[ref]
        if "const" in s:
            return _lit(s["const"])
        if "enum" in s:
            return "(" + " | ".join(_lit(v) for v in s["enum"]) + ")"
        for key in ("anyOf", "oneOf"):
            if key in s:
                return "(" + " | ".join(self.compile(x, hint) for x in s[key]) + ")"
        if "allOf" in s:
            if len(s["allOf"]) != 1:
                raise Unsupported("allOf with several schemas")
            return self.compile(s["allOf"][0], hint)
        t = s.get("type")
        if isinstance(t, list):
            return "(" + " | ".join(self.compile({**s, "type": x}, hint) for x in t) + ")"
        if t == "string":
            fmt = {"date-time": "date_time", "date": "date", "time": "time"}.get(s.get("format", ""))
            if fmt:
                return f'("\\"" {fmt} "\\"")'
            lo, hi = s.get("minLength", 0), s.get("maxLength")
            if lo or hi is not None:
                return f'("\\"" char{{{lo},{"" if hi is None else hi}}} "\\"")'
            return "string"
        if t in ("number", "integer"):
            lo = s.get("minimum", s.get("exclusiveMinimum"))
            hi = s.get("maximum", s.get("exclusiveMaximum"))
            if lo is not None and lo >= 0 and hi is not None and hi <= 1 and t == "number":
                return "unit_interval"
            if lo is not None and lo >= 0:
                return "nonneg_integer" if t == "integer" else "nonneg_number"
            return _PRIMITIVES[t]
        if t in _PRIMITIVES:
            return _PRIMITIVES[t]
        if t == "array":
            item = self.rule(self.compile(s.get("items", True), hint + "_item"), hint + "_item")
            lo, hi = s.get("minItems", 0), s.get("maxItems")
            if hi is None and DEFAULT_MAX_ITEMS:
                hi = max(DEFAULT_MAX_ITEMS, lo)
            if hi == 0:
                return '"[]"'
            more = f'("," {item}){{{max(lo - 1, 0)},{"" if hi is None else hi - 1}}}'
            body = f"{item} {more}"
            return f'("[" {body} "]")' if lo > 0 else f'("[" ({body})? "]")'
        if t == "object":
            props = s.get("properties") or {}
            if s.get("additionalProperties", True) is not False:
                raise Unsupported("object allows additional properties")
            if set(s.get("required", [])) != set(props):
                raise Unsupported("object with optional properties")
            if not props:
                return '"{}"'
            parts = [f"{_lit(k)} \":\" {self.rule(self.compile(v, k), k)}" for k, v in props.items()]
            return '("{" ' + ' "," '.join(parts) + ' "}")'
        raise Unsupported(f"type {t!r}")


def grammar_for(schema: dict) -> str:
    """EBNF (xgrammar dialect, root rule `root`) accepting compact JSON for `schema`."""
    c = _Compiler(schema)
    root = c.compile(schema, "root")
    lines = [f"root ::= {root}"] + [f"{k} ::= {v}" for k, v in c.rules.items()]
    return "\n".join(lines) + _BASE
