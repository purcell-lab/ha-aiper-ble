"""The native trace guard brackets the real shared query lifecycle."""

from unittest.mock import Mock, patch

import pytest

from custom_components.aiper_ble_diagnostics.bluetooth_transport import query_once
from custom_components.aiper_ble_diagnostics.protocol import ProtocolError

from .helpers import TARGET
from .test_bluetooth import radio as radio
from .test_polling import OP, PATH, response
from .test_protocol import frame


@pytest.mark.parametrize("fail_at", [0, 1, 2, 3])
async def test_guard_runs_before_connect_and_both_prewrite_stages(hass, radio, fail_at):
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise ProtocolError("proxy_trace_session_changed")

    radio.mutations.append(
        lambda client: setattr(client, "reply", frame(response("OpInfo")))
    )
    with (
        patch(
            f"{PATH}.proxy_trace.pinned_client_class",
            side_effect=lambda base, *args: base,
        ),
        patch(f"{PATH}.proxy_trace.assert_proxy_backend") as assertion,
    ):
        report = {}
        await query_once(
            hass, TARGET, report, OP, proxy_source="test_proxy", proxy_guard=guard
        )
    if fail_at:
        assert report["error_code"] == "proxy_trace_session_changed"
        assert report["write_attempts"] == 0
        assert calls == fail_at
    else:
        assert report["status"] == "query_complete"
        assert calls == 3
        assert assertion.call_count == 2
    if fail_at == 1:
        assert not radio.clients
    else:
        assert len(radio.clients) == 1
        assert radio.clients[0].calls.count("connect") == 1
        assert radio.clients[0].calls.count("disconnect") == 1


async def test_pin_cannot_run_without_logging_guard(hass, radio):
    with patch(
        f"{PATH}.proxy_trace.pinned_client_class", side_effect=lambda base, *args: base
    ):
        report = {}
        await query_once(hass, TARGET, report, OP, proxy_source="test_proxy")
    assert report["error_code"] == "proxy_trace_guard_required"
    assert not radio.clients


async def test_wrong_actual_backend_never_writes(hass, radio):
    with (
        patch(
            f"{PATH}.proxy_trace.pinned_client_class",
            side_effect=lambda base, *args: base,
        ),
        patch(
            f"{PATH}.proxy_trace.assert_proxy_backend",
            side_effect=ProtocolError("proxy_trace_backend_mismatch"),
        ),
    ):
        report = {}
        await query_once(
            hass, TARGET, report, OP, proxy_source="test_proxy", proxy_guard=Mock()
        )
    assert report["write_attempts"] == 0
    assert report["cleanup"] == "disconnected_confirmed"
