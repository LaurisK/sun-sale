"""Master automation switch for sunSale."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
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


class AutomationSwitch(CoordinatorEntity, SwitchEntity):
    """When off the coordinator still computes schedules but does not send commands."""

    _attr_name = "sunSale Automation"
    _attr_icon = "mdi:auto-fix"

    def __init__(self, coordinator: SunSaleCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._entry = entry

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
