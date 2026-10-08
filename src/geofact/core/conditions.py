"""Implements: FA62 (conditions ``when`` evaluated at expansion time), FA59 (name lookup of placeholders).

Names, scopes and ``when`` expressions

The expansion (``core/expansion.py``) resolves placeholders (``${name}``,
``${name.key}``) and evaluates ``when`` conditions against a ``Scope``: the
declared parameters, plus a loop variable or the arguments of a module. Both need
the same lookup, so scope, lookup and failure type live here.

``when`` is a boolean or an expression, evaluated by a whitelisted AST walker,
never by ``eval``: ``and``, ``or``, ``not``, comparisons, names, ``name.key``,
numbers, texts, ``true``/``false`` and lists. Comparisons are type-safe (text and
number are never equal). ``${name}`` in an expression stands for the value; inside
quotes it is replaced by its text. ``and``/``or`` evaluate every operand (no
short-circuit), so a type error shows up for every parameter value.

Messages are German (ASCII transliteration); every failure carries its position.
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")

Loc = tuple[str, ...]


class ExpansionFail(Exception):
    """One configuration error of the expansion, at a position (tuple of parts)."""

    def __init__(self, loc: Loc, message: str) -> None:
        super().__init__(message)
        self.loc = tuple(str(part) for part in loc)
        self.message = message

    @property
    def location(self) -> str:
        return " -> ".join(self.loc)


def value_label(value: Any) -> str:
    """German name of a value's type for messages."""
    if isinstance(value, bool):
        return "Wahrheitswert"
    if isinstance(value, int):
        return "ganze Zahl"
    if isinstance(value, float):
        return "Zahl"
    if isinstance(value, str):
        return "Text"
    if isinstance(value, list):
        return "Liste"
    if isinstance(value, dict):
        return "Mapping"
    if value is None:
        return "null"
    return type(value).__name__


def text_of(value: Any) -> str:
    """A scalar as text; booleans become ``true``/``false`` (YAML spelling)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def kind_of(value: Any) -> str:
    """Comparison class of a value: bool, number, text or other."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    return "other"


@dataclass(frozen=True)
class Scope:
    """Visible names with their values and kinds (Parameter, Schleifenvariable,
    Modulargument). A child scope must not shadow a visible name."""

    values: Mapping[str, Any]
    kinds: Mapping[str, str]

    def child(self, name: str, value: Any, kind: str, loc: Loc) -> "Scope":
        if not isinstance(name, str) or not IDENTIFIER.match(name):
            raise ExpansionFail(
                loc, f"'{name}' ist kein Bezeichner ([A-Za-z_][A-Za-z0-9_]*)"
            )
        if name in self.values:
            raise ExpansionFail(
                loc,
                f"{kind} '{name}' verdeckt {self.kinds[name]} gleichen Namens - anderen Namen wählen",
            )
        return Scope({**self.values, name: value}, {**self.kinds, name: kind})

    def known(self) -> str:
        names = [f"{name} ({self.kinds[name]})" for name in self.values]
        return ", ".join(names) if names else "keine"


def lookup(reference: str, scope: Scope, loc: Loc) -> Any:
    """Value of ``name`` or ``name.key.key`` in ``scope`` (deep copy)."""
    shown = "${" + reference + "}"
    if not _REFERENCE.match(reference):
        raise ExpansionFail(
            loc,
            f"Platzhalter '{shown}' ist ungültig (erlaubt: ${{name}} oder ${{name.key}})",
        )
    parts = reference.split(".")
    head = parts[0]
    if head not in scope.values:
        raise ExpansionFail(
            loc,
            f"Platzhalter '{shown}': '{head}' ist weder ein deklarierter Parameter noch eine "
            f"Schleifenvariable oder ein Modulargument (bekannt: {scope.known()})",
        )
    value = scope.values[head]
    for depth, key in enumerate(parts[1:], start=1):
        prefix = ".".join(parts[:depth])
        if not isinstance(value, dict):
            raise ExpansionFail(
                loc,
                f"Platzhalter '{shown}': '{prefix}' ist {value_label(value)}, kein Mapping - "
                "'.key' geht nur bei Mapping-Werten",
            )
        if key not in value:
            raise ExpansionFail(
                loc,
                f"Platzhalter '{shown}': '{prefix}' hat keinen Schlüssel '{key}' "
                f"(vorhanden: {', '.join(map(str, value)) or 'keine'})",
            )
        value = value[key]
    return copy.deepcopy(value)


