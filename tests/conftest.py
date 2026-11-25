"""Fixtures for the config-flow tests.

    pip install -r requirements_test.txt
    pytest tests
"""
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/smartfilterpro in every test."""
    yield
