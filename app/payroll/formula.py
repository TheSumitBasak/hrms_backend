"""The §5.4 payroll formula language: parse, validate, order and evaluate.

Formulas are parsed by a hand-written recursive-descent parser into a small AST (PSTR-10);
nothing here uses ``eval``, ``exec`` or Python's own ``ast``, because the latter turns ``0.4``
into a float and PINV-01 forbids floating point anywhere near money.

Evaluation uses the PINV-02 decimal context (34 digits, ``ROUND_HALF_UP``) and applies no
rounding unless the formula calls a rounding function (PSTR-12). ``IF`` evaluates only the
branch it takes, so a division by zero in the other branch is not an error.

Errors map to the §20 codes: :class:`FormulaSyntaxError` -> ``PAY_FORMULA_INVALID``,
:class:`FormulaCycleError` -> ``PAY_FORMULA_CYCLE``, :class:`FormulaEvaluationError` ->
``PAY_FORMULA_ERROR`` (employee-level, PSTR-13).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from typing import Union

MAX_LENGTH = 1000
MAX_DEPTH = 32
MAX_REFERENCES = 64

VARIABLES = frozenset(
    {
        "CTC_ANNUAL",
        "CTC_MONTHLY",
        "PAID_DAYS",
        "PERIOD_DAYS",
        "SEGMENT_DAYS",
        "SERVICE_YEARS",
        "AGE_YEARS",
        "IS_METRO_RENT",
        "MIN_WAGE_MONTHLY",
        "PF_CEILING",
    }
)
FUNCTIONS = frozenset({"MIN", "MAX", "ROUND", "ROUND_UP", "ROUND_DOWN", "IF", "CLAMP"})

Value = Union[Decimal, bool]  # noqa: UP007 - runtime alias, kept explicit for readability


class FormulaError(ValueError):
    """Base class; ``code`` is the §20 error code."""

    code = "PAY_FORMULA_INVALID"


class FormulaSyntaxError(FormulaError):
    code = "PAY_FORMULA_INVALID"


class FormulaCycleError(FormulaError):
    code = "PAY_FORMULA_CYCLE"


class FormulaEvaluationError(FormulaError):
    code = "PAY_FORMULA_ERROR"


def decimal_context() -> Context:
    """PINV-02. A fresh context per calculation; the global one is never touched."""
    return Context(
        prec=34,
        rounding=ROUND_HALF_UP,
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


# ---------------------------------------------------------------------------------- AST


@dataclass(frozen=True, slots=True)
class Num:
    value: Decimal


@dataclass(frozen=True, slots=True)
class Ref:
    kind: str  # "C" component or "V" variable
    name: str


@dataclass(frozen=True, slots=True)
class Unary:
    op: str  # "-" or "NOT"
    operand: Node


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    args: tuple[Node, ...]


Node = Union[Num, Ref, Unary, Binary, Call]  # noqa: UP007


# ------------------------------------------------------------------------------- lexer

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<num>\d+(?:\.\d+)?)
      | (?P<ref>[CV]\.[A-Z][A-Z0-9_]*)
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op><=|>=|==|!=|[-+*/(),<>])
    )""",
    re.VERBOSE,
)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise FormulaSyntaxError(f"unexpected character at position {position}")
        position = match.end()
        kind = match.lastgroup
        if kind is None:  # pragma: no cover - every alternative is a named group
            raise FormulaSyntaxError(f"unexpected character at position {position}")
        tokens.append((kind, match.group(kind)))
    return tokens


# ------------------------------------------------------------------------------ parser


