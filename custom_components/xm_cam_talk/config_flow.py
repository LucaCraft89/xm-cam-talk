"""Config flow for XM Camera Talk."""
from __future__ import annotations

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_BRIDGE_URL, CONF_CAMS, CONF_VOICE, DOMAIN


class XMCamTalkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Ask for the bridge URL, discover cameras from /cams."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_BRIDGE_URL].rstrip("/")
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    f"{url}/cams", timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    cams = (await resp.json())["cams"]
            except Exception:  # noqa: BLE001 - any failure = cannot connect
                errors["base"] = "cannot_connect"
            else:
                if not cams:
                    errors["base"] = "no_cams"
                else:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"XM Talk ({url})",
                        data={
                            CONF_BRIDGE_URL: url,
                            CONF_CAMS: cams,
                            CONF_VOICE: user_input.get(CONF_VOICE, "en"),
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BRIDGE_URL, default="http://192.168.1.186:8090"
                    ): str,
                    vol.Optional(CONF_VOICE, default="en"): str,
                }
            ),
            errors=errors,
        )
