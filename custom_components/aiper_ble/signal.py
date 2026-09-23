"""Passive signal strength from the robot's advertisements; no radio activity.

Home Assistant already receives the robot's advertisements through every
adapter and proxy. This monitor only subscribes to that stream for one
address: it never scans, connects or writes, and it survives without the
Bluetooth manager (the sensor then simply stays unavailable).
"""

import logging
from datetime import datetime
from time import monotonic

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .transport_diagnostics import number, optional

LOGGER = logging.getLogger(__name__)
# Advertisements arrive several times a second; publish a changed value at
# most this often. Loss of the advertisement is published at once.
SIGNAL_PUBLISH_SECONDS = 10
SCANNER_TYPES = {"usb", "uart", "remote", "unknown"}


class SignalMonitor:
    """Latest RSSI of the robot's advertisement on Home Assistant's best route."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self.hass = hass
        self.address = address
        self.rssi: int | float | None = None
        self.seen_at: datetime | None = None
        self.source: str | None = None
        self.active = False
        self._published: int | float | None = None
        self._published_at: float | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []

    @callback
    def async_start(self) -> None:
        """Subscribe to advertisements and to loss of the advertisement."""
        try:
            self._unsubscribe.append(
                bluetooth.async_register_callback(
                    self.hass,
                    self._advertisement,
                    BluetoothCallbackMatcher(address=self.address, connectable=True),
                    BluetoothScanningMode.PASSIVE,
                )
            )
            self._unsubscribe.append(
                bluetooth.async_track_unavailable(
                    self.hass, self._unavailable, self.address, connectable=True
                )
            )
        except Exception:  # noqa: BLE001 - optional observation only
            LOGGER.debug("Passive signal monitoring unavailable")
            self.async_stop()
            return
        self.active = True

    @callback
    def async_stop(self) -> None:
        while self._unsubscribe:
            self._unsubscribe.pop()()
        self.active = False

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @property
    def scanner_type(self) -> str | None:
        """Kind of route currently carrying the advertisement, never its identity."""
        source = self.source
        if source is None:
            return None
        scanner = optional(lambda: bluetooth.async_scanner_by_source(self.hass, source))
        kind = optional(lambda: scanner.details.scanner_type.value)
        return kind if isinstance(kind, str) and kind in SCANNER_TYPES else None

    @callback
    def _advertisement(
        self, info: BluetoothServiceInfoBleak, _change: BluetoothChange
    ) -> None:
        rssi = number(info.rssi, -127, 20)
        if rssi is None:
            return
        self.rssi = rssi
        self.seen_at = dt_util.utcnow()
        self.source = info.source
        now = monotonic()
        if rssi == self._published:
            return
        if (
            self._published is None
            or self._published_at is None
            or now - self._published_at >= SIGNAL_PUBLISH_SECONDS
        ):
            self._publish(now)

    @callback
    def _unavailable(self, _info: BluetoothServiceInfoBleak) -> None:
        self.rssi = None
        self.source = None
        self._publish(monotonic())

    @callback
    def _publish(self, now: float) -> None:
        self._published = self.rssi
        self._published_at = now
        for update in list(self._listeners):
            update()
