from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import logging
import os
import secrets
import socket
import ssl
import threading
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
INDEX_PATH = APP_DIR / "static" / "index.html"
LOGIN_PATH = APP_DIR / "static" / "login.html"
DOORS_IMAGE_PATH = APP_DIR / "images.png"
SESSION_COOKIE = "site_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7
PUBLIC_PATHS = {"/login", "/health"}
FALLBACK_LATITUDE = 32.0841
FALLBACK_LONGITUDE = 34.8878
FALLBACK_PLACE = "Petah Tikva"
WEATHER_MAX_AGE_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 12
STATUS_POLL_SECONDS = 30

load_dotenv(APP_DIR / ".env")
logger = logging.getLogger(__name__)

app = FastAPI(title="Lock reminder", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
PASSWORD_HASH = hashlib.sha256(os.environ.pop("SITE_PASSWORD", "").encode("utf-8")).digest()
SESSION_FERNET = Fernet(
    base64.urlsafe_b64encode(hashlib.sha256(os.environ.get("SESSION_SECRET", "").encode("utf-8")).digest())
)


@dataclass
class Place:
    name: str
    latitude: float
    longitude: float


@dataclass
class WeatherView:
    status: str
    scene: str
    temperature_c: float | None
    place: str


@dataclass
class PageSnapshot:
    mode: str
    unix_time: int
    timezone_name: str
    weather: WeatherView | None


class PageController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = "idle"
        self._place: Place | None = None
        self._weather: WeatherView | None = None
        self._weather_fetched_at: datetime | None = None
        self._listeners: list[asyncio.Queue[str]] = []

    def add_listener(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            self._listeners.append(queue)

    def remove_listener(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            if queue in self._listeners:
                self._listeners.remove(queue)

    def apply_database_status(self, raw_value: str | None) -> None:
        mode = mode_for_status(raw_value)
        with self._lock:
            changed = self._mode != mode
            self._mode = mode
        if changed:
            logger.info("Page mode set to %s", mode)
            self.publish()

    def publish(self) -> None:
        payload = json.dumps(snapshot_payload(self.snapshot()))
        with self._lock:
            listeners = list(self._listeners)
        for queue in listeners:
            queue.put_nowait(payload)

    def snapshot(self) -> PageSnapshot:
        now = clock_now()
        with self._lock:
            mode = self._mode
        weather = self._weather_for_mode(mode)
        return PageSnapshot(
            mode=mode,
            unix_time=int(now.timestamp()),
            timezone_name=clock_zone_name(),
            weather=weather,
        )

    def _weather_for_mode(self, mode: str) -> WeatherView | None:
        if mode != "automate":
            return None
        with self._lock:
            cached = self._weather
            fetched_at = self._weather_fetched_at
        if cached is not None and fetched_at is not None and not weather_unavailable(cached):
            age_seconds = (datetime.now() - fetched_at).total_seconds()
            if age_seconds < WEATHER_MAX_AGE_SECONDS:
                return cached
        fresh = load_weather(self._current_place())
        if weather_unavailable(fresh):
            return fresh
        with self._lock:
            self._weather = fresh
            self._weather_fetched_at = datetime.now()
        return fresh

    def _current_place(self) -> Place:
        with self._lock:
            if self._place is not None:
                return self._place
        place = load_place()
        with self._lock:
            self._place = place
        return place


controller = PageController()


def require_configured_secrets() -> None:
    empty_hash = hashlib.sha256(b"").digest()
    if PASSWORD_HASH == empty_hash:
        raise RuntimeError("Set SITE_PASSWORD in the env file before starting.")
    if not os.environ.get("SESSION_SECRET", "").strip():
        raise RuntimeError("Set SESSION_SECRET in the env file before starting.")
    database_url()


def password_matches(given: str) -> bool:
    given_hash = hashlib.sha256(given.encode("utf-8")).digest()
    return secrets.compare_digest(given_hash, PASSWORD_HASH)


def issue_session_token() -> str:
    return SESSION_FERNET.encrypt(b"ok").decode("ascii")


def session_is_valid(token: str) -> bool:
    try:
        payload = SESSION_FERNET.decrypt(token.encode("ascii"), ttl=SESSION_TTL_SECONDS)
    except (InvalidToken, ValueError, UnicodeError):
        return False
    return payload == b"ok"


def cookie_is_secure(request: Request) -> bool:
    forced = os.environ.get("COOKIE_SECURE", "").strip().lower()
    if forced in {"1", "true", "yes"}:
        return True
    if forced in {"0", "false", "no"}:
        return False
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


def session_request(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    return bool(token) and session_is_valid(token)


def login_markup(failed: bool) -> str:
    page = LOGIN_PATH.read_text(encoding="utf-8")
    message = "Wrong password." if failed else ""
    return page.replace("<!-- message -->", message)


def clock_zone_name() -> str:
    return os.environ.get("CLOCK_TIMEZONE", "Asia/Jerusalem").strip() or "Asia/Jerusalem"


def clock_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(clock_zone_name()))
    except ZoneInfoNotFoundError:
        return datetime.now()


def load_place() -> Place:
    configured = place_from_env()
    if configured is not None:
        return configured
    return Place(FALLBACK_PLACE, FALLBACK_LATITUDE, FALLBACK_LONGITUDE)


def place_from_env() -> Place | None:
    latitude_text = os.environ.get("WEATHER_LATITUDE", "").strip()
    longitude_text = os.environ.get("WEATHER_LONGITUDE", "").strip()
    if not latitude_text or not longitude_text:
        return None
    try:
        latitude = float(latitude_text)
        longitude = float(longitude_text)
    except ValueError:
        return None
    place_name = os.environ.get("WEATHER_PLACE", "").strip() or FALLBACK_PLACE
    return Place(place_name, latitude, longitude)


def weather_unavailable(view: WeatherView) -> bool:
    return view.status == "Weather unavailable"


def whole_number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def load_weather(place: Place) -> WeatherView:
    query = urlencode(
        {
            "latitude": place.latitude,
            "longitude": place.longitude,
            "current": "temperature_2m,weather_code,is_day",
            "timezone": "auto",
        }
    )
    try:
        payload = fetch_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    except (OSError, ValueError, TimeoutError) as error:
        logger.warning("Weather request failed: %s", type(error).__name__)
        return WeatherView("Weather unavailable", "cloudy", None, place.name)
    current = payload.get("current")
    if not isinstance(current, dict):
        return WeatherView("Weather unavailable", "cloudy", None, place.name)
    code = whole_number(current.get("weather_code"))
    is_day_value = whole_number(current.get("is_day"))
    temperature = current.get("temperature_2m")
    if code is None or is_day_value is None:
        return WeatherView("Weather unavailable", "cloudy", None, place.name)
    status, scene = describe_weather(code, is_day_value == 1)
    temperature_c = float(temperature) if isinstance(temperature, (int, float)) else None
    return WeatherView(status, scene, temperature_c, place.name)


def describe_weather(code: int, is_day: bool) -> tuple[str, str]:
    if code == 0:
        return ("Clear", "clear-day" if is_day else "clear-night")
    if code in (1, 2):
        return ("Partly cloudy", "partly-cloudy" if is_day else "cloudy-night")
    if code == 3:
        return ("Overcast", "cloudy")
    if code in (45, 48):
        return ("Fog", "fog")
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return ("Rain", "rain")
    if code in (71, 73, 75, 77, 85, 86):
        return ("Snow", "snow")
    if code in (95, 96, 99):
        return ("Thunderstorm", "storm")
    return ("Cloudy", "cloudy")


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            raise OSError("No IPv4 address")
        sock = socket.create_connection(infos[0][4], self.timeout)
        context = self._context if isinstance(self._context, ssl.SSLContext) else ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


class IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(IPv4HTTPSConnection, request, context=self._context)


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "lock-reminder", "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(IPv4HTTPSHandler())
    with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw_body = response.read().decode("utf-8")
    parsed = json.loads(raw_body)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object")
    return parsed


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Set DATABASE_URL in the env file before starting.")
    return url


def mode_for_status(raw_value: str | None) -> str:
    if raw_value is None:
        return "idle"
    command = raw_value.strip().lower()
    if command == "automate":
        return "automate"
    if command == "mellon":
        return "mellon"
    return "idle"


def read_database_status() -> str | None:
    with psycopg.connect(database_url(), connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM state LIMIT 1")
            row = cursor.fetchone()
    if row is None or not isinstance(row[0], str):
        return None
    return row[0]


async def refresh_page_from_database() -> None:
    try:
        status = await asyncio.to_thread(read_database_status)
    except (psycopg.Error, OSError):
        logger.warning("Could not read the status")
        return
    controller.apply_database_status(status)


async def watch_database_status(stop: asyncio.Event) -> None:
    while not stop.is_set():
        await refresh_page_from_database()
        try:
            await asyncio.wait_for(stop.wait(), STATUS_POLL_SECONDS)
        except TimeoutError:
            continue


@asynccontextmanager
async def app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    stop = asyncio.Event()
    watcher = asyncio.create_task(watch_database_status(stop))
    try:
        yield
    finally:
        stop.set()
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


app.router.lifespan_context = app_lifespan


def snapshot_payload(snapshot: PageSnapshot) -> dict[str, Any]:
    weather_payload: dict[str, Any] | None = None
    if snapshot.weather is not None:
        weather_payload = {
            "status": snapshot.weather.status,
            "scene": snapshot.weather.scene,
            "temperature_c": snapshot.weather.temperature_c,
            "place": snapshot.weather.place,
        }
    return {
        "mode": snapshot.mode,
        "unix": snapshot.unix_time,
        "timezone": snapshot.timezone_name,
        "weather": weather_payload,
    }


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse(login_markup(False))


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    raw_body = (await request.body()).decode("utf-8")
    supplied = parse_qs(raw_body).get("password", [""])[0]
    if not password_matches(supplied):
        return HTMLResponse(login_markup(True), status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=cookie_is_secure(request),
        samesite="lax",
        path="/",
    )
    return response


@app.get("/")
async def main_page() -> HTMLResponse:
    await refresh_page_from_database()
    page = INDEX_PATH.read_text(encoding="utf-8")
    payload = json.dumps(snapshot_payload(controller.snapshot())).replace("<", "\\u003c")
    return HTMLResponse(page.replace("/*STATE*/null", payload))


@app.get("/state")
async def page_state() -> JSONResponse:
    await refresh_page_from_database()
    return JSONResponse(snapshot_payload(controller.snapshot()))


@app.get("/updates")
async def page_updates(request: Request) -> StreamingResponse:
    queue: asyncio.Queue[str] = asyncio.Queue()
    controller.add_listener(queue)

    async def events() -> AsyncIterator[str]:
        try:
            yield "retry: 600000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=60)
                except TimeoutError:
                    continue
                yield "data: " + payload + "\n\n"
        finally:
            controller.remove_listener(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/media/doors")
def doors_image() -> FileResponse:
    if not DOORS_IMAGE_PATH.is_file():
        raise HTTPException(status_code=404, detail="Image is missing")
    media_type = "image/png" if DOORS_IMAGE_PATH.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(DOORS_IMAGE_PATH, media_type=media_type)


@app.middleware("http")
async def require_password(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if session_request(request):
        return await call_next(request)
    if request.method == "GET" and request.url.path == "/":
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


require_configured_secrets()
