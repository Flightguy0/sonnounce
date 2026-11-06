"""Beskederne mens højttalerne henter dem.

I iPhone-appen ligger den samme server inde i telefonen. Er man ude, kan
højttaleren ikke nå telefonen — så er det Home Assistant, inde i huset, der
holder filen mens den bliver hentet. Den ligger i hukommelsen, ikke på disken,
og forsvinder efter to minutter.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from .const import CLIP_PATH

_LOGGER = logging.getLogger(__name__)

LIFETIME = 120


@dataclass
class Clip:
    data: bytes
    content_type: str
    fetched: asyncio.Event = field(default_factory=asyncio.Event)
    handle: asyncio.TimerHandle | None = None


class ClipStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._clips: dict[str, Clip] = {}

    @callback
    def put(self, data: bytes, content_type: str) -> str:
        clip_id = secrets.token_urlsafe(12)
        clip = Clip(data=data, content_type=content_type)
        clip.handle = self.hass.loop.call_later(LIFETIME, self.drop, clip_id)
        self._clips[clip_id] = clip
        return clip_id

    @callback
    def drop(self, clip_id: str) -> None:
        clip = self._clips.pop(clip_id, None)
        if clip and clip.handle:
            clip.handle.cancel()

    def get(self, clip_id: str) -> Clip | None:
        return self._clips.get(clip_id)

    async def wait_for_fetch(self, clip_id: str, timeout: float) -> bool:
        """Venter på at en højttaler henter klippet.

        Sonos svarer gladeligt at kommandoen lykkedes, uden at der kommer lyd
        ud. Hentningen er det eneste håndfaste tegn på at beskeden nåede frem.
        """
        clip = self._clips.get(clip_id)
        if not clip:
            return False
        try:
            async with asyncio.timeout(timeout):
                await clip.fetched.wait()
            return True
        except TimeoutError:
            return False

    @callback
    def clear(self) -> None:
        for clip_id in list(self._clips):
            self.drop(clip_id)


class ClipView(HomeAssistantView):
    """Det højttaleren henter fra. Uden login — en højttaler har ikke noget."""

    url = CLIP_PATH + "/{clip_id}"
    name = "api:sonnounce:clip"
    requires_auth = False

    def __init__(self, store_getter) -> None:
        self._store_getter = store_getter

    async def _serve(self, request: web.Request, clip_id: str, head: bool) -> web.StreamResponse:
        store: ClipStore | None = self._store_getter()
        clip = store.get(clip_id) if store else None
        if not clip:
            return web.Response(status=404, text="not found")
        _LOGGER.debug("%s hentede %s (%d bytes)", request.remote, clip_id, len(clip.data))
        clip.fetched.set()
        headers = {
            "Content-Type": clip.content_type,
            "Content-Length": str(len(clip.data)),
            "Accept-Ranges": "none",
            "Cache-Control": "no-store",
        }
        if head:
            return web.Response(headers=headers)
        return web.Response(body=clip.data, headers=headers)

    async def get(self, request: web.Request, clip_id: str) -> web.StreamResponse:
        return await self._serve(request, clip_id, False)

    async def head(self, request: web.Request, clip_id: str) -> web.StreamResponse:
        return await self._serve(request, clip_id, True)
