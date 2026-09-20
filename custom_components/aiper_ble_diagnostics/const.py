"""Constants for the opt-in Aiper diagnostic integration."""

DOMAIN = "aiper_ble_diagnostics"
SIGNAL_RESULT = f"{DOMAIN}_result"
REDACT_KEYS = {
    "address",
    "name",
    "adapter_address",
    "Address",
    "Name",
    "target",
    "adapter",
    "sample_hex",
    "error",
    "protocol_response",
    "protocol_responses",
    "notification_samples",
}
