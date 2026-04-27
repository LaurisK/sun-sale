"""Master automation switch for sunSale."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SunSaleCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SunSaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AutomationSwitch(coordinator, entry)])


class AutomationSwitch(CoordinatorEntity, RestoreEntity, SwitchEntity):
    """When off the coordinator still computes schedules but does not send commands.

    State is persisted via RestoreEntity so it survives HA restarts.
    Defaults to OFF on first install.
    """

    _attr_name = "sunSale Automation"
    _attr_icon = "mdi:auto-fix"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self.coordinator.automation_enabled = last.state == "on"
        # If last is None (first install) the coordinator default of False stays.

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "sunSale",
            "manufacturer": "sunSale",
        }

    @property
    def is_on(self) -> bool:
        return self.coordinator.automation_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.automation_enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.automation_enabled = False
        self.async_write_ha_state()
