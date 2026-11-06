"""Det appen ringer til.

Ligesom på Homey er der intet login. Står telefonen på husets net, henter den
en nøgle med `pair`, og den nøgle sendes med i headeren `X-Sonnounce-Key` på
alle andre kald — hjemme og ude, over hvilken fjernadgang man end har. Et
almindeligt Home Assistant-login (Bearer) virker også, så man kan bruge
kaldene fra sine egne scripts.

`pair` udleverer kun nøglen til en adresse på husets eget net og aldrig
gennem Home Assistant Cloud. Den der står på husets wifi, kan i forvejen
styre højttalerne direkte, så der bliver ikke givet mere væk end der står
åbent.

  GET    /api/sonnounce/hello              findes Sonnounce her
  GET    /api/sonnounce/pair               nøglen (kun fra husets net)
  GET    /api/sonnounce/info               version og adresserne ude og hjemme
  GET    /api/sonnounce/speakers           højttalerne der kan sendes til
  POST   /api/sonnounce/announce           send en besked (kroppen er lyden)
  GET    /api/sonnounce/archive            er "Gem beskeder" slået til, hvor mange
  POST   /api/sonnounce/archive            gem en kopi (kroppen er WAV-filen)
  GET    /api/sonnounce/archive/list       listen over gemte beskeder
  GET    /api/sonnounce/archive/file/<id>  én besked
  DELETE /api/sonnounce/archive/file/<id>  slet én besked

Indstillingerne til en afsendelse står i adressen (`?speakers=…&volume=…`), så
kroppen kan være selve lydfilen. Højttalernavnene er en JSON-liste.
"""

from __future__ import annotations

import ipaddress
import json
from http import HTTPStatus

from aiohttp import web
from yarl import URL

from homeassistant.components.http import KEY_AUTHENTICATED, KEY_HASS, HomeAssistantView
from homeassistant.helpers import instance_id
from homeassistant.helpers.network import NoURLAvailableError, get_url, is_cloud_connection

from .announcer import AnnounceError, Announcer
from .const import DOMAIN, MAX_BYTES, VERSION


def _announcer(request: web.Request) -> Announcer | None:
    return request.app[KEY_HASS].data.get(DOMAIN)


