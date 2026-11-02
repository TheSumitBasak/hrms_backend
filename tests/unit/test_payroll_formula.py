"""§5.4 formula language: PSTR-10 to PSTR-15."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.payroll.formula import (
    FormulaCycleError,
    FormulaEvaluationError,
    FormulaSyntaxError,
    ast_hash,
    component_codes,
    evaluate,
    evaluate_all,
    evaluation_order,
    parse,
    validate_all,
)

D = Decimal


def run(text: str, **values: Decimal | bool) -> Decimal | bool:
    return evaluate(parse(text), {k.replace("__", "."): v for k, v in values.items()})


class TestParsing:
    def test_literals_are_decimal_not_float(self) -> None:
        assert run("0.1 + 0.2") == D("0.3")  # would be 0.30000000000000004 as floats

    def test_precedence_and_parentheses(self) -> None:
        assert run("2 + 3 * 4") == D(14)
        assert run("(2 + 3) * 4") == D(20)
        assert run("-2 * 3") == D(-6)

    def test_references_and_codes(self) -> None:
        node = parse("C.BASIC * 0.4 + V.PF_CEILING")
        assert component_codes(node) == {"BASIC"}

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "1 +",
            "C.",
            "FOO(1)",
            "V.NOPE",
            "1 2",
            "ROUND(1)",
            "IF(1, 2)",
            "1 $ 2",
            "__import__('os')",
            "C.A.B",
        ],
    )
    def test_invalid_syntax_is_rejected(self, text: str) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse(text)

    def test_limits(self) -> None:
        with pytest.raises(FormulaSyntaxError):
            parse("1" + " + 1" * 300)  # > 1000 chars
        with pytest.raises(FormulaSyntaxError):
            parse(" + ".join(["1"] * 40))  # AST depth 40 > 32
        with pytest.raises(FormulaSyntaxError):
            parse(
                " + ".join(f"C.X{i}" if False else f"C.A{chr(65 + i % 26)}{i}" for i in range(65))
            )

    def test_hash_ignores_whitespace_and_number_formatting(self) -> None:
        assert ast_hash(parse("C.A*0.4")) == ast_hash(parse(" C.A  *  0.40 "))
        assert ast_hash(parse("C.A*0.4")) != ast_hash(parse("C.A*0.5"))


class TestEvaluation:
    def test_functions(self) -> None:
        assert run("MIN(3, 1, 2)") == D(1)
        assert run("MAX(3, 1, 2)") == D(3)
        assert run("CLAMP(15, 0, 10)") == D(10)
        assert run("ROUND(2.5, 0)") == D(3)  # half up, not banker's
        assert run("ROUND(-2.5, 0)") == D(-3)
        assert run("ROUND_UP(2.01, 0)") == D(3)
        assert run("ROUND_DOWN(2.99, 0)") == D(2)
        assert run("ROUND(1234.5678, 2)") == D("1234.57")

    def test_no_rounding_unless_asked(self) -> None:
        assert run("10 / 3") == D("3.333333333333333333333333333333333")

    def test_booleans_and_comparisons(self) -> None:
        assert run("1 < 2 AND NOT (3 < 2)") is True
        assert run("1 == 1.0") is True
        assert run("IF(V.IS_METRO_RENT, 0.5, 0.4)", V__IS_METRO_RENT=True) == D("0.5")

    def test_if_is_lazy(self) -> None:
        assert run("IF(1 > 0, 5, 1 / 0)") == D(5)

    def test_and_or_short_circuit(self) -> None:
        assert run("1 > 2 AND 1 / 0 > 0") is False
        assert run("1 < 2 OR 1 / 0 > 0") is True

    @pytest.mark.parametrize(
        "text",
        [
            "1 / 0",
            "C.MISSING",
            "1 + (1 < 2)",
            "IF(1, 2, 3)",
            "NOT 1",
            "CLAMP(1, 5, 2)",
            "ROUND(1, 0.5)",
        ],
    )
    def test_evaluation_errors(self, text: str) -> None:
        with pytest.raises(FormulaEvaluationError):
            run(text)

    def test_annual_to_monthly_has_no_intermediate_rounding(self) -> None:
        assert run("V.CTC_ANNUAL / 12", V__CTC_ANNUAL=D("100000")) == D(
            "8333.333333333333333333333333333333"
        )


class TestOrdering:
    def test_dependencies_come_first_and_ties_follow_display_order_then_code(self) -> None:
        formulas = {
            "SPECIAL": parse("V.CTC_MONTHLY - C.BASIC - C.HRA"),
            "HRA": parse("C.BASIC * 0.4"),
            "BASIC": parse("V.CTC_MONTHLY * 0.5"),
            "CONV": parse("1600"),
        }
        order = evaluation_order(formulas, {"CONV": 0, "BASIC": 1, "HRA": 2, "SPECIAL": 3})
        assert order == ["CONV", "BASIC", "HRA", "SPECIAL"]
        assert evaluation_order(formulas) == ["BASIC", "CONV", "HRA", "SPECIAL"]

    def test_cycle_is_rejected(self) -> None:
        with pytest.raises(FormulaCycleError):
            validate_all({"A": "C.B + 1", "B": "C.A + 1"})
        with pytest.raises(FormulaCycleError):
            validate_all({"A": "C.A + 1"})

    def test_plain_inputs_do_not_create_dependencies(self) -> None:
        validate_all({"HRA": "C.BASIC * 0.4"})  # BASIC has no formula: it is an input

    def test_evaluate_all_chains_results(self) -> None:
        formulas = validate_all(
            {"HRA": "C.BASIC * 0.4", "SPECIAL": "V.CTC_MONTHLY - C.BASIC - C.HRA"}
        )
        out = evaluate_all(formulas, {"C.BASIC": D("40000"), "V.CTC_MONTHLY": D("100000")})
        assert out == {"HRA": D("16000.0"), "SPECIAL": D("44000.0")}

    def test_evaluate_all_names_the_failing_component(self) -> None:
        formulas = validate_all({"HRA": "C.BASIC / C.ZERO"})
        with pytest.raises(FormulaEvaluationError, match="HRA"):
            evaluate_all(formulas, {"C.BASIC": D(1), "C.ZERO": D(0)})
