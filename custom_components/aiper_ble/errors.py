"""User-facing errors with translation keys; English stays as the fallback text."""

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN

# One place for every message. strings.json "exceptions" mirrors this table.
MESSAGES = {
    "enable_polling_first": "Enable authorised polling first.",
    "polling_suspended": "Polling suspended; review diagnostics first.",
    "already_running": "A poll or probe is already running.",
    "select_fixed_query": "Select a fixed diagnostic query.",
    "same_radio_opinfo_only": "Same-radio diagnostics allow only OpInfo.",
    "proxy_trace_opinfo_only": "Proxy trace allows only one OpInfo query.",
    "disable_polling_for_bisection": "Disable recurring polling before bisection.",
    "closing_or_suspended": "Integration closing or polling suspended.",
    "cooldown": "Polling cooldown: retry in {seconds} seconds.",
    "select_control": "Select start_cleaning or stop_cleaning.",
    "control_closing_or_suspended": (
        "Integration closing or suspended; review diagnostics."
    ),
    "control_busy": "A poll, probe or control is already running.",
    "control_unconfirmed": (
        "Cleaning control {status}; inspect diagnostics and robot. "
        "No automatic retry. BLE stop is not an emergency stop."
    ),
    "vacuum_controls_disabled": (
        "Vacuum controls are disabled. Enable them in the integration's options "
        "once the robot is in water, unplugged and nobody is in the pool, and "
        "keep the app closed."
    ),
    "select_loaded_entry": "Select a loaded Aiper BLE entry.",
    "unloading": "Integration is unloading.",
    "probe_running": "A probe is already running.",
    "control_confirmations": (
        "Close other BLE clients and confirm robot in water, unplugged, "
        "no people in pool, no update in progress and safe to operate."
    ),
    "coordinator_unavailable": "Polling coordinator is unavailable.",
    "isolated_confirmations": (
        "Confirm idle BLE clients, query write and notifications."
    ),
    "proxy_logging_confirmation": "Authorise bounded native proxy logging.",
    "isolated_query_failed": "Isolated query {status}. See integration diagnostics.",
    "confirm_app_closed": (
        "Close the Aiper app and other BLE clients, then confirm_app_closed: true."
    ),
    "poll_failed": (
        "Aiper BLE poll {status}: {error_code}. See integration diagnostics; "
        "do not bypass the cooldown."
    ),
    "local_adapter_required": (
        "This legacy diagnostic action requires a local adapter entry. "
        "HA-managed polling supports this proxy-only entry."
    ),
    "confirm_read_only": (
        "Authorise one raw characteristic read with confirm_read_only: true."
    ),
    "confirm_notifications": (
        "Authorise notification subscription with confirm_notifications."
    ),
    "confirm_query_write": (
        "Authorise notification subscription and one selected status command "
        "with confirm_notifications and confirm_query_write."
    ),
    "probe_stopped": "Probe stopped ({status}). Download integration diagnostics.",
}


def _kwargs(key, placeholders):
    return {
        "translation_domain": DOMAIN,
        "translation_key": key,
        "translation_placeholders": {k: str(v) for k, v in placeholders.items()},
    }


def validation(key, **placeholders):
    """A caller mistake: wrong input, missing confirmation, busy or gated state."""
    return ServiceValidationError(
        MESSAGES[key].format(**placeholders), **_kwargs(key, placeholders)
    )


def failure(key, **placeholders):
    """An operation that ran and did not succeed."""
    return HomeAssistantError(
        MESSAGES[key].format(**placeholders), **_kwargs(key, placeholders)
    )
