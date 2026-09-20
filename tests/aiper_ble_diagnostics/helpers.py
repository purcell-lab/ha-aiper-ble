"""Fake local BlueZ transport: no hardware or network."""

import copy

from custom_components.aiper_ble_diagnostics import probe as p

TARGET = p.Target(
    "AA:BB:CC:DD:EE:01",
    "Aiper-Surfer S1-TEST",
    "/org/bluez/hci0",
    "AA:BB:CC:DD:EE:02",
)


def objects():
    service = TARGET.device_path + "/service001"
    char = service + "/char002"
    return {
        TARGET.adapter_path: {
            "org.bluez.Adapter1": {"Address": TARGET.adapter_address, "Powered": True}
        },
        TARGET.device_path: {
            p.DEVICE_IF: {
                "Address": TARGET.address,
                "Name": TARGET.name,
                "Adapter": TARGET.adapter_path,
                "Connected": False,
                "Paired": False,
                "Trusted": False,
                "ServicesResolved": False,
            }
        },
        service: {
            p.SERVICE_IF: {
                "Device": TARGET.device_path,
                "UUID": p.EXPECTED_SERVICE,
                "Primary": True,
            }
        },
        char: {
            p.CHAR_IF: {
                "Service": service,
                "UUID": p.EXPECTED_CHARACTERISTIC,
                "Flags": ["read", "write", "notify"],
                "Value": [99],
            }
        },
        char + "/desc003": {
            p.DESC_IF: {"Characteristic": char, "UUID": "2902", "Value": [0, 0]}
        },
        "/unrelated": {p.SERVICE_IF: {"Device": "/other", "UUID": "private"}},
    }


class Fake:
    def __init__(self):
        self.data = objects()
        self.calls = []

    async def objects(self):
        self.calls.append("metadata")
        return copy.deepcopy(self.data)

    async def device(self):
        self.calls.append("device_metadata")
        return copy.deepcopy(self.data[TARGET.device_path][p.DEVICE_IF])

    async def connect(self):
        self.calls.append("connect")
        self.data[TARGET.device_path][p.DEVICE_IF].update(
            Connected=True, ServicesResolved=True
        )

    async def disconnect(self):
        self.calls.append("disconnect")
        self.data[TARGET.device_path][p.DEVICE_IF]["Connected"] = False

    async def read_sample(self):
        p.readable_path(self.data, TARGET)
        self.calls.append("read")
        return bytes.fromhex("012345ab")
