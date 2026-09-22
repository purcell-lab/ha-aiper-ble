"""Gold rules: Bluetooth discovery, reconfigure flow, repair issue on suspension."""

import time
from dataclasses import asdict

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiper_ble.const import DOMAIN
from custom_components.aiper_ble.repairs import async_create_fix_flow

from .helpers import TARGET
from .test_integration import fake_bluez as fake_bluez  # noqa: F401 - fixture
from .test_polling import setup
from .test_polling import transport as transport  # noqa: F401 - fixture


def service_info(name, address=TARGET.address):
    advertisement = AdvertisementData(
        local_name=name,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=-127,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="test_proxy",
        device=BLEDevice(address, name, {}),
        advertisement=advertisement,
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


async def test_bluetooth_discovery_confirms_then_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info(TARGET.name),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"name": TARGET.name}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["address"] == TARGET.address
    assert result["data"]["name"] == TARGET.name
    assert result["result"].unique_id == TARGET.address


async def test_discovery_of_configured_robot_aborts_without_touching_it(hass):
    """Scanners spell the name differently; rewriting it would reload the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=asdict(TARGET), unique_id=TARGET.address
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info("Aiper_Surfer S1_S1Y00000000"),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == asdict(TARGET)


async def test_reconfigure_reselects_robot_and_reloads(hass, fake_bluez):
    entry = MockConfigEntry(
        domain=DOMAIN, data=asdict(TARGET), unique_id=TARGET.address
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    # The local BlueZ fallback keys candidates by D-Bus path.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": TARGET.device_path}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data == asdict(TARGET)
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert fake_bluez.calls == ["metadata", "metadata"]  # form, then submit


async def test_suspension_raises_repair_issue_fixed_by_reload(hass, transport):
    entry = await setup(hass)
    coordinator = entry.runtime_data.coordinator
    registry = ir.async_get(hass)
    issue_id = f"polling_suspended_{entry.entry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    coordinator.error_code = "cleanup_requires_review"
    coordinator.suspended = True
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"error_code": "cleanup_requires_review"}
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    flow.handler = DOMAIN
    flow.issue_id = issue_id
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM
    result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinator is not coordinator
    assert not entry.runtime_data.coordinator.suspended
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert len(transport[0]) == 8