def _error(message: str, status: int = HTTPStatus.BAD_REQUEST) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _not_ready() -> web.Response:
    return _error("Sonnounce is not set up in Home Assistant yet", HTTPStatus.SERVICE_UNAVAILABLE)


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _names(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        names = json.loads(value)
    except ValueError:
        return [value]
    return [str(n) for n in names] if isinstance(names, list) else []


async def _body(request: web.Request) -> bytes:
    if request.content_length and request.content_length > MAX_BYTES:
        raise AnnounceError("the announcement is too large")
    return await request.read()


class SonnounceView(HomeAssistantView):
    """Kræver Sonnounce-nøglen eller et Home Assistant-login."""

    requires_auth = False

    @staticmethod
    def check(request: web.Request) -> web.Response | None:
        """`None` hvis kaldet må gå igennem, ellers svaret der skal gives."""
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        if request.get(KEY_AUTHENTICATED):
            return None
        if announcer.authorize(request.headers.get("X-Sonnounce-Key")):
            return None
        return _error("the key does not match", HTTPStatus.FORBIDDEN)


def _urls(hass) -> tuple[str | None, str | None]:
    def url(**kwargs) -> str | None:
        try:
            return get_url(hass, **kwargs)
        except NoURLAvailableError:
            return None

    return (
        url(allow_internal=False, prefer_cloud=True),
        url(allow_external=False, allow_cloud=False),
    )


def _is_home(request: web.Request) -> bool:
    try:
        address = ipaddress.ip_address(request.remote or "")
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_private or address.is_loopback or address.is_link_local


def _asked_locally(request: web.Request) -> bool:
    """Om telefonen ringede på en adresse der kun findes derhjemme.

    En dobbelt bund til `_is_home`. Står der en omvendt proxy på samme maskine
    (fx en tunnel til internettet), kan et kald udefra se ud som om det kom fra
    maskinen selv. Men det kom ind på det offentlige navn — en IP-adresse på
    husets net, et .local-navn eller den interne adresse er det kun hjemme.
    """
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        internal = request.app[KEY_HASS].config.internal_url
        return bool(internal) and URL(internal).host == host
    return address.is_private or address.is_loopback or address.is_link_local


class PairView(HomeAssistantView):
    """Udleverer nøglen til en telefon på husets net. Det er hele parringen."""

    url = "/api/sonnounce/pair"
    name = "api:sonnounce:pair"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        if is_cloud_connection(hass) or not _is_home(request) or not _asked_locally(request):
            return _error("pairing is only possible from home", HTTPStatus.FORBIDDEN)
        external, internal = _urls(hass)
        return web.json_response(
            {
                "key": announcer.key,
                "uuid": await instance_id.async_get(hass),
                "name": hass.config.location_name,
                "externalUrl": external,
                "internalUrl": internal,
            },
            headers={"Cache-Control": "no-store"},
        )


class HelloView(HomeAssistantView):
    """Uden login, så appen kan se om Sonnounce er installeret før man logger ind."""

    url = "/api/sonnounce/hello"
    name = "api:sonnounce:hello"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        return web.json_response(
            {
                "app": "sonnounce",
                "version": VERSION,
                "ready": DOMAIN in hass.data,
                "uuid": await instance_id.async_get(hass),
            }
        )


class InfoView(SonnounceView):
    url = "/api/sonnounce/info"
    name = "api:sonnounce:info"

    async def get(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        hass = request.app[KEY_HASS]
        external, internal = _urls(hass)
        return web.json_response(
            {
                "version": VERSION,
                "uuid": await instance_id.async_get(hass),
                "name": hass.config.location_name,
                "ready": DOMAIN in hass.data,
                # Adressen udefra: Home Assistant Cloud, eller den man selv har
                # skrevet ind. Mangler den, kan appen kun bruges hjemme.
                "externalUrl": external,
                "internalUrl": internal,
            }
        )


class SpeakersView(SonnounceView):
    url = "/api/sonnounce/speakers"
    name = "api:sonnounce:speakers"

    async def get(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        try:
            return web.json_response(await announcer.list_speakers())
        except AnnounceError as err:
            return _error(str(err), HTTPStatus.BAD_GATEWAY)


class AnnounceView(SonnounceView):
    url = "/api/sonnounce/announce"
    name = "api:sonnounce:announce"

    async def post(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        q = request.query
        try:
            audio = await _body(request)
            music_volume = _int(q.get("musicVolume"))
            result = await announcer.announce(
                audio,
                content_type=request.content_type or "audio/wav",
                wanted=_names(q.get("speakers")),
                volume=_int(q.get("volume")),
                music_volume=music_volume if music_volume is not None else 2,
                duck=q.get("duck", "true") != "false",
                duration=_float(q.get("durationSeconds")),
                host=q.get("host"),
                # Appen siger nej, når beskeden allerede gemmes et andet sted.
                archive=q.get("archive", "true") != "false",
            )
        except AnnounceError as err:
            return _error(str(err), HTTPStatus.BAD_GATEWAY)
        return web.json_response(result.as_dict())


class ArchiveView(SonnounceView):
    url = "/api/sonnounce/archive"
    name = "api:sonnounce:archive"

    async def get(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        return web.json_response(await announcer.archive.stats())

    async def post(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        """En kopi fra telefonen af en besked den selv sendte hjemme."""
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        if not announcer.archive.enabled:
            return web.json_response({"saved": False})
        try:
            data = await _body(request)
            saved = await announcer.archive.add(
                data, _names(request.query.get("speakers")), _float(request.query.get("seconds"))
            )
        except (AnnounceError, ValueError) as err:
            return _error(str(err))
        return web.json_response({"saved": saved})


class ArchiveListView(SonnounceView):
    url = "/api/sonnounce/archive/list"
    name = "api:sonnounce:archive:list"

    async def get(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        return web.json_response(await announcer.archive.list())


class ArchiveFileView(SonnounceView):
    url = "/api/sonnounce/archive/file/{entry_id}"
    name = "api:sonnounce:archive:file"

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        if denied := self.check(request):
            return denied
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        try:
            data = await announcer.archive.read(entry_id)
        except (ValueError, FileNotFoundError):
            return _error("not found", HTTPStatus.NOT_FOUND)
        return web.Response(body=data, content_type="audio/wav", headers={"Cache-Control": "no-store"})

    async def delete(self, request: web.Request, entry_id: str) -> web.Response:
        if denied := self.check(request):
            return denied
        announcer = _announcer(request)
        if not announcer:
            return _not_ready()
        try:
            await announcer.archive.remove(entry_id)
        except ValueError:
            return _error("not found", HTTPStatus.NOT_FOUND)
        return web.json_response({"deleted": True})


ALL_VIEWS = (
    HelloView,
    PairView,
    InfoView,
    SpeakersView,
    AnnounceView,
    ArchiveView,
    ArchiveListView,
    ArchiveFileView,
)
