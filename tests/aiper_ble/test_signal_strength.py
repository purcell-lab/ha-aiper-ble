"""Signal strength: passive, from the advertisement stream, throttled."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.aiper_ble import signal as signal_module

from .helpers import TARGET
from .test_polling import OPTIONS, setup
from .test_polling import transport as transport  # noqa: F401 - fixture

ENTITY = "sensor.aiper_ble_signal_strength"


class Radio:
    """Capture the two Bluetooth subscriptions; no manager is needed."""

    def __init__(self):
        self.advertisement = None
        self.unavailable = None
        self.matcher = None
        self.unsubscribed = 0
        self.cached = None

    def register(self, hass, callback, matcher, mode):
        self.advertisement = callback
        self.matcher = matcher
        return self._unsub

    def track(self, hass, callback, address, connectable=True):
        self.unavailable = callback
        assert address == TARGET.address and connectable
        return self._unsub

    def _unsub(self):
        self.unsubscribed += 1

    def last_service_info(self, hass, address, connectable=True):
        assert address == TARGET.address
        return self.cached

    def cache(self, rssi, age=0.0, source="proxy-1"):
        self.cached = SimpleNamespace(
            address=TARGET.address,
            rssi=rssi,
            source=source,
            time=monotonic_time_coarse() - age,
        )

    def advertise(self, rssi, source="proxy-1"):
        self.advertisement(
            SimpleNamespace(address=TARGET.address, rssi=rssi, source=source), None
        )


async def start(hass, options=None):
    radio = Radio()
    with (
        patch.object(
            signal_module.bluetooth, "async_register_callback", radio.register
        ),
        patch.object(signal_module.bluetooth, "async_track_unavailable", radio.track),
    ):
        entry = await setup(hass, options)
    return entry, radio


def sampling(radio):
    return patch.object(
        signal_module.bluetooth, "async_last_service_info", radio.last_service_info
    )


async def tick(hass, seconds=11):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_live_value_from_advertisements_without_any_connection(hass, transport):
    entry, radio = await start(hass)
    assert radio.matcher["address"] == TARGET.address and radio.matcher["connectable"]
    assert hass.states.get(ENTITY).state == "unavailable"  # nothing received yet
    polls = len(transport[0])
    radio.advertise(-71)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "-71"
    assert state.attributes["unit_of_measurement"] == "dBm"
    assert state.attributes["device_class"] == "signal_strength"
    assert state.attributes["source"] == "passive_advertisement_on_best_route"
    assert state.attributes["last_seen"] is not None
    assert len(transport[0]) == polls  # no BLE activity
    assert entry.runtime_data.coordinator.live_signal == -71


async def test_changes_are_throttled_and_loss_is_immediate(hass, transport):
    entry, radio = await start(hass)
    with patch.object(signal_module, "monotonic", return_value=1000.0):
        radio.advertise(-71)
        radio.advertise(-75)  # changed within the publish window: held back
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "-71"
    assert entry.runtime_data.signal.rssi == -75  # tracked regardless
    with patch.object(signal_module, "monotonic", return_value=1011.0):
        radio.advertise(-77)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "-77"
    radio.unavailable(SimpleNamespace(address=TARGET.address))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unavailable"
    assert entry.runtime_data.coordinator.live_signal is None


async def test_sampling_picks_up_signal_only_changes_and_loss(hass, transport):
    """HA's callback skips RSSI-only changes; the cached advertisement has them."""
    entry, radio = await start(hass)
    with sampling(radio):
        radio.cache(-72, age=1.5)
        await tick(hass)
        state = hass.states.get(ENTITY)
        assert state.state == "-72"
        seen = dt_util.parse_datetime(state.attributes["last_seen"])
        assert 0 <= (dt_util.utcnow() - seen).total_seconds() < 5
        radio.cache(-97)
        await tick(hass)
        assert hass.states.get(ENTITY).state == "-97"
        assert entry.runtime_data.coordinator.live_signal == -97
        radio.cached = None  # HA no longer holds an advertisement
        await tick(hass)
        assert hass.states.get(ENTITY).state == "unavailable"
        assert len(transport[0]) == 4  # setup's cycle only; sampling is passive


async def test_invalid_rssi_is_ignored(hass, transport):
    _, radio = await start(hass)
    radio.advertise("strong")
    radio.advertise(5.5e9)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unavailable"


async def test_fast_interval_follows_the_live_signal(hass, transport):
    entry, radio = await start(hass, {**OPTIONS, "poll_interval": 60})
    coordinator = entry.runtime_data.coordinator
    assert coordinator.effective_interval == 300  # nothing received yet
    radio.advertise(-85)
    assert coordinator.effective_interval == 60
    radio.advertise(-93)
    assert coordinator.effective_interval == 300
    radio.unavailable(SimpleNamespace(address=TARGET.address))
    assert coordinator.effective_interval == 300


async def test_unload_unsubscribes_and_monitor_failure_is_tolerated(hass, transport):
    entry, radio = await start(hass)
    monitor = entry.runtime_data.signal
    assert monitor.active
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert radio.unsubscribed == 2  # the sampling timer is HA's own subscription
    assert not monitor.active
    with patch.object(
        signal_module.bluetooth,
        "async_register_callback",
        side_effect=RuntimeError("no manager"),
    ):
        entry = await setup(hass)
    assert not entry.runtime_data.signal.active
    assert hass.states.get(ENTITY).state == "unavailable"
    assert entry.runtime_data.coordinator.status == "ok"


async def test_unavailable_when_polling_disabled(hass, transport):
    _, radio = await start(hass, {})
    radio.advertise(-60)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unavailable"
