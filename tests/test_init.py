"""Tests for __init__.py — async_setup_entry and async_unload_entry."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.sun_sale import async_setup_entry, async_unload_entry
from custom_components.sun_sale.const import DOMAIN


def make_hass(domain_data: dict | None = None) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: domain_data} if domain_data is not None else {}
    hass.http = MagicMock()
    hass.services = MagicMock()
    hass.services.has_service.return_value = False
    hass.config_entries = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return hass


def make_entry(entry_id: str = "test_entry") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.add_update_listener.return_value = MagicMock()
    return entry


async def test_async_setup_entry_registers_coordinator():
    hass = make_hass()
    entry = make_entry()

    with patch("custom_components.sun_sale.SunSaleCoordinator") as MockCoord:
        mock_coord = MagicMock()
        mock_coord.async_setup = AsyncMock()
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        MockCoord.return_value = mock_coord

        result = await async_setup_entry(hass, entry)

    assert result is True
    assert DOMAIN in hass.data
    assert hass.data[DOMAIN]["test_entry"] is mock_coord


async def test_async_setup_entry_registers_debug_view_once():
    hass = make_hass()
    entry = make_entry()

    with patch("custom_components.sun_sale.SunSaleCoordinator") as MockCoord:
        mock_coord = MagicMock()
        mock_coord.async_setup = AsyncMock()
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        MockCoord.return_value = mock_coord

        await async_setup_entry(hass, entry)

    hass.http.register_view.assert_called_once()


async def test_async_setup_entry_registers_service():
    hass = make_hass()
    entry = make_entry()

    with patch("custom_components.sun_sale.SunSaleCoordinator") as MockCoord:
        mock_coord = MagicMock()
        mock_coord.async_setup = AsyncMock()
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        MockCoord.return_value = mock_coord

        await async_setup_entry(hass, entry)

    hass.services.async_register.assert_called_once()


async def test_async_setup_entry_skips_duplicate_debug_view():
    hass = make_hass()
    hass.data["sun_sale_debug_view_registered"] = True
    entry = make_entry()

    with patch("custom_components.sun_sale.SunSaleCoordinator") as MockCoord:
        mock_coord = MagicMock()
        mock_coord.async_setup = AsyncMock()
        mock_coord.async_config_entry_first_refresh = AsyncMock()
        MockCoord.return_value = mock_coord

        await async_setup_entry(hass, entry)

    hass.http.register_view.assert_not_called()


async def test_async_unload_entry_removes_coordinator_on_success():
    coord = MagicMock()
    hass = make_hass(domain_data={"test_entry": coord})
    entry = make_entry()

    result = await async_unload_entry(hass, entry)

    assert result is True
    assert "test_entry" not in hass.data[DOMAIN]


async def test_async_unload_entry_keeps_coordinator_on_failure():
    coord = MagicMock()
    hass = make_hass(domain_data={"test_entry": coord})
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    entry = make_entry()

    result = await async_unload_entry(hass, entry)

    assert result is False
    assert "test_entry" in hass.data[DOMAIN]
