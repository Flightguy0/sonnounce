"""Faste værdier for Sonnounce."""

from __future__ import annotations

DOMAIN = "sonnounce"
VERSION = "2.2.1"

# Indstillinger, sat under Konfigurer på integrationen.
CONF_ARCHIVE_ENABLED = "archive_enabled"
CONF_ARCHIVE_DAYS = "archive_days"
CONF_SPEAKER_HOST = "speaker_host"
# Ikke en indstilling, men en knap: laver en ny nøgle, så alle telefoner
# skal tilføje Home Assistant igen.
CONF_RESET_KEY = "reset_key"

DEFAULT_ARCHIVE_DAYS = 30

# Hændelser automatiseringer kan bygge på — Home Assistants svar på Homeys
# flow-kort.
EVENT_PLAYED = "sonnounce_played"
EVENT_FAILED = "sonnounce_failed"
EVENT_SPEAKERS_LOST = "sonnounce_speakers_lost"
EVENT_SPEAKERS_FOUND = "sonnounce_speakers_found"

# Fem minutter lyd i 22 kHz, 16 bit mono er omkring 13 MB. Home Assistant
# tager selv imod op til 16 MB i ét kald.
MAX_BYTES = 16 * 1024 * 1024

# Stien højttalerne henter beskeden fra. Den skal kunne nås uden login, for
# en højttaler har ikke noget login; id'et er tilfældigt og lever kun to
# minutter.
CLIP_PATH = "/api/sonnounce/clip"
