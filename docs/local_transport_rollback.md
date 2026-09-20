# Local BlueZ transport rollback

## Reason and scope

The HA-managed transport uses Bleak through HA's Bluetooth wrappers and
`bleak_retry_connector`, potentially choosing an ESPHome proxy. The original
transport calls BlueZ directly over the local system D-Bus, pinned to the saved
adapter. Switching between these paths changes both the software stack and
potentially the radio. A successful local test alone cannot identify which
layer of the HA/proxy path caused the failure.

Version 0.9.2 adds `use_local_adapter` to the integration options. When true,
polling and the four independent query actions use only the original local
BlueZ transport. No HA-managed connection, proxy fallback, automatic retry or
pairing is performed. Proxy-only entries cannot enable this option. Existing
configurations retain HA routing until the option is explicitly enabled.

The query catalog, sensor mapping, response CRC validation, all-or-nothing
publication, shared cooldown and unsafe-cleanup suspension remain unchanged.
Legacy manual query actions already use local BlueZ; their raw response is not
equivalent to a coordinator-verified sensor update.

## Controlled observations

Times below are Australia/Brisbane, 20 September 2026.

| Path | Start | Outcome |
| --- | --- | --- |
| HA/Bleak via nearby ESPHome proxy | 12:43:51 | Connect timeout after about 20 seconds; RSSI -58 dBm, no writes or notifications; disconnect confirmed |
| Original pinned local BlueZ | 12:49:35 | OpInfo completed at 12:49:40; preflight RSSI -76 dBm; one write, one notification, 89 response bytes; notifications stopped and disconnect confirmed |

Both used OpInfo, omitted the empty-data CRC on the request, and wrote with
response. Recurring polling was disabled. More than five minutes elapsed
after cleanup of the first attempt. No restart or robot command was inserted
between these tests.

The local response contained `res: 0`, `wifi_rssi: -127` and checksum `52546`.
The raw Wi-Fi value is not the Bluetooth signal strength and is not interpreted
as a useful live Wi-Fi reading. The legacy action labels its response CRC as
unverified; the coordinator independently checks CRC before publishing values.

These observations support reverting this installation to direct BlueZ. They
do not establish a general defect in Bleak or in ESPHome, and they do not yet
validate INFO/WARN or the full four-query cycle.