# --- when: whitelisted expression evaluator ---

_SYMBOLS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "in",
    ast.NotIn: "not in",
}
_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_NUMBER_TEXT = re.compile(r"^\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?\s*$")
_ALLOWED_HINT = (
    "erlaubt sind and, or, not, Vergleiche (==, !=, <, <=, >, >=, in, not in), Namen, "
    "name.key, Zahlen, Texte, true/false und Listen; keine Aufrufe, keine Indizes"
)


class _WhenEvaluator:
    def __init__(
        self,
        scope: Scope,
        loc: Loc,
        expression: str,
        placeholders: Mapping[str, str] | None = None,
    ) -> None:
        self.scope = scope
        self.loc = loc
        self.expression = expression
        # internal name in the parsed source -> placeholder reference (name or name.key)
        self.placeholders = dict(placeholders or {})

    def fail(self, message: str) -> ExpansionFail:
        return ExpansionFail(self.loc, f"when '{self.expression}': {message}")

    def show(self, node: ast.AST) -> str:
        """Source text of ``node`` with the placeholders as written (``${name}``)."""
        text = ast.unparse(node)
        for internal, reference in self.placeholders.items():
            text = text.replace(internal, "${" + reference + "}")
        return text

    def evaluate(self, node: ast.AST) -> Any:
        if isinstance(node, ast.BoolOp):
            # no short-circuit: every operand is evaluated, so a type error in a
            # later operand shows up for every parameter value
            values = []
            for operand in node.values:
                value = self.evaluate(operand)
                if not isinstance(value, bool):
                    raise self.fail(
                        f"'{self.show(operand)}' ergibt {value_label(value)} ({value!r}), "
                        "and/or brauchen true oder false"
                    )
                values.append(value)
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = self.evaluate(node.operand)
            if not isinstance(value, bool):
                raise self.fail(
                    f"not braucht true oder false, '{self.show(node.operand)}' ergibt "
                    f"{value_label(value)} ({value!r})"
                )
            return not value
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and kind_of(node.operand.value) == "number"
        ):
            return -node.operand.value
        if isinstance(node, ast.Compare):
            left = self.evaluate(node.left)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self.evaluate(comparator)
                if not self.compare(left, operator, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Name):
            return self.name(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            head = self.placeholders.get(node.value.id, node.value.id)
            return lookup(f"{head}.{node.attr}", self.scope, self.loc)
        if isinstance(node, ast.Constant) and kind_of(node.value) in (
            "bool",
            "number",
            "text",
        ):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.evaluate(element) for element in node.elts]
        raise self.fail(f"'{self.show(node)}' ist nicht erlaubt ({_ALLOWED_HINT})")

    def name(self, name: str) -> Any:
        if name in self.placeholders:
            return lookup(self.placeholders[name], self.scope, self.loc)
        if name in self.scope.values:
            return self.scope.values[name]
        if name in ("true", "True"):
            return True
        if name in ("false", "False"):
            return False
        raise self.fail(
            f"'{name}' ist weder ein deklarierter Parameter noch eine Schleifenvariable oder "
            f"ein Modulargument (bekannt: {self.scope.known()}); Text in Anführungszeichen setzen"
        )

    def mismatch(self, left: Any, symbol: str, right: Any) -> ExpansionFail:
        kinds = {kind_of(left), kind_of(right)}
        if kinds == {"number", "text"}:
            number, text = (left, right) if kind_of(left) == "number" else (right, left)
            if _NUMBER_TEXT.match(text):
                return self.fail(
                    f"Zahl {number!r} wird mit '{symbol}' gegen den Text {text!r} verglichen - "
                    f"Text und Zahl sind nie gleich und nicht geordnet: Zahl ohne "
                    f"Anführungszeichen schreiben ({text.strip()}) oder den Parameter als "
                    "integer/float deklarieren"
                )
        return self.fail(
            f"Vergleich von {value_label(left)} {left!r} mit {value_label(right)} {right!r} "
            f"('{symbol}') - beide Seiten müssen denselben Typ haben (Zahl/Zahl, Text/Text, "
            "true/false); Zahl ohne Anführungszeichen?"
        )

    def compare(self, left: Any, operator: ast.cmpop, right: Any) -> bool:
        symbol = _SYMBOLS.get(type(operator))
        if symbol is None:
            raise self.fail(f"Operator nicht erlaubt ({_ALLOWED_HINT})")
        if isinstance(operator, (ast.In, ast.NotIn)):
            if not isinstance(right, list):
                raise self.fail(
                    f"'{symbol}' braucht rechts eine Liste, erhalten {value_label(right)} ({right!r})"
                )
            for element in right:
                if kind_of(element) == "other" or kind_of(element) != kind_of(left):
                    raise self.mismatch(left, symbol, element)
            found = left in right
            return found if isinstance(operator, ast.In) else not found
        if (
            kind_of(left) == "other"
            or kind_of(right) == "other"
            or kind_of(left) != kind_of(right)
        ):
            raise self.mismatch(left, symbol, right)
        if isinstance(operator, _ORDERING) and kind_of(left) == "bool":
            raise self.fail(f"true/false lassen sich nicht mit '{symbol}' ordnen")
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right
        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        return left >= right