class _Parser:
    """Precedence: OR < AND < NOT < comparison < +,- < *,/ < unary - < atom."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self._tokens = tokens
        self._index = 0
        self._depth = 0

    def parse(self) -> Node:
        node = self._or()
        if self._index != len(self._tokens):
            raise FormulaSyntaxError(f"unexpected {self._tokens[self._index][1]!r}")
        return node

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise FormulaSyntaxError("unexpected end of formula")
        self._index += 1
        return token

    def _is(self, *values: str) -> bool:
        token = self._peek()
        return token is not None and token[1].upper() in values and token[0] in {"op", "word"}

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise FormulaSyntaxError(f"formula is nested deeper than {MAX_DEPTH} levels")

    def _or(self) -> Node:
        self._enter()
        node = self._and()
        while self._is("OR"):
            self._take()
            node = Binary("OR", node, self._and())
        self._depth -= 1
        return node

    def _and(self) -> Node:
        node = self._not()
        while self._is("AND"):
            self._take()
            node = Binary("AND", node, self._not())
        return node

    def _not(self) -> Node:
        if self._is("NOT"):
            self._take()
            self._enter()
            node = Unary("NOT", self._not())
            self._depth -= 1
            return node
        return self._comparison()

    def _comparison(self) -> Node:
        node = self._additive()
        if self._is("<", "<=", ">", ">=", "==", "!="):
            op = self._take()[1]
            node = Binary(op, node, self._additive())
        return node

    def _additive(self) -> Node:
        node = self._multiplicative()
        while self._is("+", "-"):
            op = self._take()[1]
            node = Binary(op, node, self._multiplicative())
        return node

    def _multiplicative(self) -> Node:
        node = self._unary()
        while self._is("*", "/"):
            op = self._take()[1]
            node = Binary(op, node, self._unary())
        return node

    def _unary(self) -> Node:
        if self._is("-"):
            self._take()
            self._enter()
            node = Unary("-", self._unary())
            self._depth -= 1
            return node
        return self._atom()

    def _atom(self) -> Node:
        kind, value = self._take()
        if kind == "num":
            return Num(Decimal(value))  # from the source text: never via float
        if kind == "ref":
            prefix, name = value.split(".", 1)
            if prefix == "V" and name not in VARIABLES:
                raise FormulaSyntaxError(f"unknown variable V.{name}")
            return Ref(prefix, name)
        if kind == "op" and value == "(":
            self._enter()
            node = self._or()
            self._depth -= 1
            self._expect(")")
            return node
        if kind == "word":
            name = value.upper()
            if name not in FUNCTIONS:
                raise FormulaSyntaxError(f"unknown function or word {value!r}")
            self._expect("(")
            args: list[Node] = []
            if not self._is(")"):
                self._enter()
                args.append(self._or())
                while self._is(","):
                    self._take()
                    args.append(self._or())
                self._depth -= 1
            self._expect(")")
            _check_arity(name, len(args))
            return Call(name, tuple(args))
        raise FormulaSyntaxError(f"unexpected {value!r}")

    def _expect(self, value: str) -> None:
        if not self._is(value):
            raise FormulaSyntaxError(f"expected {value!r}")
        self._take()


_ARITY: dict[str, tuple[int, int | None]] = {
    "MIN": (1, None),
    "MAX": (1, None),
    "ROUND": (2, 2),
    "ROUND_UP": (2, 2),
    "ROUND_DOWN": (2, 2),
    "IF": (3, 3),
    "CLAMP": (3, 3),
}


def _check_arity(name: str, count: int) -> None:
    low, high = _ARITY[name]
    if count < low or (high is not None and count > high):
        raise FormulaSyntaxError(f"{name} takes {low if high == low else f'{low}+'} argument(s)")


def parse(text: str) -> Node:
    """Parse ``text`` or raise :class:`FormulaSyntaxError` (PSTR-15 limits enforced)."""
    if not text or not text.strip():
        raise FormulaSyntaxError("formula is empty")
    if len(text) > MAX_LENGTH:
        raise FormulaSyntaxError(f"formula is longer than {MAX_LENGTH} characters")
    node = _Parser(_tokenize(text)).parse()
    if len(references(node)) > MAX_REFERENCES:
        raise FormulaSyntaxError(f"formula has more than {MAX_REFERENCES} references")
    if depth(node) > MAX_DEPTH:
        raise FormulaSyntaxError(f"formula is nested deeper than {MAX_DEPTH} levels")
    return node


def depth(node: Node) -> int:
    """Height of the AST; a chain of 40 additions is 40 deep even without parentheses."""
    if isinstance(node, Unary):
        return 1 + depth(node.operand)
    if isinstance(node, Binary):
        return 1 + max(depth(node.left), depth(node.right))
    if isinstance(node, Call):
        return 1 + max((depth(arg) for arg in node.args), default=0)
    return 1


# ------------------------------------------------------------------------ introspection


def references(node: Node) -> list[Ref]:
    """Every reference in source order, duplicates included."""
    if isinstance(node, Ref):
        return [node]
    if isinstance(node, Unary):
        return references(node.operand)
    if isinstance(node, Binary):
        return references(node.left) + references(node.right)
    if isinstance(node, Call):
        return [ref for arg in node.args for ref in references(arg)]
    return []


def component_codes(node: Node) -> frozenset[str]:
    return frozenset(ref.name for ref in references(node) if ref.kind == "C")


def canonical(node: Node) -> str:
    """A whitespace- and case-insensitive rendering used for the AST hash (PSTR-14)."""
    if isinstance(node, Num):
        return format(node.value.normalize(), "f")
    if isinstance(node, Ref):
        return f"{node.kind}.{node.name}"
    if isinstance(node, Unary):
        return f"({node.op} {canonical(node.operand)})"
    if isinstance(node, Binary):
        return f"({node.op} {canonical(node.left)} {canonical(node.right)})"
    return f"{node.name}({','.join(canonical(arg) for arg in node.args)})"


def ast_hash(node: Node) -> str:
    """SHA-256 of the normalized AST, so ``C.A*0.4`` and ``C.A * 0.40`` hash alike."""
    return hashlib.sha256(canonical(node).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------- ordering


def evaluation_order(
    formulas: Mapping[str, Node], display_order: Mapping[str, int] | None = None
) -> list[str]:
    """Topological order of the components that have formulas (PSTR-11).

    A component depends on the formula components it references; references to components
    without a formula are plain inputs and impose no order. Ties break by ``display_order``
    then code. A cycle raises :class:`FormulaCycleError` naming the components involved.
    """
    order = display_order or {}
    deps = {code: component_codes(node) & formulas.keys() for code, node in formulas.items()}
    remaining = dict(deps)
    result: list[str] = []
    while remaining:
        ready = sorted(
            (code for code, needs in remaining.items() if not needs & remaining.keys()),
            key=lambda code: (order.get(code, 0), code),
        )
        if not ready:
            raise FormulaCycleError(
                "formulas reference each other in a cycle: " + ", ".join(sorted(remaining))
            )
        first = ready[0]
        result.append(first)
        del remaining[first]
    return result


# ----------------------------------------------------------------------------- evaluation

_QUANTUM = Decimal(1)


def _as_number(value: Value) -> Decimal:
    if isinstance(value, bool):
        raise FormulaEvaluationError("a yes/no value was used where a number is needed")
    return value


def _as_bool(value: Value) -> bool:
    if not isinstance(value, bool):
        raise FormulaEvaluationError("a number was used where a yes/no value is needed")
    return value


def _round(value: Decimal, digits: Decimal, mode: str, ctx: Context) -> Decimal:
    if digits != digits.to_integral_value():
        raise FormulaEvaluationError("the number of decimal places must be a whole number")
    # §6.8: ROUND_UP / ROUND_DOWN are ceiling / floor on positive amounts.
    return value.quantize(_QUANTUM.scaleb(-int(digits)), rounding=mode, context=ctx)


def evaluate(node: Node, values: Mapping[str, Decimal | bool]) -> Decimal | bool:
    """Evaluate ``node``. ``values`` maps ``"C.CODE"`` / ``"V.NAME"`` to numbers or booleans.

    Raises :class:`FormulaEvaluationError` for an unknown reference, division by zero or a
    number/boolean type mismatch.
    """
    ctx = decimal_context()
    try:
        return _eval(node, values, ctx)
    except (DivisionByZero, InvalidOperation, Overflow) as error:
        raise FormulaEvaluationError(f"arithmetic error: {type(error).__name__}") from error


def _eval(node: Node, values: Mapping[str, Decimal | bool], ctx: Context) -> Decimal | bool:  # noqa: PLR0911
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Ref):
        key = f"{node.kind}.{node.name}"
        if key not in values:
            raise FormulaEvaluationError(f"no value for {key}")
        return values[key]
    if isinstance(node, Unary):
        operand = _eval(node.operand, values, ctx)
        if node.op == "NOT":
            return not _as_bool(operand)
        return ctx.minus(_as_number(operand))
    if isinstance(node, Binary):
        if node.op in {"AND", "OR"}:  # short-circuit, so a guarded division is safe
            left = _as_bool(_eval(node.left, values, ctx))
            if node.op == "AND" and not left:
                return False
            if node.op == "OR" and left:
                return True
            return _as_bool(_eval(node.right, values, ctx))
        left_value = _eval(node.left, values, ctx)
        right_value = _eval(node.right, values, ctx)
        if node.op in {"==", "!="}:
            equal = left_value == right_value and type(left_value) is type(right_value)
            return equal if node.op == "==" else not equal
        a, b = _as_number(left_value), _as_number(right_value)
        arithmetic: dict[str, Callable[[Decimal, Decimal], Decimal | bool]] = {
            "+": ctx.add,
            "-": ctx.subtract,
            "*": ctx.multiply,
            "/": ctx.divide,
            "<": lambda x, y: x < y,
            "<=": lambda x, y: x <= y,
            ">": lambda x, y: x > y,
            ">=": lambda x, y: x >= y,
        }
        return arithmetic[node.op](a, b)
    return _call(node, values, ctx)


def _call(node: Call, values: Mapping[str, Decimal | bool], ctx: Context) -> Decimal | bool:
    if node.name == "IF":  # lazy: only the chosen branch is evaluated
        branch = node.args[1] if _as_bool(_eval(node.args[0], values, ctx)) else node.args[2]
        return _eval(branch, values, ctx)
    args = [_as_number(_eval(arg, values, ctx)) for arg in node.args]
    if node.name == "MIN":
        return min(args)
    if node.name == "MAX":
        return max(args)
    if node.name == "CLAMP":
        value, low, high = args
        if low > high:
            raise FormulaEvaluationError("CLAMP lower bound is above the upper bound")
        return max(low, min(value, high))
    mode = {"ROUND": ROUND_HALF_UP, "ROUND_UP": ROUND_CEILING, "ROUND_DOWN": ROUND_FLOOR}[node.name]
    return _round(args[0], args[1], mode, ctx)


def evaluate_all(
    formulas: Mapping[str, Node],
    inputs: Mapping[str, Decimal | bool],
    display_order: Mapping[str, int] | None = None,
) -> dict[str, Decimal]:
    """Evaluate every formula in dependency order, feeding results into later ones.

    ``inputs`` holds the plain values (``C.<code>`` for components without a formula and
    every ``V.<name>``). Returns ``{component_code: amount}``. A formula that does not
    evaluate to a number raises :class:`FormulaEvaluationError`, naming the component.
    """
    scope: dict[str, Decimal | bool] = dict(inputs)
    results: dict[str, Decimal] = {}
    for code in evaluation_order(formulas, display_order):
        try:
            value = evaluate(formulas[code], scope)
            amount = _as_number(value)
        except FormulaEvaluationError as error:
            raise FormulaEvaluationError(f"{code}: {error}") from error
        results[code] = amount
        scope[f"C.{code}"] = amount
    return results


def validate_all(texts: Mapping[str, str]) -> dict[str, Node]:
    """Parse every formula and reject cycles; returns the ASTs keyed by component code."""
    parsed = {code: parse(text) for code, text in texts.items()}
    evaluation_order(parsed)
    return parsed


def reference_summary(node: Node) -> Sequence[str]:
    """Distinct references as ``C.X`` / ``V.Y`` strings, sorted, for the validate endpoint."""
    return sorted({f"{ref.kind}.{ref.name}" for ref in references(node)})
