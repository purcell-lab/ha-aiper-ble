"""BlueZ metadata, not an emptied HA backend, verifies local diagnostic cleanup."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.aiper_ble_diagnostics import local_bleak

from .helpers import TARGET
from .test_polling import OP, PATH


@pytest.mark.parametrize(
    "case",
    [
        "disconnected",
        "connected",
        "missing",
        "wrong_identity",
        "bus_error",
        "cancelled",
    ],
)
async def test_independent_cleanup_read_is_fail_closed(case):
    props = {
        "Connected": False,
        "Address": TARGET.address,
        "Adapter": TARGET.adapter_path,
    }
    if case == "connected":
        props["Connected"] = True
    elif case == "missing":
        props.pop("Connected")
    elif case == "wrong_identity":
        props["Address"] = "OTHER"
    api = AsyncMock()
    api.device.return_value = props
    if case == "bus_error":
        api.device.side_effect = RuntimeError("PRIVATE_BUS_ERROR")

    @asynccontextmanager
    async def metadata_only(target):
        assert target == TARGET
        yield api

    async def managed(hass, target, report, query, *, pin_local):
        assert pin_local
        report.update(
            status="failed",
            cleanup="disconnected_confirmed",
            transport_diagnostics={"connect_calls_observed": 1},
        )
        if case == "cancelled":
            raise asyncio.CancelledError

    report = {}
    with (
        patch(f"{PATH}.bluetooth_transport.query_once", managed),
        patch.object(local_bleak, "open_bluez", metadata_only),
    ):
        if case == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await local_bleak.query_once(None, TARGET, report, OP)
        else:
            await local_bleak.query_once(None, TARGET, report, OP)
    if case in {"disconnected", "cancelled"}:
        assert report["status"] == "failed"
        assert report["cleanup"] == "disconnected_confirmed"
    else:
        assert report["status"] == "cleanup_requires_review"
        assert report["cleanup"] == "disconnect_unconfirmed"
    api.device.assert_awaited_once()
    api.connect.assert_not_called()
    api.disconnect.assert_not_called()
    assert "PRIVATE_BUS_ERROR" not in str(report)


async def test_preconnect_refusal_does_not_open_system_bus():
    with (
        patch(f"{PATH}.bluetooth_transport.query_once", AsyncMock()),
        patch.object(local_bleak, "open_bluez") as bluez,
    ):
        await local_bleak.query_once(None, TARGET, {}, OP)
        bluez.assert_not_called()
