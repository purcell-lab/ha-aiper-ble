"""Pinned local BlueZ queries, without Bleak or proxy fallback."""

from typing import Any

from homeassistant.core import HomeAssistant

from .probe import Target, open_bluez, probe
from .protocol import Control, Query
from .transport_diagnostics import TransportDiagnostics


async def query_once(
    hass: HomeAssistant,
    target: Target,
    report: dict[str, Any],
    query: Query | Control,
) -> None:
    """Reuse the original guarded transport; verification stays in coordinator."""
    report.update(
        transport="local_bluez",
        phase="preflight",
        write_attempts=0,
        notification_count=0,
        received_bytes=0,
    )
    diagnostics = TransportDiagnostics(report, "local_bluez")
    diagnostics.phase("local_probe_including_cleanup")
    # Cached advertisement age is unavailable over BlueZ's device interface, so
    # read HA's existing per-scanner cache. This is a passive read of already
    # collected metadata: it starts no scan, opens no connection and never
    # participates in transport selection, which stays pinned to the saved radio.
    diagnostics.snapshot(hass, target.address, "local_preflight")
    if target.adapter_path is None or target.adapter_address is None:
        report.update(
            status="failed",
            error_code="local_adapter_required",
            failure_stage="preflight",
            cleanup="not_connected",
        )
        diagnostics.finish()
        return
    try:
        async with open_bluez(target, query=query) as api:
            await probe(api, target, report, connect=True, query=query)
    finally:
        report["phase"] = report.get("stage", "preflight")
        # The legacy diagnostic contains exception text and identity metadata.
        # The coordinator publishes only its existing fixed-field allowlist.
        report.pop("error", None)
        diagnostics.finish()
