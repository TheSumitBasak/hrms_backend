"""Salary request contracts that need no database."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.salary import (
    SalaryComponentCreateRequest,
    SalaryTemplateCreateRequest,
    TemplatePublishRequest,
)


@pytest.mark.parametrize("code", ["INR", "USD", "EUR"])
def test_iso_currency_is_accepted(code: str) -> None:
    request = SalaryTemplateCreateRequest(
        template_code="T1", template_name="Template", currency=code
    )
    assert request.currency == code


@pytest.mark.parametrize("code", ["XXX", "ABC", "inr", "IN", "INRR"])
def test_non_iso_currency_is_rejected(code: str) -> None:
    with pytest.raises(ValidationError):
        SalaryTemplateCreateRequest(template_code="T1", template_name="Template", currency=code)


def test_component_currency_uses_the_same_check() -> None:
    with pytest.raises(ValidationError):
        SalaryComponentCreateRequest(
            component_code="BASIC",
            component_name="Basic",
            component_category="EARNINGS",
            value_type="FIXED_AMOUNT",
            amount_period="MONTHLY",
            currency="XXX",
        )


def test_publish_body_distinguishes_omitted_from_empty_mappings() -> None:
    assert TemplatePublishRequest().mappings is None
    assert TemplatePublishRequest(mappings=[]).mappings == []
    assert TemplatePublishRequest(change_note="x").model_dump(
        exclude_unset=True, exclude={"mappings"}
    ) == {"change_note": "x"}


def test_percentage_component_needs_no_reference_label() -> None:
    request = SalaryComponentCreateRequest(
        component_code="HRA", component_name="HRA", value_type="PERCENTAGE"
    )
    assert request.component_type == "EARNING"
    assert request.display_name is None


def test_apply_basic_rounds_half_up_and_requires_basic() -> None:
    from decimal import Decimal

    from app.core.errors import ValidationFailed
    from app.services.salary import _apply_basic

    rows = [
        {
            "component_code": "BASIC",
            "value_type": "FIXED_AMOUNT",
            "value_amount": Decimal("33333.33"),
        },
        {"component_code": "HRA", "value_type": "PERCENTAGE", "value_percentage": Decimal("15")},
    ]
    _apply_basic(rows)
    assert rows[1]["computed_amount"] == Decimal("5000.00")
    with pytest.raises(ValidationFailed):
        _apply_basic([rows[1]])
    with pytest.raises(ValidationFailed):
        _apply_basic([{**rows[0], "value_amount": None}])
    with pytest.raises(ValidationFailed):
        _apply_basic([rows[0], {**rows[1], "value_percentage": None}])
