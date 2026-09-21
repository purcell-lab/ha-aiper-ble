# Working rules for this repository

Standing instructions from the repository owner. They apply to every
conversation, GitHub comment, commit message, document and log summary
produced for this project.

## Time

- Report every time in the owner's local time, `Australia/Brisbane`
  (AEST, UTC+10, no daylight saving). Label it `AEST`.
- Convert before reporting. Home Assistant history and `home-assistant.log`
  are already local; the host kernel journal and Supervisor journal are UTC.
- Never quote a raw UTC timestamp on its own. If a UTC value is needed for
  cross-checking, put the local time first and the UTC value in brackets.

## Robot and site facts

- The robot has no dock. "On charger" means it was plugged into the wall
  charger by hand. Do not describe it as docked or docking.
- Production polling uses the Home Assistant Bluetooth path through the
  Athom plug proxy. The hci0 USB adapter is still ranked by Home Assistant
  and can be selected for individual connections.

## Privacy

- Keep MAC addresses, serial numbers, hostnames, internal URLs and other
  private identifiers out of public issues, pull requests and commits.
- The Home Assistant MCP webhook URL is session-only. Never write it into
  the repository or GitHub.

## Bluetooth actions

- Do not trigger a poll, probe, proxy trace or any other BLE action without
  a fresh go-ahead from the owner for that action.
- Health checks and log reviews are read-only unless asked otherwise.
