"""Taler med en Sonos-højttaler over UPnP på port 1400.

Den gamle vej, og den eneste S1-højttalere og de ældste S2-modeller har til at
afspille noget de ikke selv har fundet. Se `legacy.py`. Samme kode som
`SonosUPnP.js` i Homey-appen og `SonosUPnPClient.swift` i iPhone-appen.
"""

from __future__ import annotations

import html
import re

import aiohttp

from .sonos_local import SonosError

AV = ("/MediaRenderer/AVTransport/Control", "urn:schemas-upnp-org:service:AVTransport:1")
RC = ("/MediaRenderer/RenderingControl/Control", "urn:schemas-upnp-org:service:RenderingControl:1")
CD = ("/MediaServer/ContentDirectory/Control", "urn:schemas-upnp-org:service:ContentDirectory:1")

_FIELD = re.compile(r"<([A-Za-z][A-Za-z0-9:]*)>([^<]*)</\1>")
_RES = re.compile(r"<res[^>]*>([^<]*)</res>")


def _fields(xml: str) -> dict[str, str]:
    """Felterne i et SOAP-svar er flade: `<Navn>værdi</Navn>`."""
    return {m.group(1): html.unescape(m.group(2)) for m in _FIELD.finditer(xml)}


def seconds(value: str | None) -> int | None:
    """"0:03:21" → 201."""
    parts = str(value or "").split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


class SonosUPnP:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def call(
        self,
        host: str,
        service: tuple[str, str],
        action: str,
        args: list[tuple[str, str]] | None = None,
        *,
        instance: bool = True,
        timeout: float = 4,
    ) -> dict[str, str]:
        path, urn = service
        inner = "<InstanceID>0</InstanceID>" if instance else ""
        for key, value in args or []:
            inner += f"<{key}>{html.escape(str(value))}</{key}>"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{urn}">{inner}</u:{action}>'
            "</s:Body></s:Envelope>"
        )
        try:
            async with self._session.post(
                f"http://{host}:1400{path}",
                data=body.encode(),
                headers={
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPACTION": f'"{urn}#{action}"',
                },
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                xml = await resp.text()
                if resp.status != 200:
                    raise SonosError(f"UPnP {action} {_fields(xml).get('errorCode') or resp.status}")
                return _fields(xml)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SonosError(f"{host} did not answer {action}") from err

    async def media_info(self, host: str) -> dict:
        r = await self.call(host, AV, "GetMediaInfo")
        return {"uri": r.get("CurrentURI", ""), "metadata": r.get("CurrentURIMetaData", "")}

    async def position_info(self, host: str) -> dict:
        r = await self.call(host, AV, "GetPositionInfo")
        try:
            track = int(r.get("Track") or 0)
        except ValueError:
            track = 0
        return {
            "track": track,
            "rel_time": r.get("RelTime", ""),
            "track_uri": r.get("TrackURI", ""),
            "duration": r.get("TrackDuration", ""),
        }

    async def transport_state(self, host: str) -> str:
        return (await self.call(host, AV, "GetTransportInfo")).get("CurrentTransportState", "")

    async def play_mode(self, host: str) -> str:
        return (await self.call(host, AV, "GetTransportSettings")).get("PlayMode", "NORMAL")

    async def volume(self, host: str) -> int:
        value = (await self.call(host, RC, "GetVolume", [("Channel", "Master")])).get("CurrentVolume")
        try:
            return int(value)
        except (TypeError, ValueError) as err:
            raise SonosError("the speaker did not report its volume") from err

    async def is_muted(self, host: str) -> bool:
        return (await self.call(host, RC, "GetMute", [("Channel", "Master")])).get("CurrentMute") == "1"

    async def set_uri(self, host: str, uri: str, metadata: str) -> None:
        await self.call(host, AV, "SetAVTransportURI", [("CurrentURI", uri), ("CurrentURIMetaData", metadata)])

    async def play(self, host: str) -> None:
        await self.call(host, AV, "Play", [("Speed", "1")])

    async def pause(self, host: str) -> None:
        await self.call(host, AV, "Pause")

    async def stop(self, host: str) -> None:
        await self.call(host, AV, "Stop")

    async def seek(self, host: str, unit: str, target: str) -> None:
        await self.call(host, AV, "Seek", [("Unit", unit), ("Target", target)])

    async def set_play_mode(self, host: str, mode: str) -> None:
        await self.call(host, AV, "SetPlayMode", [("NewPlayMode", mode)])

    async def leave_group(self, host: str) -> None:
        await self.call(host, AV, "BecomeCoordinatorOfStandaloneGroup")

    async def set_volume(self, host: str, volume: float) -> None:
        level = max(0, min(100, round(volume)))
        await self.call(host, RC, "SetVolume", [("Channel", "Master"), ("DesiredVolume", str(level))])

    async def set_mute(self, host: str, muted: bool) -> None:
        await self.call(host, RC, "SetMute", [("Channel", "Master"), ("DesiredMute", "1" if muted else "0")])

    async def add_to_queue(self, host: str, uri: str, metadata: str, position: int = 0) -> int:
        """Lægger en adresse i køen. `position` 0 er til sidst."""
        r = await self.call(
            host,
            AV,
            "AddURIToQueue",
            [
                ("EnqueuedURI", uri),
                ("EnqueuedURIMetaData", metadata),
                ("DesiredFirstTrackNumberEnqueued", str(position)),
                ("EnqueueAsNext", "0"),
            ],
            timeout=15,
        )
        try:
            return int(r.get("FirstTrackNumberEnqueued") or 0)
        except ValueError:
            return 0

    async def remove_from_queue(self, host: str, track: int) -> None:
        await self.call(host, AV, "RemoveTrackFromQueue", [("ObjectID", f"Q:0/{track}"), ("UpdateID", "0")])

    async def queue_uris(self, host: str) -> list[str]:
        found_all: list[str] = []
        while True:
            r = await self.call(
                host,
                CD,
                "Browse",
                [
                    ("ObjectID", "Q:0"),
                    ("BrowseFlag", "BrowseDirectChildren"),
                    ("Filter", "*"),
                    ("StartingIndex", str(len(found_all))),
                    ("RequestedCount", "500"),
                    ("SortCriteria", ""),
                ],
                instance=False,
            )
            found = [html.unescape(m.group(1)) for m in _RES.finditer(r.get("Result", ""))]
            found_all.extend(found)
            try:
                total = int(r.get("TotalMatches") or len(found_all))
            except ValueError:
                total = len(found_all)
            if not found or len(found_all) >= total:
                return found_all
