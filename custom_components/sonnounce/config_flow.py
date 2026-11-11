"""Opsætningen i Home Assistant.

Der er ikke noget at udfylde: Sonnounce finder selv højttalerne. Under
Konfigurer er der to valg: "Tilføj en telefon eller computer", der åbner for
parring i fem minutter, og "Indstillinger", hvor man kan slå "Gem beskeder" til
og skrive adressen på en højttaler, hvis Home Assistant og højttalerne står på
hvert sit net.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_ARCHIVE_DAYS,
    CONF_ARCHIVE_ENABLED,
    CONF_RESET_KEY,
    CONF_SPEAKER_HOST,
    DEFAULT_ARCHIVE_DAYS,
    DOMAIN,
)


class SonnounceConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Sonnounce", data={})
        return self.async_show_form(step_id="user")

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """En Sonos-højttaler dukkede op på nettet."""
        announcer = self.hass.data.get(DOMAIN)
        if announcer is not None:
            announcer.remember_host(str(discovery_info.ip_address))
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Sonnounce", data={})
        self._set_confirm_only()
        return self.async_show_form(step_id="confirm")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SonnounceOptionsFlow()


class SonnounceOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["pair", "settings"])

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Åbner for parring. Kun administratorer kan nå hertil."""
        announcer = self.hass.data.get(DOMAIN)
        if announcer is None:
            return self.async_abort(reason="not_ready")
        announcer.open_pairing()
        return self.async_abort(reason="pairing_open")

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_SPEAKER_HOST] = (user_input.get(CONF_SPEAKER_HOST) or "").strip()
            if user_input.pop(CONF_RESET_KEY, False):
                announcer = self.hass.data.get(DOMAIN)
                if announcer is not None:
                    await announcer.reset_key()
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ARCHIVE_ENABLED, default=options.get(CONF_ARCHIVE_ENABLED, False)
                ): bool,
                vol.Required(
                    CONF_ARCHIVE_DAYS, default=options.get(CONF_ARCHIVE_DAYS, DEFAULT_ARCHIVE_DAYS)
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3650)),
                vol.Optional(
                    CONF_SPEAKER_HOST,
                    description={"suggested_value": options.get(CONF_SPEAKER_HOST, "")},
                ): str,
                vol.Optional(CONF_RESET_KEY, default=False): bool,
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)
