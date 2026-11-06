"""Taler direkte med Sonos-højttalerne på det lokale net.

Det er den samme vej iPhone-appen og Homey-appen bruger: en websocket på port
1443, som Sonos' egen app taler med. Den kræver hverken konto, login eller
udviklernøgle — kun at afsenderen er på samme net. Home Assistant står i
huset, så den betingelse er opfyldt, også når telefonen er et helt andet sted.

Grænsefladen er ikke offentligt dokumenteret og kan i princippet ændre sig med
en firmwareopdatering. Til gengæld er der ingen sky imellem og ingen konto der
kan mangle.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass

import aiohttp
from yarl import URL

API_KEY = "123e4567-e89b-12d3-a456-426655440000"
SUBPROTOCOL = "v1.api.smartspeaker.audio"
TIMEOUT = 12

_HOUSEHOLD = re.compile(r"<HouseholdControlID>([^<]+)</HouseholdControlID>")


@dataclass
class Speaker:
    """Én højttaler, som den så ud da systemet sidst blev spurgt."""

    id: str
    name: str
    host: str
    household_id: str | None
    supports_audio_clip: bool
    supports_playback: bool
    group_id: str | None
    is_playing: bool

    @property
    def can_announce(self) -> bool:
        return self.supports_audio_clip or self.supports_playback


class SonosError(Exception):
    """En højttaler sagde nej eller svarede ikke."""


class SonosLocal:
    """Kommandoerne over websocketten."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Pr. adresse og ikke ét for alle: et hus kan have både et S1- og et
        # S2-system, og spørger man det ene med det andets id, svarer det med
        # en fejl.
        self._households: dict[str, str] = {}

    async def _call(self, host: str, header: dict, body: dict | None = None) -> dict:
        """Sender én kommando og venter på svaret.

        Hver kommando får sin egen forbindelse. En besked sendes sjældent, og
        en forbindelse der har stået åben i timevis er den slags der fejler
        netop når den skal bruges.
        """
        cmd_id = str(time.time_ns())
        url = f"wss://{host}:1443/websocket/api"
        try:
            async with asyncio.timeout(TIMEOUT):
                async with self._session.ws_connect(
                    url,
                    protocols=[SUBPROTOCOL],
                    headers={"X-Sonos-Api-Key": API_KEY},
                    # Højttaleren har et selvsigneret certifikat. Der er ingen
                    # udsteder at spørge på et lokalt net.
                    ssl=False,
                ) as ws:
                    await ws.send_str(json.dumps([{**header, "cmdId": cmd_id}, body or {}]))
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        try:
                            parsed = json.loads(msg.data)
                        except ValueError:
                            continue
                        if not isinstance(parsed, list) or len(parsed) < 2:
                            continue
                        reply_header, reply_body = parsed[0], parsed[1]
                        if reply_header.get("cmdId") != cmd_id:
                            continue  # Hændelser højttaleren sender af sig selv.
                        if reply_header.get("success") is False or reply_header.get("type") == "globalError":
                            raise SonosError(
                                (reply_body or {}).get("errorCode") or f"{header['command']} was rejected"
                            )
                        return reply_body or {}
        except TimeoutError as err:
            raise SonosError(f"{host} did not answer {header['command']}") from err
        except aiohttp.ClientError as err:
            raise SonosError(f"{host}: {err}") from err
        raise SonosError(f"the connection to {host} closed")

    async def status_page(self, host: str, timeout: float = TIMEOUT) -> str:
        """Højttalerens statusside, frit tilgængelig på det lokale net."""
        try:
            async with self._session.get(
                f"http://{host}:1400/status/zp", timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                return await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SonosError(f"{host} did not answer") from err

    async def household_id(self, host: str) -> str:
        if host in self._households:
            return self._households[host]
        match = _HOUSEHOLD.search(await self.status_page(host))
        if not match:
            raise SonosError("the speaker did not report its household id")
        self._households[host] = match.group(1)
        return match.group(1)

    async def is_sonos(self, host: str) -> bool:
        try:
            return bool(_HOUSEHOLD.search(await self.status_page(host, 4)))
        except SonosError:
            return False

    async def fetch_all_systems(self, hosts: list[str | None]) -> tuple[str, list[Speaker]]:
        """Højttalerne fra alle Sonos-systemer, der kan nås fra adresserne.

        Et S1 og et S2 kan ikke være i samme system, men de står fint på samme
        net. Adresserne spørges samtidig, og der spørges kun én i hvert system
        om resten. Den første der svarer står først — den huskes til næste gang.
        """
        unique = list(dict.fromkeys(h for h in hosts if h))

        async def ask(host: str) -> tuple[str, str] | None:
            try:
                match = _HOUSEHOLD.search(await self.status_page(host, 3))
            except SonosError:
                return None
            if not match:
                return None
            self._households[host] = match.group(1)
            return host, match.group(1)

        by_household: dict[str, str] = {}
        for answer in await asyncio.gather(*(ask(h) for h in unique)):
            if answer and answer[1] not in by_household:
                by_household[answer[1]] = answer[0]
        if not by_household:
            raise SonosError("no speaker could be reached")

        async def system(host: str) -> list[Speaker]:
            try:
                return await self.fetch_system(host)
            except SonosError:
                return []

        seen: set[str] = set()
        speakers: list[Speaker] = []
        for found in await asyncio.gather(*(system(h) for h in by_household.values())):
            for speaker in found:
                if speaker.id not in seen:
                    seen.add(speaker.id)
                    speakers.append(speaker)
        speakers.sort(key=lambda s: s.name.casefold())
        return next(iter(by_household.values())), speakers

    async def fetch_system(self, host: str) -> list[Speaker]:
        """Alle højttalere og grupper. Ét kald giver hele billedet."""
        household = await self.household_id(host)
        reply = await self._call(
            host,
            {"namespace": "groups:1", "command": "getGroups", "householdId": household},
            {"includeDeviceInfo": True},
        )
        group_for: dict[str, tuple[str, bool]] = {}
        for group in reply.get("groups") or []:
            playing = group.get("playbackState") in (
                "PLAYBACK_STATE_PLAYING",
                "PLAYBACK_STATE_BUFFERING",
            )
            for pid in group.get("playerIds") or []:
                group_for[pid] = (group.get("id"), playing)

        speakers = []
        for player in reply.get("players") or []:
            if not player.get("id") or not player.get("name"):
                continue
            player_host = host
            ws_url = player.get("websocketUrl")
            if isinstance(ws_url, str):
                player_host = URL(ws_url).host or host
            caps = player.get("capabilities") or []
            group = group_for.get(player["id"])
            speakers.append(
                Speaker(
                    id=player["id"],
                    name=player["name"],
                    host=player_host,
                    household_id=household,
                    supports_audio_clip="AUDIO_CLIP" in caps,
                    supports_playback="PLAYBACK" in caps,
                    group_id=group[0] if group else None,
                    is_playing=group[1] if group else False,
                )
            )
        return speakers

    def _header(self, namespace: str, command: str, speaker: Speaker) -> dict:
        # S1 vil have husstands-id'et med i hver kommando; S2 er ligeglad.
        header = {"namespace": namespace, "command": command, "playerId": speaker.id}
        if speaker.household_id:
            header["householdId"] = speaker.household_id
        return header

    async def volume(self, speaker: Speaker) -> int:
        reply = await self._call(speaker.host, self._header("playerVolume:1", "getVolume", speaker))
        if not isinstance(reply.get("volume"), int):
            raise SonosError("the speaker did not report its volume")
        return reply["volume"]

    async def set_volume(self, speaker: Speaker, volume: float) -> None:
        await self._call(
            speaker.host,
            self._header("playerVolume:1", "setVolume", speaker),
            {"volume": _clamp(volume)},
        )

    async def load_audio_clip(self, speaker: Speaker, stream_url: str, volume: float) -> None:
        """Afspiller beskeden oven på det der spiller.

        `volume` gælder kun beskeden. Derfor kan musikken skrues helt ned mens
        beskeden kommer ud i højttalerens normale lydstyrke.
        """
        await self._call(
            speaker.host,
            self._header("audioClip:1", "loadAudioClip", speaker),
            {
                "name": "Sonnounce",
                "appId": "dk.hoffmeyer-net.sonosmeddelelser",
                "streamUrl": stream_url,
                "volume": _clamp(volume),
                "priority": "HIGH",
            },
        )


def _clamp(value: float) -> int:
    return max(0, min(100, round(value)))
