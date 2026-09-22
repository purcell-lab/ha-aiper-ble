"""Repair flow: a suspended entry is fixed by reloading it after review."""

from typing import Any

from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


class SuspendedPollingRepairFlow(ConfirmRepairFlow):
    """Reload the entry once the owner has reviewed the diagnostics."""

    def __init__(self, entry_id: str | None) -> None:
        self.entry_id = entry_id

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        if user_input is not None and self.entry_id is not None:
            await self.hass.config_entries.async_reload(self.entry_id)
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    stored = (data or {}) or ((issue.data if issue else None) or {})
    entry_id = stored.get("entry_id")
    return SuspendedPollingRepairFlow(entry_id if isinstance(entry_id, str) else None)
