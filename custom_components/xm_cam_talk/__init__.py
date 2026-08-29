"""XM Camera Talk integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_BRIDGE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NOTIFY]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = dict(entry.data)
    _LOGGER.debug(
        "Setting up XM Camera Talk: bridge=%s cameras=%s",
        data.get(CONF_BRIDGE_URL), data.get("cams"),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("XM Camera Talk ready (%d camera(s))", len(data.get("cams", [])))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.debug("Unloading XM Camera Talk entry %s", entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
