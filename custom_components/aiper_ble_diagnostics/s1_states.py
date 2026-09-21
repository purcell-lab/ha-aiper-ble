"""S1 INFO-derived states, independently traced from Android 3.6.1 build 82."""

INFO_STATES = (
    "standby",
    "working",
    "charging",
    "fully_charged",
    "updating",
    "sunward",
    "unknown_code",
)


def info_state(values):
    """Decode INFO, not the app's connectivity/warning/OTA overlay enum.

    Unknown status values deliberately do not inherit the app's standby fallback.
    Mode 8 suppresses WORKING; mode 9 selects SUNWARD after charging/OTA checks.
    """
    status = values.get("info_status_raw")
    mode = values.get("info_mode_raw")
    battery = values.get("battery")
    if type(status) is not int or status not in {0, 1, 2, 3, 4}:
        return "unknown_code"
    if status == 4:
        return "updating"
    if status == 3 or (status == 2 and battery == 100):
        return "fully_charged"
    if status == 2:
        return "charging"
    if mode == 9:
        return "sunward"
    if status == 1 and mode != 8:
        return "working"
    return "standby"
