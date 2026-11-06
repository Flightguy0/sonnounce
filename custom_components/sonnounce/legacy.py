"""Beskeder på højttalere der ikke kan lægge en besked oven på musikken.

Det er S1 og de ældste modeller på S2. Musikken tones blødt ned, holder pause
mens beskeden lyder, og tones op igen bagefter — samme nummer, samme sted i
sangen, samme radio, samme lydstyrke. Radio kommer tilbage gennem køen:
beskeden og radioen lægges efter hinanden, så højttaleren gør radioen klar mens
beskeden spiller.

Samme fremgangsmåde som `Legacy.js` i Homey-appen og `LegacyAnnouncer.swift`
i iPhone-appen, hvor tallene er forklaret.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CLIP_PATH
from .sonos_local import Speaker
from .sonos_upnp import SonosUPnP, seconds

_LOGGER = logging.getLogger(__name__)

QUEUE_LEAD = 6.5
SHORTEST_RADIO_CLIP = 7.0
RADIO_TAIL = 0.6

LIVE_PREFIXES = (
    "x-sonosapi-stream:", "x-sonosapi-radio:", "x-rincon-mp3radio:", "x-sonosapi-hls:",
    "x-rincon-stream:", "x-sonos-htastream:", "aac:", "hls-radio:", "x-sonos-vli:",
)


def is_clip_url(address: str) -> bool:
    return f"{CLIP_PATH}/" in (address or "")


def is_direct_radio(track_uri: str) -> bool:
    """Selve radioudsendelsen, ikke TuneIns indslag før den."""
    if not (track_uri.startswith("x-rincon-mp3radio://") or track_uri.startswith("aac://")):
        return False
    return not any(word in track_uri for word in ("tunein-ondemand", "bump", ".m3u8"))


def is_live(uri: str) -> bool:
    return uri.startswith(LIVE_PREFIXES)


def is_seekable(value: str) -> bool:
    parts = value.split(":")
    return (
        len(parts) == 3
        and all(p.isdigit() for p in parts)
        and value not in ("0:00:00", "00:00:00")
    )


def without_repeat(mode: str) -> str | None:
    if mode in ("REPEAT_ALL", "REPEAT_ONE"):
        return "NORMAL"
    if mode in ("SHUFFLE", "SHUFFLE_REPEAT_ONE"):
        return "SHUFFLE_NOREPEAT"
    return None


def didl(url: str) -> str:
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="sonos-announcements" parentID="-1" restricted="true">'
        "<dc:title>Sonnounce</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:audio/wav:*">{escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


def pad_wav(audio: bytes, minimum: float, tail: float) -> tuple[bytes, float | None]:
    """Lægger stilhed til sidst i en WAV-fil.

    Filen kommer til at vare mindst `minimum` sekunder og få mindst `tail`
    sekunders stilhed til sidst. Er det ikke en WAV vi kan læse, kommer den
    tilbage som den er.
    """
    pos = 12
    byte_rate = 0
    block_align = 2
    while pos + 8 <= len(audio):
        chunk_id = audio[pos:pos + 4]
        size = struct.unpack_from("<I", audio, pos + 4)[0]
        if chunk_id == b"fmt " and pos + 24 <= len(audio):
            byte_rate = struct.unpack_from("<I", audio, pos + 16)[0]
            block_align = struct.unpack_from("<H", audio, pos + 20)[0] or 2
        if chunk_id == b"data" and byte_rate > 0:
            data = audio[pos + 8:min(len(audio), pos + 8 + size)]
            now = len(data) / byte_rate
            extra = max(tail, minimum - now)
            pad = round(extra * byte_rate)
            pad -= pad % block_align
            head = bytearray(audio[:pos + 8])
            struct.pack_into("<I", head, pos + 4, len(data) + pad)
            out = bytes(head) + data + bytes(pad)
            out = out[:4] + struct.pack("<I", len(out) - 8) + out[8:]
            return out, (len(data) + pad) / byte_rate
        pos += 8 + size + (size % 2)
    return audio, None


@dataclass
class Published:
    id: str
    url: str
    drop: Callable[[], None]


@dataclass
class Snapshot:
    player_id: str
    host: str
    name: str
    media: dict
    position: dict
    was_playing: bool
    play_mode: str
    volume: int
    muted: bool
    quiet: int | None = None


@dataclass
class Entry:
    snapshot: Snapshot
    generation: int
    clip: str
    radio: dict | None = None
    drop: Callable[[], None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Legacy:
    def __init__(
        self,
        hass: HomeAssistant,
        upnp: SonosUPnP,
        publish: Callable[[bytes, str, str], Published],
    ) -> None:
        self.hass = hass
        self.upnp = upnp
        # publish(audio, content_type, speaker_host) → Published
        self.publish = publish
        self.active: dict[str, Entry] = {}
        self.generation = 0

    def _background(self, coro, name: str) -> None:
        self.hass.async_create_background_task(coro, name)

    async def start(
        self,
        speaker: Speaker,
        *,
        stream_url: str,
        audio: bytes,
        content_type: str,
        duration: float,
        clip_volume: int,
        music_volume: int | None,
    ) -> str | None:
        """Starter beskeden og vender tilbage når den spiller.

        Giver id'et på en ekstra fil tilbage, når højttaleren henter sin egen
        udgave af beskeden (før radio).
        """
        host = speaker.host
        running = self.active.get(speaker.id)
        snapshot = running.snapshot if running else await self._snapshot(speaker)
        self.generation += 1
        mine = self.generation

        member = snapshot.media["uri"].startswith("x-rincon:")
        quiet = None if music_volume is None else min(music_volume, snapshot.volume)

        if (
            not running
            and not member
            and snapshot.was_playing
            and "wav" in (content_type or "").lower()
            and is_direct_radio(snapshot.position["track_uri"])
            and not (seconds(snapshot.position["duration"]) or 0) > 0
        ):
            return await self._start_radio(speaker, snapshot, mine, audio, clip_volume, quiet)

        if not running and not member and snapshot.was_playing and quiet is not None and quiet < snapshot.volume:
            await self._fade(host, snapshot.volume, quiet)
            snapshot.quiet = quiet
            if snapshot.media["uri"].startswith("x-rincon-queue:"):
                await self._quietly(self.upnp.pause(host))
                now = await self._quietly(self.upnp.position_info(host))
                if now and now["track"] == snapshot.position["track"]:
                    snapshot.position["rel_time"] = now["rel_time"]

        self.active[speaker.id] = Entry(snapshot=snapshot, generation=mine, clip=stream_url)

        if member:
            # Kun den valgte højttaler tages ud af gruppen; resten spiller videre.
            await self._quietly(self.upnp.leave_group(host))
        elif snapshot.media["uri"].startswith("x-rincon-queue:") and without_repeat(snapshot.play_mode):
            await self._quietly(self.upnp.set_play_mode(host, without_repeat(snapshot.play_mode)))

        await self.upnp.set_uri(host, stream_url, didl(stream_url))
        if snapshot.muted:
            await self._quietly(self.upnp.set_mute(host, False))
        await self._quietly(self.upnp.set_volume(host, clip_volume))
        await self.upnp.play(host)
        _LOGGER.debug("%s: besked med pause (før: %s)", speaker.name, snapshot.media["uri"])

        self._background(self._finish(speaker.id, mine, duration), f"sonnounce legacy {speaker.name}")
        return None

    @staticmethod
    async def _quietly(coro):
        try:
            return await coro
        except Exception:  # noqa: BLE001 — en enkelt kommando må ikke vælte resten
            return None

    async def _snapshot(self, speaker: Speaker) -> Snapshot:
        host = speaker.host
        q = self._quietly
        media, position, state, mode, volume, muted = await asyncio.gather(
            self.upnp.media_info(host),
            q(self.upnp.position_info(host)),
            q(self.upnp.transport_state(host)),
            q(self.upnp.play_mode(host)),
            self.upnp.volume(host),
            q(self.upnp.is_muted(host)),
        )
        return Snapshot(
            player_id=speaker.id,
            host=host,
            name=speaker.name,
            media=media,
            position=position or {"track": 0, "rel_time": "", "track_uri": "", "duration": ""},
            was_playing=state in ("PLAYING", "TRANSITIONING"),
            play_mode=mode or "NORMAL",
            volume=volume,
            muted=bool(muted),
        )

    # Radio

    async def _start_radio(self, speaker, snapshot, mine, audio, clip_volume, quiet) -> str | None:
        host = speaker.host
        original = snapshot.volume
        padded, length = pad_wav(audio, SHORTEST_RADIO_CLIP, RADIO_TAIL)
        clip = self.publish(padded, "audio/wav", host)
        duration = length or SHORTEST_RADIO_CLIP
        radio = snapshot.position["track_uri"]

        from_queue = snapshot.media["uri"].startswith("x-rincon-queue:")
        position = snapshot.position["track"] + 1 if from_queue and snapshot.position["track"] > 0 else 0
        queued = asyncio.ensure_future(self.upnp.add_to_queue(host, clip.url, didl(clip.url), position))
        if quiet is not None and quiet < original:
            await self._fade(host, original, quiet)
        track = await queued
        mode = snapshot.play_mode if snapshot.play_mode != "NORMAL" else None

        handover = {
            "duration": duration,
            "track": track,
            "radio": radio,
            "play_mode": mode,
            "quiet": quiet,
            "started_at": time.monotonic(),
        }
        self.active[speaker.id] = Entry(
            snapshot=snapshot, generation=mine, clip=clip.url, radio=handover, drop=clip.drop
        )

        # Stop først og sæt beskedens lydstyrke mens der er stille.
        await self._quietly(self.upnp.stop(host))
        if snapshot.muted:
            await self._quietly(self.upnp.set_mute(host, False))
        await self._quietly(self.upnp.set_volume(host, clip_volume))
        if not from_queue:
            await self.upnp.set_uri(host, f"x-rincon-queue:{speaker.id}#0", "")
        if mode:
            await self._quietly(self.upnp.set_play_mode(host, "NORMAL"))
        await self.upnp.seek(host, "TRACK_NR", str(track))
        await self.upnp.play(host)
        handover["started_at"] = time.monotonic()
        _LOGGER.debug("%s: besked før radioen, nr. %s i køen", speaker.name, track)

        async def queue_radio() -> None:
            await asyncio.sleep(max(0.0, 0.25 + duration - QUEUE_LEAD))
            entry = self.active.get(speaker.id)
            if not entry or entry.generation != mine:
                return
            try:
                await self.upnp.add_to_queue(host, radio, snapshot.media["metadata"], track + 1)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("radioen kom ikke i køen: %s", err)

        self._background(queue_radio(), "sonnounce radio queue")
        self._background(self._finish(speaker.id, mine, duration), f"sonnounce legacy {speaker.name}")
        return clip.id

    # Bagefter

    async def _finish(self, speaker_id: str, mine: int, duration: float) -> None:
        entry = self.active.get(speaker_id)
        if not entry or entry.generation != mine:
            return
        host = entry.snapshot.host
        try:
            waited = (
                await self._wait_for_radio(host, entry, speaker_id, mine)
                if entry.radio
                else await self._wait_until_done(host, entry.clip, duration)
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: %s", entry.snapshot.name, err)
            waited = "timedOut"

        if entry.drop:
            self.hass.loop.call_later(5, entry.drop)

        current = self.active.get(speaker_id)
        if not current or current.generation != mine:
            return
        del self.active[speaker_id]

        if entry.radio:
            if waited == "wentOn":
                await self._tidy_queue(host, entry.radio["radio"])
                if entry.radio["play_mode"]:
                    await self._quietly(self.upnp.set_play_mode(host, entry.radio["play_mode"]))
                return
            await self._tidy_queue(host, None)
            if waited != "replaced":
                if entry.radio["play_mode"]:
                    await self._quietly(self.upnp.set_play_mode(host, entry.radio["play_mode"]))
                await self._resume_radio(host, entry.radio["radio"], entry.snapshot, entry.radio["quiet"])
                return

        if waited == "replaced":
            await self._quietly(self.upnp.set_volume(host, entry.snapshot.volume))
            return
        await self._restore(entry.snapshot)

    async def _wait_until_done(self, host: str, clip: str, duration: float) -> str:
        began = time.monotonic()
        heard_playing = False
        while time.monotonic() - began < duration + 8:
            await asyncio.sleep(0.4 if heard_playing else 0.25)
            state = await self._quietly(self.upnp.transport_state(host))
            if not state:
                continue
            position = await self._quietly(self.upnp.position_info(host))
            other = bool(
                position
                and position["track_uri"]
                and position["track_uri"] != clip
                and not is_clip_url(position["track_uri"])
            )
            if state in ("PLAYING", "TRANSITIONING"):
                if not heard_playing and other:
                    return "replaced"
                heard_playing = True
                continue
            if other:
                return "replaced"
            if heard_playing or time.monotonic() - began > 6:
                return "finished"
        return "timedOut"

    async def _wait_for_radio(self, host: str, entry: Entry, speaker_id: str, mine: int) -> str:
        radio = entry.radio
        began = time.monotonic()
        clip_seen = False
        lowered = False

        def still_mine() -> bool:
            current = self.active.get(speaker_id)
            return bool(current and current.generation == mine)

        while time.monotonic() - began < radio["duration"] + 12:
            await asyncio.sleep(0.1)
            position = await self._quietly(self.upnp.position_info(host))
            state = await self._quietly(self.upnp.transport_state(host))
            if not position or not state:
                continue
            playing = state in ("PLAYING", "TRANSITIONING")
            lower_at = radio["started_at"] + 0.25 + radio["duration"] - RADIO_TAIL / 2

            if position["track_uri"] == entry.clip:
                if state == "PLAYING":
                    clip_seen = True
                if not lowered and radio["quiet"] is not None and time.monotonic() >= lower_at and still_mine():
                    lowered = True
                    await self._quietly(self.upnp.set_volume(host, radio["quiet"]))
                if not playing and clip_seen:
                    return "finished"
                continue
            if is_clip_url(position["track_uri"]):
                continue
            if position["track_uri"] == radio["radio"] and (clip_seen or lowered):
                if state != "PLAYING":
                    if not playing:
                        return "finished"
                    continue
                if radio["quiet"] is not None and still_mine():
                    if not lowered:
                        await self._quietly(self.upnp.set_volume(host, radio["quiet"]))
                    await self._ramp(host, radio["quiet"], entry.snapshot.volume)
                elif still_mine():
                    await self._quietly(self.upnp.set_volume(host, entry.snapshot.volume))
                return "wentOn"
            if position["track_uri"] and (clip_seen or time.monotonic() - began > 4):
                return "replaced"
        return "timedOut"

    async def _resume_radio(self, host: str, radio: str, snapshot: Snapshot, quiet: int | None) -> None:
        try:
            uris = await self._quietly(self.upnp.queue_uris(host)) or []
            if radio not in uris:
                await self.upnp.add_to_queue(host, radio, snapshot.media["metadata"])
                uris = await self._quietly(self.upnp.queue_uris(host)) or [radio]
            number = len(uris) - uris[::-1].index(radio)
            start = quiet if quiet is not None else snapshot.volume
            await self._quietly(self.upnp.set_volume(host, start))
            await self.upnp.set_uri(host, f"x-rincon-queue:{snapshot.player_id}#0", "")
            await self.upnp.seek(host, "TRACK_NR", str(number))
            await self.upnp.play(host)
            for _ in range(60):
                await asyncio.sleep(0.2)
                if await self._quietly(self.upnp.transport_state(host)) == "PLAYING":
                    break
            await self._ramp(host, start, snapshot.volume)
            await self._tidy_queue(host, radio)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: radioen kom ikke i gang igen: %s", snapshot.name, err)
            await self._quietly(self.upnp.set_volume(host, snapshot.volume))

    async def _tidy_queue(self, host: str, radio: str | None) -> None:
        """Fjerner beskeder fra køen, og radioer vi selv har lagt der."""
        uris = await self._quietly(self.upnp.queue_uris(host))
        if uris is None:
            return
        info = await self._quietly(self.upnp.position_info(host))
        current = info["track"] if info else 0
        doomed = [
            index + 1
            for index, uri in enumerate(uris)
            if index + 1 != current and (is_clip_url(uri) or (radio and uri == radio))
        ]
        for number in sorted(doomed, reverse=True):
            await self._quietly(self.upnp.remove_from_queue(host, number))

    async def _restore(self, s: Snapshot) -> None:
        host = s.host
        uri = s.media["uri"]
        _LOGGER.debug("%s: sætter tilbage til %s", s.name, uri)
        await self._quietly(self.upnp.set_volume(host, s.quiet if s.quiet is not None else s.volume))
        if s.muted:
            await self._quietly(self.upnp.set_mute(host, True))
        try:
            if uri.startswith("x-rincon:"):
                await self.upnp.set_uri(host, uri, "")
                await self._quietly(self.upnp.set_volume(host, s.volume))
                return
            if not uri:
                await self.upnp.set_uri(host, f"x-rincon-queue:{s.player_id}#0", "")
                await self._quietly(self.upnp.set_volume(host, s.volume))
                return
            target = (
                s.position["track_uri"]
                if is_live(uri) and is_direct_radio(s.position["track_uri"])
                else uri
            )
            await self.upnp.set_uri(host, target, s.media["metadata"])
            if uri.startswith("x-rincon-queue:"):
                if s.position["track"] > 0:
                    await self._quietly(self.upnp.seek(host, "TRACK_NR", str(s.position["track"])))
                if is_seekable(s.position["rel_time"]):
                    await self._seek_and_check(host, s.position["rel_time"])
                if without_repeat(s.play_mode):
                    await self._quietly(self.upnp.set_play_mode(host, s.play_mode))
            elif not is_live(uri) and is_seekable(s.position["rel_time"]):
                await self._seek_and_check(host, s.position["rel_time"])

            if not s.was_playing:
                if s.quiet is not None:
                    await self._quietly(self.upnp.set_volume(host, s.volume))
                return
            await self.upnp.play(host)
            if s.quiet is not None:
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if await self._quietly(self.upnp.transport_state(host)) == "PLAYING":
                        break
                await self._ramp(host, s.quiet, s.volume)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: kunne ikke sætte musikken tilbage: %s", s.name, err)
            await self._quietly(self.upnp.set_volume(host, s.volume))

    async def _seek_and_check(self, host: str, target: str) -> None:
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(0.4)
            await self._quietly(self.upnp.seek(host, "REL_TIME", target))
            now = await self._quietly(self.upnp.position_info(host))
            want = seconds(target)
            got = seconds(now["rel_time"]) if now else None
            if want is None or got is None or abs(got - want) <= 3:
                return

    async def _fade(self, host: str, start: int, end: int) -> None:
        for step in (start - (start - end) // 2, end + (start - end) // 5, end):
            await self._quietly(self.upnp.set_volume(host, step))
            await asyncio.sleep(0.06)

    async def _ramp(self, host: str, start: int, end: int) -> None:
        if end <= start:
            await self._quietly(self.upnp.set_volume(host, end))
            return
        for i in range(1, 9):
            t = i / 8
            await self._quietly(self.upnp.set_volume(host, start + round((end - start) * t * t)))
            await asyncio.sleep(0.15)
