"""sunSale – electricity buy/sell optimiser for Home Assistant."""
from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from .contract.const import DOMAIN
from .orchestration.coordinator import SunSaleCoordinator
from .orchestration.debug_view import SunSaleDebugView

_PANEL_KEY = f"{DOMAIN}_panel_registered"
_STATIC_PATH = "/sun_sale"
_PANEL_URL = "sun-sale"
_WEBCOMPONENT = "sun-sale-panel"


def _js_hash(filename: str) -> str:
    """Return the first 8 hex characters of the MD5 hash of a www JS file for cache-busting.

    Args:
        filename: Filename relative to the integration's www/ directory.

    Returns:
        8-character hex string, or "0" when the file cannot be read.
    """
    path = Path(__file__).parent / "www" / filename
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()[:8]  # noqa: S324
    except OSError:
        return "0"

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

    if not hass.data.get(_PANEL_KEY):
        await hass.http.async_register_static_paths([
            StaticPathConfig(
                _STATIC_PATH,
                str(Path(__file__).parent / "www"),
                cache_headers=False,
            )
        ])
        await async_register_panel(
            hass,
            frontend_url_path=_PANEL_URL,
            webcomponent_name=_WEBCOMPONENT,
            sidebar_title="Sun Sale",
            sidebar_icon="mdi:solar-panel",
            module_url=f"{_STATIC_PATH}/sun-sale-panel.js?v={_js_hash('sun-sale-panel.js')}",
            require_admin=False,
            config={},
        )
        hass.data[_PANEL_KEY] = True

    async def handle_force_recalculate(call: ServiceCall) -> None:
        """Trigger an immediate refresh on all sunSale coordinator instances."""
        for coord in hass.data[DOMAIN].values():
            await coord.async_request_refresh()

    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_RECALCULATE):
        hass.services.async_register(DOMAIN, SERVICE_FORCE_RECALCULATE, handle_force_recalculate)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and remove its coordinator from hass.data.

    Args:
        hass: Home Assistant instance.
        entry: Config entry being unloaded.

    Returns:
        True when all platforms unloaded successfully.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options change.

    Args:
        hass: Home Assistant instance.
        entry: Config entry whose options were updated.
    """
    await hass.config_entries.async_reload(entry.entry_id)
