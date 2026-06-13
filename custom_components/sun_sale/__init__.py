"""sunSale – electricity buy/sell optimiser for Home Assistant."""
from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import async_remove_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .contract.const import DOMAIN
from .orchestration.coordinator import SunSaleCoordinator
from .orchestration.debug_view import SunSaleDebugView

# Stores the JS hash the panel is currently registered with; a value mismatch
# on subsequent setups means the panel.js changed and we must re-register so
# the frontend ``?v=<hash>`` query bypasses the browser's cached ES module.
_PANEL_KEY = f"{DOMAIN}_panel_registered_hash"
_STATIC_REGISTERED_KEY = f"{DOMAIN}_static_registered"
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

PLATFORMS = ["sensor", "switch", "number", "select"]

SERVICE_FORCE_RECALCULATE = "force_recalculate"
SERVICE_FORCE_VERIFY_INVERTER_MODE = "force_verify_inverter_mode"


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

    if not hass.is_running:
        @callback
        def _on_ha_started(_event) -> None:
            """Refresh once HA finishes starting so solar forecast entity has its watts attribute."""
            hass.async_create_task(coordinator.async_request_refresh())

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)
        )

    if not hass.data.get(_DEBUG_VIEW_KEY):
        hass.http.register_view(SunSaleDebugView())
        hass.data[_DEBUG_VIEW_KEY] = True

    if not hass.data.get(_STATIC_REGISTERED_KEY):
        await hass.http.async_register_static_paths([
            StaticPathConfig(
                _STATIC_PATH,
                str(Path(__file__).parent / "www"),
                cache_headers=False,
            )
        ])
        hass.data[_STATIC_REGISTERED_KEY] = True

    # Re-register the panel whenever the JS hash changes so an integration
    # reload (without a full HA restart) busts the browser's ES-module cache
    # via the ?v=<hash> query string in the module URL.
    current_hash = _js_hash("sun-sale-panel.js")
    if hass.data.get(_PANEL_KEY) != current_hash:
        if hass.data.get(_PANEL_KEY) is not None:
            # Stale registration from a previous setup — clear it so the
            # new module_url takes effect. async_remove_panel raises if the
            # panel was never registered; we already checked the key.
            try:
                async_remove_panel(hass, _PANEL_URL)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
        await async_register_panel(
            hass,
            frontend_url_path=_PANEL_URL,
            webcomponent_name=_WEBCOMPONENT,
            sidebar_title="Sun Sale",
            sidebar_icon="mdi:solar-panel",
            module_url=f"{_STATIC_PATH}/sun-sale-panel.js?v={current_hash}",
            require_admin=False,
            config={},
        )
        hass.data[_PANEL_KEY] = current_hash

    async def handle_force_recalculate(call: ServiceCall) -> None:
        """Trigger an immediate refresh on all sunSale coordinator instances."""
        for coord in hass.data[DOMAIN].values():
            await coord.async_request_refresh()

    async def handle_force_verify_inverter_mode(call: ServiceCall) -> None:
        """Run an inverter-mode verify cycle now (skip the +30 s wait).

        Useful for confirming engagement after a manual mode change without
        waiting on the scheduled verify-tick. Fans out to every configured
        sunSale instance so a multi-inverter setup verifies them all.
        """
        for coord in hass.data[DOMAIN].values():
            await coord.force_verify_inverter_mode()

    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_RECALCULATE):
        hass.services.async_register(DOMAIN, SERVICE_FORCE_RECALCULATE, handle_force_recalculate)
    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_VERIFY_INVERTER_MODE):
        hass.services.async_register(
            DOMAIN, SERVICE_FORCE_VERIFY_INVERTER_MODE,
            handle_force_verify_inverter_mode,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and remove its coordinator from hass.data.

    Shared, integration-wide registrations (the sidebar panel and the two
    services) are torn down only when the *last* entry unloads — they are
    registered once and fanned out across every entry, so removing them while
    another entry is live would break that entry. The HTTP debug view and the
    www/ static path are intentionally left registered: Home Assistant exposes
    no public API to remove an aiohttp route once added, and their
    ``_DEBUG_VIEW_KEY`` / ``_STATIC_REGISTERED_KEY`` guards keep a later
    re-setup from double-registering the surviving routes.

    Args:
        hass: Home Assistant instance.
        entry: Config entry being unloaded.

    Returns:
        True when all platforms unloaded successfully.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Cancel the scheduled refresh and the control module's pending
        # verify-tick — otherwise a verify callback scheduled before unload
        # fires afterwards and issues real Modbus writes from a torn-down entry.
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            _async_teardown_shared(hass)
    return unload_ok


def _async_teardown_shared(hass: HomeAssistant) -> None:
    """Remove the shared panel and services once no sunSale entries remain.

    Clearing ``_PANEL_KEY`` resets the cached panel-registration hash so a
    later setup re-registers the panel cleanly (the static path that serves
    its JS survives, so the fresh registration still resolves).

    Args:
        hass: Home Assistant instance.
    """
    if hass.data.get(_PANEL_KEY) is not None:
        try:
            async_remove_panel(hass, _PANEL_URL)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        hass.data.pop(_PANEL_KEY, None)

    for service in (SERVICE_FORCE_RECALCULATE, SERVICE_FORCE_VERIFY_INVERTER_MODE):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options change.

    Args:
        hass: Home Assistant instance.
        entry: Config entry whose options were updated.
    """
    await hass.config_entries.async_reload(entry.entry_id)
