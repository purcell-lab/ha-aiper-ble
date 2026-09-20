"""Explicit scalar DP allowlists; never turn arbitrary response keys into states."""

import re

OPINFO_FIELDS = {
    "bat": "Aiper BLE OpInfo battery raw",
    "status": "Aiper BLE OpInfo status raw",
    "link": "Aiper BLE OpInfo link raw",
}
MACHINE_FIELDS = {
    "cap": "battery capacity",
    "mode": "mode",
    "solar_status": "solar status",
    "status": "status",
    "temp": "temperature",
    "warn": "warning",
    "warn_code": "warning code",
    "in_water": "in water",
    "link": "link",
    "light": "light",
    "visual": "visual",
}

SENSOR_NAMES = {
    "temperature": "Aiper BLE temperature",
    "temperature_raw": "Aiper BLE S1 temperature raw",
    "solar_status_raw": "Aiper BLE solar status raw",
    "s1_timezone": "Aiper BLE S1 time zone",
    "wifi_rssi_raw": "Aiper BLE Wi-Fi RSSI raw",
    "wifi_name": "Aiper BLE Wi-Fi network",
    **{f"opinfo_{field}_raw": name for field, name in OPINFO_FIELDS.items()},
    **{
        f"opinfo_machine_{field}_raw": f"Aiper BLE OpInfo Machine {name} raw"
        for field, name in MACHINE_FIELDS.items()
    },
    "last_success": "Aiper BLE last successful poll",
}


def integer(value, *, bits=32):
    """App fields are signed Java Integer/Long; booleans are not numeric DPs."""
    return (
        value
        if type(value) is int and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)
        else None
    )


def network_name(value):
    """An SSID is text, at most 32 UTF-8 bytes; never stringify containers."""
    if not isinstance(value, str) or not value or not value.isprintable():
        return None
    try:
        return value if len(value.encode("utf-8")) <= 32 else None
    except UnicodeError:
        return None


def timezone(value):
    """Expose bounded time-zone metadata, not arbitrary response strings."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_+:/.-]{1,64}", value):
        return value
    return None


def opinfo_values(data):
    """Direct OpInfo fields and optional nested Machine candidates stay distinct.

    The nested Machine shape was an existing protocol candidate allowlist, not
    observed on the user's S1. Do not synthesize these values from another query,
    treat OpInfo.bat as a verified percentage, or conflate status/link fields.
    """
    machine = data.get("Machine")
    if not isinstance(machine, dict):
        machine = {}
    return {
        "wifi_rssi_raw": integer(data.get("wifi_rssi")),
        "wifi_name": network_name(data.get("wifi_name")),
        **{f"opinfo_{field}_raw": integer(data.get(field)) for field in OPINFO_FIELDS},
        **{
            f"opinfo_machine_{field}_raw": integer(
                machine.get(field), bits=64 if field == "warn_code" else 32
            )
            for field in MACHINE_FIELDS
        },
    }
