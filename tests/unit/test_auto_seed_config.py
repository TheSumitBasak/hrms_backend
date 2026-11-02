"""AUTO_SEED creates known-password accounts, so it must never be accepted in production."""

import pytest
from pydantic import ValidationError

from tests.conftest import build_test_settings


def test_auto_seed_defaults_off() -> None:
    assert build_test_settings().auto_seed is False


def test_auto_seed_is_allowed_in_development() -> None:
    assert build_test_settings(auto_seed=True).auto_seed is True


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_auto_seed_is_refused_in_production_like_environments(environment: str) -> None:
    with pytest.raises(ValidationError, match="auto_seed must be false"):
        build_test_settings(auto_seed=True, environment=environment)
