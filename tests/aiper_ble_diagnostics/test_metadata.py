"""Validate packaged metadata and the no-auto-discovery manifest."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "custom_components" / "aiper_ble_diagnostics"


def test_manifest_has_no_auto_discovery_or_external_requirements():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == "aiper_ble_diagnostics"
    assert manifest["dependencies"] == ["bluetooth"]
    assert manifest["requirements"] == []
    assert manifest["config_flow"]
    assert "bluetooth" not in manifest


def test_service_descriptions_and_translation_match():
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    assert english == strings
    assert (
        set(services)
        == {
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
