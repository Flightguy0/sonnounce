"""Selve afsendelsen, og opdagelsen af højttalerne.

Rækkefølgen er den samme som i iPhone- og Homey-appen: læs højttalerens egen
lydstyrke, skru musikken ned, send beskeden med den oprindelige værdi, og skru
op igen når den er talt ud. Musikken skrues ned frem for at blive sat på pause
— pause giver for meget forsinkelse. Højttalere der ikke kan lægge en besked
oven på musikken, går gennem `legacy.py`.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any

from yarl import URL

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .archive import Archive
from .clips import ClipStore
from .const import (
    CLIP_PATH,
    DOMAIN,
    EVENT_FAILED,
    EVENT_PLAYED,
    EVENT_SPEAKERS_FOUND,
    EVENT_SPEAKERS_LOST,
)
from .legacy import Legacy, Published
from .sonos_local import SonosError, SonosLocal, Speaker
from .sonos_upnp import SonosUPnP

_LOGGER = logging.getLogger(__name__)

SONOS_SERVICE = "_sonos._tcp.local."


@dataclass
class AnnounceResult:
    sent: int
    failed: list[str]
    fetched: bool
    speakers: list[str]
    seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "fetched": self.fetched,
            "speakers": self.speakers,
            "seconds": self.seconds,
        }


class AnnounceError(Exception):
    """Beskeden kunne ikke sendes. Teksten går videre til telefonen."""


class Announcer:
    def __init__(self, hass: HomeAssistant, archive: Archive, manual_host: str | None) -> None:
        self.hass = hass
        self.archive = archive
        self.manual_host = clean_host(manual_host)
        session = async_get_clientsession(hass, verify_ssl=False)
        self.sonos = SonosLocal(session)
        self.upnp = SonosUPnP(session)
        self.clips = ClipStore(hass)
        self.legacy = Legacy(hass, self.upnp, self._publish)
        self._store: Store = Store(hass, 1, f"{DOMAIN}.state")
        self.known_host: str | None = None
        self.seen_hosts: set[str] = set()
        self._browser = None
        self._reachable: bool | None = None
        self._checking = False
        self.last_announcement: float | None = None
        self.key: str = ""
        self._failures = 0
        self._failed_at = 0.0

    async def async_start(self) -> None:
        state = await self._store.async_load() or {}
        self.known_host = state.get("known_host")
        # Nøglen telefonen bruger. Laves én gang og bliver stående — en ny ved
        # hver genstart ville betyde at appen holdt op med at virke, hver gang
        # Home Assistant blev opdateret.
        self.key = state.get("key") or ""
        if not self.key:
            self.key = secrets.token_urlsafe(32)
            await self._store.async_save(self._state())
        await self._start_browser()

    async def async_stop(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        self.clips.clear()

    def _state(self) -> dict:
        return {"known_host": self.known_host, "key": self.key}

    async def reset_key(self) -> None:
        """En ny nøgle. Alle telefoner skal så tilføje Home Assistant igen."""
        self.key = secrets.token_urlsafe(32)
        await self._store.async_save(self._state())

    def authorize(self, given: str | None) -> bool:
        """Kontrollerer nøglen på et kald.

        Sammenligningen tager lige lang tid uanset hvor mange tegn der passer,
        og der tages højst imod 20 forkerte forsøg i kvarteret.
        """
        now = time.monotonic()
        if now - self._failed_at > 15 * 60:
            self._failures = 0
        if self._failures >= 20:
            return False
        if self.key and given and hmac.compare_digest(given.encode(), self.key.encode()):
            self._failures = 0
            return True
        self._failures += 1
        self._failed_at = now
        return False

    # Opdagelse

    @callback
    def remember_host(self, host: str | None) -> None:
        """En højttaler set på nettet — af browseren eller Home Assistants egen opdagelse."""
        if host and ":" not in host:
            self.seen_hosts.add(host)

    async def _start_browser(self) -> None:
        """Lytter efter Sonos på Bonjour, så adresserne er kendt på forhånd."""
        try:
            from homeassistant.components import zeroconf
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

            aiozc = await zeroconf.async_get_async_instance(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("ingen Bonjour-opdagelse: %s", err)
            return

        async def resolve(name: str) -> None:
            info = AsyncServiceInfo(SONOS_SERVICE, name)
            if await info.async_request(aiozc.zeroconf, 3000):
                for address in info.parsed_addresses():
                    self.remember_host(address)

        def handler(zeroconf, service_type, name, state_change) -> None:  # noqa: ANN001
            if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
                self.hass.loop.call_soon_threadsafe(
                    lambda: self.hass.async_create_background_task(resolve(name), "sonnounce resolve")
                )

        self._browser = AsyncServiceBrowser(aiozc.zeroconf, SONOS_SERVICE, handlers=[handler])

    async def all_systems(self, preferred: str | None = None) -> tuple[str, list[Speaker]]:
        hosts = [clean_host(preferred), self.manual_host, self.known_host, *self.seen_hosts]
        try:
            entry, speakers = await self.sonos.fetch_all_systems(hosts)
        except SonosError as err:
            raise AnnounceError(
                "no speaker could be reached on the local network"
                if self.manual_host
                else "no speaker could be reached. If Home Assistant and the speakers are on "
                "separate networks, enter the address of one speaker under Configure on "
                "the Sonnounce integration"
            ) from err
        if entry != self.known_host:
            self.known_host = entry
            self._store.async_delay_save(self._state, 5)
        return entry, speakers

    async def list_speakers(self) -> list[dict[str, str]]:
        _, speakers = await self.all_systems()
        return [{"id": s.id, "name": s.name} for s in speakers if s.can_announce]

    # Adressen højttalerne henter fra

    def _base_url(self, speaker_host: str) -> str:
        """Hvor højttaleren kan nå Home Assistant.

        Har man selv skrevet en intern adresse ind i Home Assistant, er det den.
        Ellers den adresse Home Assistant bruger mod højttaleren — den kan
        højttaleren nå tilbage på, for den kom lige derfra.
        """
        internal = self.hass.config.internal_url
        if internal:
            url = URL(internal)
            if url.scheme == "http":
                return str(url.with_path("").with_query(None)).rstrip("/")
        port = self.hass.http.server_port or 8123
        scheme = "https" if self.hass.http.ssl_certificate else "http"
        return f"{scheme}://{local_address_toward(speaker_host)}:{port}"

    def _publish(self, data: bytes, content_type: str, speaker_host: str) -> Published:
        clip_id = self.clips.put(data, content_type)
        url = f"{self._base_url(speaker_host)}{CLIP_PATH}/{clip_id}"
        return Published(id=clip_id, url=url, drop=lambda: self.clips.drop(clip_id))

    # Afsendelse

    async def announce(
        self,
        audio: bytes,
        content_type: str = "audio/wav",
        wanted: list[str] | None = None,
        volume: int | None = None,
        music_volume: int = 2,
        duck: bool = True,
        duration: float | None = None,
        host: str | None = None,
        archive: bool = True,
    ) -> AnnounceResult:
        """Sender beskeden og fortæller automatiseringer hvad der kom ud af det."""
        try:
            result = await self._run(audio, content_type, wanted, volume, music_volume, duck, duration, host)
        except AnnounceError as err:
            self.hass.bus.async_fire(EVENT_FAILED, {"error": str(err)})
            raise

        if result.sent == 0:
            self.hass.bus.async_fire(
                EVENT_FAILED,
                {
                    "error": f"the announcement was not played on: {', '.join(result.failed)}"
                    if result.failed
                    else "no speaker accepted the announcement"
                },
            )
            return result

        reached = [name for name in result.speakers if name not in result.failed]
        self.last_announcement = time.monotonic()

        if archive and self.archive.enabled and "wav" in content_type.lower():
            try:
                await self.archive.add(audio, reached, result.seconds)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("kunne ikke gemme beskeden: %s", err)

        self.hass.bus.async_fire(
            EVENT_PLAYED,
            {"speakers": reached, "seconds": round(result.seconds), "heard": result.fetched},
        )
        return result

    async def _run(self, audio, content_type, wanted, volume, music_volume, duck, duration, host) -> AnnounceResult:
        if not audio:
            raise AnnounceError("no audio was included")

        entry, system = await self.all_systems(host)
        targets = pick_speakers(system, wanted)
        if not targets:
            raise AnnounceError("none of the selected speakers were found")

        clip = self._publish(audio, content_type, entry)
        seconds = duration or estimate_seconds(audio, content_type)
        originals: dict[str, int] = {}
        failed: list[str] = []
        fetch_ids = [clip.id]

        async def one(speaker: Speaker) -> None:
            if not speaker.supports_audio_clip:
                try:
                    clip_volume = volume if volume is not None else await self.upnp.volume(speaker.host)
                    extra = await self.legacy.start(
                        speaker,
                        stream_url=clip.url,
                        audio=audio,
                        content_type=content_type,
                        duration=seconds,
                        clip_volume=clip_volume,
                        music_volume=music_volume if duck else None,
                    )
                    if extra:
                        fetch_ids.append(extra)
                except Exception as err:  # noqa: BLE001
                    failed.append(speaker.name)
                    _LOGGER.warning("%s: %s", speaker.name, err)
                return
            try:
                clip_volume = volume
                if clip_volume is None or duck:
                    current = await self.sonos.volume(speaker)
                    originals[speaker.id] = current
                    if clip_volume is None:
                        clip_volume = current
                if duck and speaker.is_playing:
                    await self.sonos.set_volume(speaker, music_volume)
                await self.sonos.load_audio_clip(speaker, clip.url, clip_volume)
            except Exception as err:  # noqa: BLE001
                failed.append(speaker.name)
                _LOGGER.warning("%s: %s", speaker.name, err)

        await asyncio.gather(*(one(s) for s in targets))

        async def restore() -> None:
            await asyncio.sleep(seconds + 2)
            for speaker in targets:
                if not speaker.supports_audio_clip or not duck or not speaker.is_playing:
                    continue
                original = originals.get(speaker.id)
                if original is None:
                    continue
                try:
                    await self.sonos.set_volume(speaker, original)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("kunne ikke skrue op igen på %s: %s", speaker.name, err)
            clip.drop()

        self.hass.async_create_background_task(restore(), "sonnounce restore")

        fetched = False
        if len(failed) < len(targets):
            waits = [asyncio.ensure_future(self.clips.wait_for_fetch(i, 6)) for i in fetch_ids]
            for done in asyncio.as_completed(waits):
                if await done:
                    fetched = True
                    break
            for task in waits:
                task.cancel()
        if not fetched:
            _LOGGER.warning("ingen højttaler hentede beskeden fra %s", clip.url)

        return AnnounceResult(
            sent=len(targets) - len(failed),
            failed=failed,
            fetched=fetched,
            speakers=[s.name for s in targets],
            seconds=seconds,
        )

    # Holder øje med om højttalerne svarer

    async def check_reachable(self) -> None:
        """Kaldes hvert femte minut. Hændelserne fyrer kun når svaret skifter."""
        if self._checking:
            return
        self._checking = True
        try:
            try:
                reachable = len(await self.list_speakers()) > 0
            except Exception:  # noqa: BLE001
                reachable = False
        finally:
            self._checking = False
        previous, self._reachable = self._reachable, reachable
        if previous is None or previous == reachable:
            return
        self.hass.bus.async_fire(EVENT_SPEAKERS_FOUND if reachable else EVENT_SPEAKERS_LOST)

    @property
    def reachable(self) -> bool | None:
        return self._reachable


def clean_host(value: str | None) -> str:
    """Renser en adresse man selv har skrevet ind (http://, sti, port, anførselstegn)."""
    host = str(value or "").strip().strip("\"'")
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.strip()


def pick_speakers(system: list[Speaker], wanted: list[str] | None) -> list[Speaker]:
    """Id først, navn bagefter: et id skifter hvis en højttaler bliver sat op igen."""
    usable = [s for s in system if s.can_announce]
    if not wanted:
        return usable
    asked = {str(w).strip().casefold() for w in wanted}
    return [s for s in usable if s.id.casefold() in asked or s.name.casefold() in asked]


def local_address_toward(host: str) -> str:
    """Den af vores egne adresser, rutetabellen bruger mod højttaleren.

    Der sendes ingenting — en UDP-sokkel skal bare vælge sin afsenderadresse.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((host, 1400))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def estimate_seconds(audio: bytes, content_type: str) -> float:
    """Hvor længe beskeden varer. Kun WAV kan regnes ud; ellers gættes højt."""
    if "wav" not in (content_type or "").lower():
        return 60
    pos = 12
    byte_rate = 0
    while pos + 8 <= len(audio):
        chunk_id = audio[pos:pos + 4]
        size = struct.unpack_from("<I", audio, pos + 4)[0]
        if chunk_id == b"fmt " and pos + 20 <= len(audio):
            byte_rate = struct.unpack_from("<I", audio, pos + 16)[0]
        if chunk_id == b"data":
            if byte_rate > 0:
                return -(-size // byte_rate)
            break
        pos += 8 + size + (size % 2)
    return 60
