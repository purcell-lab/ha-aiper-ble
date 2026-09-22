"""Allow the custom integration while keeping all Bluetooth operations fake."""

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import mock_component


@pytest.fixture(autouse=True)
def custom_integrations(hass, enable_custom_integrations):
    """Treat Bluetooth as loaded; never initialise real USB or radio hardware."""
    mock_component(hass, "bluetooth")
    with patch(
        "homeassistant.components.bluetooth.async_discovered_service_info",
        return_value=[],
    ):
        yield
