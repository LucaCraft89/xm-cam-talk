"""Notify entities: one per camera. message -> spoken out the camera speaker."""
from __future__ import annotations

import logging

import aiohttp

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_BRIDGE_URL, CONF_CAMS, CONF_VOICE, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    url = data[CONF_BRIDGE_URL]
    voice = data.get(CONF_VOICE, "en")
    async_add_entities(
        XMCamTalkNotify(url, cam, voice, entry.entry_id) for cam in data[CONF_CAMS]
    )


class XMCamTalkNotify(NotifyEntity):
    """Speak a message out one camera."""

    _attr_has_entity_name = True

    def __init__(self, url: str, cam: str, voice: str, entry_id: str) -> None:
        self._url = url
        self._cam = cam
        self._voice = voice
        self._attr_name = cam
        self._attr_unique_id = f"{entry_id}_{cam}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{cam}")},
            name=f"XM Talk {cam}",
            manufacturer="XM / iCSee (DVRIP OPTalk)",
        )

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        session = async_get_clientsession(self.hass)
        _LOGGER.debug(
            "Speaking on %s via %s/say (%d chars)", self._cam, self._url, len(message)
        )
        try:
            async with session.post(
                f"{self._url}/say",
                json={"cam": self._cam, "text": message, "voice": self._voice},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    _LOGGER.error(
                        "Bridge returned HTTP %s speaking on %s: %s",
                        resp.status, self._cam, body[:200],
                    )
                    resp.raise_for_status()
                _LOGGER.debug("Bridge reply for %s: %s", self._cam, body[:200])
        except aiohttp.ClientError as err:
            _LOGGER.error("Failed to reach bridge %s speaking on %s: %s",
                          self._url, self._cam, err)
            raise
