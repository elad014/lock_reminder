from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEB_DIR.parent
INDEX_PATH = WEB_DIR / "static" / "index.html"
LOGIN_PATH = WEB_DIR / "static" / "login.html"
DOORS_IMAGE_PATH = PROJECT_DIR / "images.png"
SESSION_COOKIE = "site_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7
PUBLIC_PATHS = {"/login", "/health"}
DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}
FALLBACK_LATITUDE = 32.0841
FALLBACK_LONGITUDE = 34.8878
FALLBACK_PLACE = "Petah Tikva"
WEATHER_MAX_AGE_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 4

load_dotenv(PROJECT_DIR / ".env")
load_dotenv(WEB_DIR / ".env")

app = FastAPI(title="Lock reminder", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
PASSWORD_HASH = hashlib.sha256(os.environ.pop("SITE_PASSWORD", "").encode("utf-8")).digest()
API_KEY = hashlib.sha256(os.environ.pop("API_CODE", "").encode("utf-8")).digest()
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
    time_text: str
    date_text: str
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

    def apply_command(self, raw_value: str) -> str:
        command = raw_value.strip().lower()
        if command == "automate":
            mode = "automate"
        elif command == "mellon":
            mode = "mellon"
        else:
            raise HTTPException(status_code=400, detail="data must be Automate or mellon")
        with self._lock:
            self._mode = mode
        self.publish()
        return mode

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
            time_text=now.strftime("%H:%M:%S"),
            date_text=now.strftime("%A, %d %B %Y"),
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
        if cached is not None and fetched_at is not None:
            age_seconds = (datetime.now() - fetched_at).total_seconds()
            if age_seconds < WEATHER_MAX_AGE_SECONDS:
                return cached
        fresh = load_weather(self._current_place())
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
    if API_KEY == empty_hash:
        raise RuntimeError("Set API_CODE in the env file before starting.")
    if not os.environ.get("SESSION_SECRET", "").strip():
        raise RuntimeError("Set SESSION_SECRET in the env file before starting.")


def password_matches(given: str) -> bool:
    given_hash = hashlib.sha256(given.encode("utf-8")).digest()
    return secrets.compare_digest(given_hash, PASSWORD_HASH)


def api_code_matches(given: str) -> bool:
    if not given or API_KEY == hashlib.sha256(b"").digest():
        return False
    given_hash = hashlib.sha256(given.encode("utf-8")).digest()
    return secrets.compare_digest(given_hash, API_KEY)


def basic_secret(header: str) -> str | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
    if ":" not in decoded:
        return None
    return decoded.split(":", 1)[1]


def api_code_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        code = header[7:].strip()
        return code if api_code_matches(code) else None
    secret = basic_secret(header)
    if secret is not None and api_code_matches(secret):
        return secret
    custom = request.headers.get("x-api-code", "").strip()
    if custom and api_code_matches(custom):
        return custom
    return None


def encrypt_bytes(plain: bytes) -> dict[str, str]:
    nonce = os.urandom(12)
    ciphertext = AESGCM(API_KEY).encrypt(nonce, plain, None)
    return {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_envelope(payload: dict[str, Any]) -> bytes:
    nonce_text = payload.get("nonce")
    ciphertext_text = payload.get("ciphertext")
    if not isinstance(nonce_text, str) or not isinstance(ciphertext_text, str):
        raise HTTPException(status_code=400, detail="Encrypted request must include nonce and ciphertext")
    try:
        nonce = base64.b64decode(nonce_text, validate=True)
        ciphertext = base64.b64decode(ciphertext_text, validate=True)
        return AESGCM(API_KEY).decrypt(nonce, ciphertext, None)
    except (InvalidTag, ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="Could not decrypt the request")


def json_response(request: Request, payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    if request.headers.get("x-encrypted") == "1":
        envelope = encrypt_bytes(json.dumps(payload).encode("utf-8"))
        return JSONResponse(envelope, status_code=status_code, headers={"X-Encrypted": "1"})
    return JSONResponse(payload, status_code=status_code)


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


def request_is_authorized(request: Request) -> bool:
    api_paths = {"/event", "/state"}
    if request.url.path in api_paths and api_code_from_request(request) is not None:
        return True
    supplied = request.headers.get("x-site-password", "")
    if supplied and password_matches(supplied):
        return True
    return session_request(request)


def session_request(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    return bool(token) and session_is_valid(token)


def require_encrypted_api(request: Request) -> None:
    api_call = api_code_from_request(request) is not None and not session_request(request)
    if api_call and request.headers.get("x-encrypted") != "1":
        raise HTTPException(status_code=400, detail="API requests must be encrypted")


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
    except (OSError, ValueError, TimeoutError):
        return WeatherView("Weather unavailable", "cloudy", None, place.name)
    current = payload.get("current")
    if not isinstance(current, dict):
        return WeatherView("Weather unavailable", "cloudy", None, place.name)
    code = current.get("weather_code")
    is_day_value = current.get("is_day")
    temperature = current.get("temperature_2m")
    if not isinstance(code, int) or not isinstance(is_day_value, int):
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


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "lock-reminder"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw_body = response.read().decode("utf-8")
    parsed = json.loads(raw_body)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object")
    return parsed


def read_command(request_data: dict[str, Any], query_data: str | None) -> str:
    if query_data:
        return query_data
    for key in ("data", "command", "text"):
        value = request_data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise HTTPException(status_code=400, detail="data must be Automate or mellon")


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
        "time": snapshot.time_text,
        "date": snapshot.date_text,
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
def main_page() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.get("/docs", response_class=HTMLResponse)
def swagger_page() -> HTMLResponse:
    html = (WEB_DIR / "static" / "swagger.html").read_text(encoding="utf-8")
    spec = json.dumps(custom_openapi()).replace("<", "\\u003c")
    return HTMLResponse(html.replace("/*OPENAPI*/null", spec))


@app.get("/state")
def page_state(request: Request) -> JSONResponse:
    require_encrypted_api(request)
    return json_response(request, snapshot_payload(controller.snapshot()))


@app.get("/updates")
async def page_updates(request: Request) -> StreamingResponse:
    require_encrypted_api(request)
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


@app.get("/event", operation_id="read_event")
async def read_event(request: Request) -> JSONResponse:
    return await receive_event(request)


@app.post("/event", operation_id="send_event")
async def send_event(request: Request) -> JSONResponse:
    return await receive_event(request)


async def receive_event(request: Request) -> JSONResponse:
    encrypted = request.headers.get("x-encrypted") == "1"
    require_encrypted_api(request)
    query_data = None if api_code_from_request(request) is not None else request.query_params.get("data")
    body: dict[str, Any] = {}
    if request.method == "POST":
        raw_body = (await request.body()).decode("utf-8").strip()
        if encrypted:
            try:
                parsed_envelope = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Encrypted request must be JSON")
            if not isinstance(parsed_envelope, dict):
                raise HTTPException(status_code=400, detail="Encrypted request must be a JSON object")
            plain = decrypt_envelope(parsed_envelope).decode("utf-8").strip()
            try:
                parsed = json.loads(plain)
            except json.JSONDecodeError:
                body = {"data": plain}
            else:
                if isinstance(parsed, dict):
                    body = parsed
                elif isinstance(parsed, str):
                    body = {"data": parsed}
            query_data = None
        elif raw_body:
            try:
                parsed = json.loads(raw_body)
            except json.JSONDecodeError:
                body = {"data": raw_body}
            else:
                if isinstance(parsed, dict):
                    body = parsed
                elif isinstance(parsed, str):
                    body = {"data": parsed}
    mode = controller.apply_command(read_command(body, query_data))
    return json_response(request, {"mode": mode})


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
    if request.url.path in DOCS_PATHS:
        if api_code_from_request(request) is None:
            return Response(
                status_code=401,
                content="Swagger code required",
                media_type="text/plain",
                headers={"WWW-Authenticate": 'Basic realm="Swagger code"'},
            )
        return await call_next(request)
    if request_is_authorized(request):
        return await call_next(request)
    if request.method == "GET" and request.url.path == "/":
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version="1.0.0", routes=app.routes)
    components = schema.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes["ApiCode"] = {"type": "http", "scheme": "bearer", "description": "API code from the env file"}
    for path, methods in schema.get("paths", {}).items():
        if path in PUBLIC_PATHS or path in DOCS_PATHS:
            continue
        for operation in methods.values():
            if isinstance(operation, dict):
                operation["security"] = [{"ApiCode": []}]
    event_post = schema.get("paths", {}).get("/event", {}).get("post")
    if isinstance(event_post, dict):
        event_post["operationId"] = "send_event"
    if isinstance(event_post, dict):
        event_post["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["data"],
                        "properties": {
                            "data": {
                                "type": "string",
                                "enum": ["Automate", "mellon"],
                                "example": "Automate",
                            },
                        },
                    }
                }
            },
        }
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


@app.exception_handler(HTTPException)
async def encrypted_http_error(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    if request.headers.get("x-encrypted") == "1":
        return json_response(request, {"detail": detail}, exc.status_code)
    return JSONResponse({"detail": detail}, status_code=exc.status_code)


require_configured_secrets()
