"""sunSale – electricity buy/sell and EV charging optimiser for Home Assistant."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import SunSaleCoordinator
from .debug_view import SunSaleDebugView

_DEBUG_VIEW_KEY = f"{DOMAIN}_debug_view_registered"

PLATFORMS = ["sensor", "switch"]

SERVICE_FORCE_RECALCULATE = "force_recalculate"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up sunSale from a config entry."""
    coordinator = SunSaleCoordinator(hass, entry)
    await coordinator.async_setup()

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        raise ConfigEntryNotReady from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    if not hass.data.get(_DEBUG_VIEW_KEY):
        hass.http.register_view(SunSaleDebugView())
        hass.data[_DEBUG_VIEW_KEY] = True

    async def handle_force_recalculate(call: ServiceCall) -> None:
        for coord in hass.data[DOMAIN].values():
            await coord.async_request_refresh()

    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_RECALCULATE):
        hass.services.async_register(DOMAIN, SERVICE_FORCE_RECALCULATE, handle_force_recalculate)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
