"""De gemte beskeder.

Slået fra som udgangspunkt. Slår man "Gem beskeder" til under integrationens
indstillinger, bliver hver besked der går ud lagt her. Det gælder beskeder der
går gennem Home Assistant hjemmefra, og dem telefonen sender direkte hjemme —
for dem sender telefonen en kopi bagefter.

Beskederne forlader ikke huset. De ligger i Home Assistants konfigurationsmappe
(og kommer derfor med i Home Assistants egne sikkerhedskopier), til man henter
dem fra telefonen eller Mac'en — så slettes de herfra — eller til de bliver for
gamle og ryddes væk af sig selv.

Samme opbygning som `Archive.js` i Homey-appen: hver besked er en WAV-fil og
en lille JSON-beskrivelse ved siden af.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from homeassistant.core import HomeAssistant

MAX_TOTAL_BYTES = 1024 * 1024 * 1024
ID_PATTERN = re.compile(r"^[0-9]{13}-[a-f0-9]{8}$")


def is_wav(data: bytes) -> bool:
    return len(data) > 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


class Archive:
    def __init__(self, hass: HomeAssistant, enabled: bool, keep_days: int) -> None:
        self.hass = hass
        self.dir = Path(hass.config.path("sonnounce", "archive"))
        self.enabled = enabled
        self.keep_days = keep_days

    async def _run(self, func, *args):
        return await self.hass.async_add_executor_job(func, *args)

    async def init(self) -> None:
        await self._run(lambda: self.dir.mkdir(parents=True, exist_ok=True))
        await self.prune()

    async def add(self, data: bytes, speakers: list[str], seconds: float | None) -> bool:
        if not self.enabled:
            return False
        if not is_wav(data):
            raise ValueError("only WAV announcements can be saved")
        entry_id = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        meta = {
            "id": entry_id,
            "date": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "speakers": [str(name)[:100] for name in speakers][:50],
            "seconds": round(seconds) if isinstance(seconds, (int, float)) else None,
            "bytes": len(data),
        }

        def write() -> None:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{entry_id}.wav").write_bytes(data)
            (self.dir / f"{entry_id}.json").write_text(json.dumps(meta), encoding="utf-8")

        await self._run(write)
        await self.prune()
        return True

    def _list(self) -> list[dict]:
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.endswith(".json"))
        except FileNotFoundError:
            return []
        entries = []
        for name in names:
            try:
                meta = json.loads((self.dir / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # Halvt skrevet; oprydningen tager den.
            if ID_PATTERN.match(str(meta.get("id", ""))):
                entries.append(meta)
        return entries

    async def list(self) -> list[dict]:
        return await self._run(self._list)

    async def stats(self) -> dict:
        entries = await self.list()
        return {
            "enabled": self.enabled,
            "keepDays": self.keep_days,
            "count": len(entries),
            "bytes": sum(e.get("bytes") or 0 for e in entries),
        }

    async def read(self, entry_id: str) -> bytes:
        if not ID_PATTERN.match(entry_id):
            raise ValueError("invalid id")
        return await self._run((self.dir / f"{entry_id}.wav").read_bytes)

    def _remove(self, entry_id: str) -> None:
        for ext in ("wav", "json"):
            try:
                (self.dir / f"{entry_id}.{ext}").unlink()
            except FileNotFoundError:
                pass

    async def remove(self, entry_id: str) -> None:
        if not ID_PATTERN.match(entry_id):
            raise ValueError("invalid id")
        await self._run(self._remove, entry_id)

    async def clear(self) -> None:
        def clear() -> None:
            try:
                for name in os.listdir(self.dir):
                    (self.dir / name).unlink(missing_ok=True)
            except FileNotFoundError:
                pass

        await self._run(clear)

    async def prune(self) -> None:
        """Rydder beskeder væk der er for gamle, og de ældste hvis der er for mange."""

        def prune() -> None:
            try:
                names = os.listdir(self.dir)
            except FileNotFoundError:
                return
            ids = {re.sub(r"\.(wav|json)$", "", n) for n in names}
            now = time.time() * 1000
            for entry_id in ids:
                has_wav = f"{entry_id}.wav" in names
                has_json = f"{entry_id}.json" in names
                valid = bool(ID_PATTERN.match(entry_id))
                if valid and has_wav == has_json:
                    continue
                # En fil der er ved at blive skrevet, får et minut.
                if valid and now - int(entry_id.split("-")[0]) < 60_000:
                    continue
                for name in names:
                    if name.startswith(f"{entry_id}."):
                        (self.dir / name).unlink(missing_ok=True)

            entries = self._list()
            cutoff = now - self.keep_days * 86_400_000 if self.keep_days > 0 else None
            total = sum(e.get("bytes") or 0 for e in entries)
            for entry in entries:
                stamp = int(entry["id"].split("-")[0])
                too_old = cutoff is not None and stamp < cutoff
                if not too_old and total <= MAX_TOTAL_BYTES:
                    continue
                self._remove(entry["id"])
                total -= entry.get("bytes") or 0

        await self._run(prune)