_WHEN_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r"}


def _when_source(expression: str, scope: Scope, loc: Loc) -> tuple[str, dict[str, str]]:
    """Python source of a when expression and its placeholder map.

    Outside quotes ``${name}``/``${name.key}`` becomes an internal name that the
    evaluator resolves (a keyword such as ``in`` stays a valid variable name).
    Inside quotes the placeholder is replaced by its text, like an embedded
    placeholder elsewhere (``'${city}' == 'leipzig'`` compares the value, not
    the word ``city``); a list, mapping or null there is an error."""
    placeholders: dict[str, str] = {}
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(expression):
        if expression.startswith("${", index):
            end = expression.find("}", index + 2)
            if end < 0:
                raise ExpansionFail(
                    loc,
                    f"when '{expression}': Platzhalter ist nicht geschlossen (fehlt '}}'?)",
                )
            reference = expression[index + 2 : end].strip()
            value = lookup(reference, scope, loc)
            if quote is None:
                internal = f"__geofact_ph{len(placeholders)}__"
                placeholders[internal] = reference
                out.append(internal)
            else:
                if isinstance(value, (dict, list)) or value is None:
                    raise ExpansionFail(
                        loc,
                        f"when '{expression}': Platzhalter '${{{reference}}}' ist "
                        f"{value_label(value)} und kann nicht in einen Text in Anführungszeichen "
                        "eingesetzt werden - Platzhalter ohne Anführungszeichen schreiben",
                    )
                text = text_of(value)
                for raw, escaped in _WHEN_ESCAPES.items():
                    text = text.replace(raw, escaped)
                out.append(text.replace(quote, "\\" + quote))
            index = end + 1
            continue
        char = expression[index]
        if quote is not None and char == "\\" and index + 1 < len(expression):
            out.append(expression[index : index + 2])
            index += 2
            continue
        if quote is None and char in "'\"":
            quote = char
        elif char == quote:
            quote = None
        out.append(char)
        index += 1
    return "".join(out), placeholders


def evaluate_when(value: Any, scope: Scope, loc: Loc) -> bool:
    """FA62: ``True`` keeps the node (or instance element), ``False`` drops it.
    ``value`` is a boolean or an expression text; anything else, an unreadable
    expression or a non-boolean result is an ``ExpansionFail`` at ``loc``."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ExpansionFail(
            loc,
            f"when muss true/false oder ein Ausdruck (Text) sein, erhalten "
            f"{value_label(value)} ({value!r})",
        )
    expression = value.strip()
    if "$${" in expression:
        raise ExpansionFail(
            loc, f"when '{expression}': '$${{' ist in Ausdrücken nicht erlaubt"
        )
    source, placeholders = _when_source(expression, scope, loc)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpansionFail(
            loc, f"when '{expression}': Ausdruck nicht lesbar ({exc.msg})"
        ) from None
    result = _WhenEvaluator(scope, loc, expression, placeholders).evaluate(tree.body)
    if not isinstance(result, bool):
        raise ExpansionFail(
            loc,
            f"when '{expression}' muss true oder false ergeben, ergibt aber "
            f"{value_label(result)} ({result!r})",
        )
    return result
