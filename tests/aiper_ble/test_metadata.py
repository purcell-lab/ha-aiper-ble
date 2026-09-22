"""Validate packaged metadata and the no-auto-discovery manifest."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "custom_components" / "aiper_ble"


def test_manifest_discovery_matchers_icons_and_no_external_requirements():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == "aiper_ble"
    assert manifest["dependencies"] == ["bluetooth"]
    assert manifest["requirements"] == []
    assert manifest["config_flow"]
    # Passive discovery only: HA offers robots its shared scanners already see.
    assert [m["local_name"] for m in manifest["bluetooth"]] == [
        "Aiper-Surfer S1-*",
        "Aiper_Surfer S1_*",
    ]
    assert all(m["connectable"] for m in manifest["bluetooth"])
    icons = json.loads((COMPONENT / "icons.json").read_text())
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    assert set(icons["services"]) == set(services)


def test_service_descriptions_and_translation_match():
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    assert english == strings
    assert (
        set(services)
        == {
            "start_cleaning",
            "stop_cleaning",
            "query_s1_info",
            "query_opinfo",
            "query_opinfo_local_bleak",
            "query_opinfo_proxy_trace",
            "query_info",
            "query_warn",
            "poll_now",
            "preflight",
            "discover",
            "read_once",
            "protocol_preview",
            "query_once",
            "listen_once",
        }
        == set(strings["services"])
    )
    assert services["discover"]["fields"]["confirm_app_closed"]["default"] is False
    for confirmation in ("confirm_app_closed", "confirm_read_only"):
        assert services["read_once"]["fields"][confirmation]["default"] is False
    for confirmation in (
        "confirm_app_closed",
        "confirm_query_write",
        "confirm_notifications",
    ):
        assert services["query_once"]["fields"][confirmation]["default"] is False
    for name, service in services.items():
        assert set(service["fields"]) == set(strings["services"][name]["fields"])
