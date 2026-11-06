"""Sonnounce — Home Assistant-delen.

Appen på telefonen taler selv direkte med højttalerne, og det er stadig den vej
der bruges hjemme. Den her integration findes af samme grund som Homey-appen:
når telefonen ikke er på husets net, kan højttaleren ikke hente lyden fra den.
Home Assistant står i huset, så den kan holde filen frem imens.

Beskeden går telefon → Home Assistants fjernadgang → Home Assistant →
højttaleren henter den lokalt. Lyden lander aldrig et offentligt sted.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .announcer import Announcer
from .archive import Archive
from .clips import ClipView
from .const import (
    CONF_ARCHIVE_DAYS,
    CONF_ARCHIVE_ENABLED,
    CONF_SPEAKER_HOST,
    DEFAULT_ARCHIVE_DAYS,
    DOMAIN,
)
from .views import ALL_VIEWS


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Adresserne registreres med det samme og bliver stående.

    Home Assistant kan ikke fjerne en adresse igen, så de spørger selv om
    integrationen er sat op, og svarer pænt hvis den ikke er.
    """
    for view in ALL_VIEWS:
        hass.http.register_view(view())
    hass.http.register_view(ClipView(lambda: getattr(hass.data.get(DOMAIN), "clips", None)))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    options = entry.options
    archive = Archive(
        hass,
        enabled=bool(options.get(CONF_ARCHIVE_ENABLED, False)),
        keep_days=int(options.get(CONF_ARCHIVE_DAYS, DEFAULT_ARCHIVE_DAYS)),
    )
    await archive.init()

    announcer = Announcer(hass, archive, options.get(CONF_SPEAKER_HOST))
    await announcer.async_start()
    hass.data[DOMAIN] = announcer

    async def check(_now=None) -> None:
        await announcer.check_reachable()

    entry.async_on_unload(async_track_time_interval(hass, check, timedelta(minutes=5)))
    entry.async_on_unload(
        async_track_time_interval(hass, lambda _now: hass.async_create_task(archive.prune()), timedelta(days=1))
    )
    entry.async_on_unload(entry.add_update_listener(_options_changed))
    entry.async_create_background_task(hass, check(), "sonnounce first check")
    return True


async def _options_changed(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    announcer: Announcer | None = hass.data.pop(DOMAIN, None)
    if announcer:
        await announcer.async_stop()
    return True
