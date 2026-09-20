"""Pinned local BlueZ queries, without Bleak or proxy fallback."""

from .probe import open_bluez, probe


async def query_once(hass, target, report, query):
    """Reuse the original guarded transport; verification stays in coordinator."""
    report.update(
        transport="local_bluez",
        phase="preflight",
        write_attempts=0,
        notification_count=0,
        received_bytes=0,
    )
    if target.adapter_path is None or target.adapter_address is None:
        report.update(
            status="failed",
            error_code="local_adapter_required",
            failure_stage="preflight",
            cleanup="not_connected",
        )
        return
    try:
        async with open_bluez(target, query=query) as api:
            await probe(api, target, report, connect=True, query=query)
    finally:
        report["phase"] = report.get("stage", "preflight")
        # The legacy diagnostic contains exception text and identity metadata.
        # The coordinator publishes only its existing fixed-field allowlist.
        report.pop("error", None)
