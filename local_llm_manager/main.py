"""FastAPI application: REST API + static web UI."""

import base64
import ipaddress
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import __version__, config, system
from .db import Database, normalize_tags
from .docker import Docker
from .importer import ScriptImportError, import_script
from .recipefile import RecipeFileError, compact_recipe, dump_recipe_file, parse_recipe_file, recipe_from_file
from .script_check import check_script
from .manager import ActionError, Manager
from .processes import ProcessRunner
from .agent import create_agent_app
from .selfmetrics import Sink
from .executor import transport_of
from .joining import join_code
from .shares import add_share_routes
from .recipes import (CARD_COLORS, FRAMEWORKS, KINDS, RUNTIMES, build_run_argv, fill_weights, format_command,
                      annotate_copies, format_process_command, quick_recipe, slug, split_args, synced_copy,
                      uses_host_network)
from .scheduler import ACTIONS, Scheduler, compute_next_run, local_now, parse_time, validate_cron
from .timeline import build_timeline
from .transfers import check_image, check_name, check_repo

STATIC_DIR = Path(__file__).parent / "static"
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class RecipeIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    host_id: Optional[int] = None  # None = this machine
    runtime: str = "docker"
    framework: str = "vllm"
    image: str = ""
    model: str = ""
    container_name: str = ""
    host_port: int = Field(ge=1, le=65535)
    container_port: int = Field(ge=1, le=65535)
    command: str = ""
    args: str = ""
    env: str = ""
    volumes: str = ""
    docker_args: str = ""
    gpus: str = "all"
    health_path: str = "/health"
    est_memory_gb: Optional[float] = Field(default=None, ge=0)
    tags: list[str] = []
    color: str = ""
    pinned: bool = False
    kind: str = "model"
    weights_id: Optional[int] = None  # a registered weights entry; its per-host path fills {weights}

    @field_validator("kind")
    @classmethod
    def known_kind(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        return v

    @field_validator("color")
    @classmethod
    def known_color(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in CARD_COLORS:
            raise ValueError(f"color must be one of {', '.join(c for c in CARD_COLORS if c)} or empty")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def clean_tags(cls, v):
        return normalize_tags(v)

    @field_validator("name", "image", "model", "container_name", "gpus", "health_path")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("framework")
    @classmethod
    def known_framework(cls, v: str) -> str:
        if v not in FRAMEWORKS:
            raise ValueError(f"framework must be one of {', '.join(FRAMEWORKS)}")
        return v

    @model_validator(mode="after")
    def check_runtime(self):
        if self.runtime not in RUNTIMES:
            raise ValueError(f"runtime must be one of {', '.join(RUNTIMES)}")
        if FRAMEWORKS[self.framework]["runtime"] != self.runtime:
            raise ValueError(f"framework '{self.framework}' is not a {RUNTIMES[self.runtime]} framework")
        if self.runtime == "docker" and not self.image:
            raise ValueError("image is required for Docker recipes")
        if self.runtime == "process":
            if not self.command.strip():
                raise ValueError("command is required for native-process recipes")
            # The server listens on the host directly: one port, and the docker-only fields are unused.
            self.container_port = self.host_port
        return self

    @model_validator(mode="after")
    def derive_container_name(self):
        if not self.container_name:
            self.container_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.name).strip("-._").lower() or "model"
        if not NAME_RE.match(self.container_name):
            raise ValueError("container_name may only contain letters, digits, '_', '.', '-'")
        if self.health_path and not self.health_path.startswith("/"):
            self.health_path = "/" + self.health_path
        try:
            host_network = uses_host_network(split_args(self.docker_args))
        except ValueError:
            raise ValueError("Extra docker flags: unbalanced quotes")
        if host_network:
            # The server listens on the host directly, so there is only one port.
            self.container_port = self.host_port
        return self


class ImportIn(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    # Required when the static check finds risky calls.
    trust: bool = False


class SettingsIn(BaseModel):
    schedules_paused: bool


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    action: str
    recipe_id: Optional[int] = None
    cron: Optional[str] = None
    run_at: Optional[str] = None
    enabled: bool = True
    host_id: Optional[int] = None  # stop_all / free_memory: one host, or None for all hosts

    @model_validator(mode="after")
    def check(self):
        if self.action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        if ACTIONS[self.action]["needs_recipe"]:
            if self.recipe_id is None:
                raise ValueError("this action needs a recipe")
        else:
            self.recipe_id = None
        if ACTIONS[self.action]["needs_recipe"]:
            self.host_id = None  # the recipe's host is used
        self.cron = (self.cron or "").strip() or None
        self.run_at = (self.run_at or "").strip() or None
        if bool(self.cron) == bool(self.run_at):
            raise ValueError("set exactly one of cron or run_at")
        if self.cron:
            validate_cron(self.cron)
        else:
            try:
                parse_time(self.run_at)
            except ValueError:
                raise ValueError("run_at must be an ISO date-time, e.g. 2026-10-25T22:00")
        return self


class WeightIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source: str = Field(default="", max_length=300)
    # host id -> path of these weights on that host; an empty path removes the host.
    locations: dict[int, str] = {}

    @model_validator(mode="after")
    def check(self):
        self.name, self.source = self.name.strip(), self.source.strip()
        if not self.name:
            raise ValueError("name is required")
        for path in self.locations.values():
            if ":" in path or "\n" in path:
                raise ValueError("a path may not contain ':' or line breaks (it becomes a docker volume)")
        return self


class AdoptIn(BaseModel):
    recipe_ids: list[int] = Field(min_length=1)
    name: str = Field(default="", max_length=120)


class QuickIn(BaseModel):
    """A recipe made from an existing recipe's (or a preset's) settings and other weights."""
    host_id: Optional[int] = None
    base_recipe_id: Optional[int] = None   # copy this recipe's engine settings ...
    framework: str = ""                    # ... or start from this preset
    weights_id: Optional[int] = None       # the model: registered weights,
    path: str = Field(default="", max_length=1000)   # a folder / .gguf on the host (registered on save),
    model: str = Field(default="", max_length=300)   # or a Hugging Face id
    name: str = Field(default="", max_length=100)
    context: Optional[int] = Field(default=None, ge=256, le=10_000_000)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    save: bool = True                      # False: only return the draft and its command

    @model_validator(mode="after")
    def check(self):
        self.path, self.model, self.name = self.path.strip(), self.model.strip(), self.name.strip()
        if sum(bool(x) for x in (self.weights_id, self.path, self.model)) != 1:
            raise ValueError("pick the model: registered weights, a path on the host or a Hugging Face id")
        if ":" in self.path or "\n" in self.path:
            raise ValueError("a path may not contain ':' or line breaks (it becomes a docker volume)")
        if not self.base_recipe_id and self.framework not in FRAMEWORKS:
            raise ValueError("pick the settings: an existing recipe or an engine preset")
        return self


class RestartJobIn(BaseModel):
    recipe_ids: list[int] = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=120)


class PullJobIn(BaseModel):
    image: str = Field(min_length=1, max_length=300)
    host_ids: list[int] = Field(min_length=1, max_length=100)


class DownloadJobIn(BaseModel):
    repo: str = Field(min_length=1, max_length=200)   # Hugging Face model id, e.g. Qwen/Qwen3-8B
    name: str = Field(default="", max_length=128)     # folder in each host's model dir; default: the repo name
    host_ids: list[int] = Field(min_length=1, max_length=100)
    weight_id: Optional[int] = None                   # register the paths on this entry (default: by name)
    once: bool = True                                 # download on the first host only, copy to the others


class CopyJobIn(BaseModel):
    weight_id: int
    host_ids: list[int] = Field(min_length=1, max_length=100)


class CopyIn(BaseModel):
    host_ids: list[int] = Field(min_length=1)


class WeightPathIn(BaseModel):
    path: str = Field(default="", max_length=1000)


class JoinIn(BaseModel):
    """An agent asking to be added (DGXM_HUB_URL on that machine)."""
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=1, max_length=300)
    token: str = Field(min_length=32, max_length=300)
    version: str = Field(default="", max_length=40)
    info: dict[str, Any] = {}

    @model_validator(mode="after")
    def check(self):
        self.name, self.url = self.name.strip(), self.url.strip().rstrip("/")
        if not re.match(r"^[A-Za-z0-9][\w.-]*$", self.name):
            raise ValueError("name may only contain letters, digits, '_', '.', '-'")
        if not re.match(r"^https?://[A-Za-z0-9.\-\[\]:]+$", self.url):
            raise ValueError("url must be http(s)://host:port")
        self.info = {k: str(v)[:100] for k, v in list(self.info.items())[:10]}
        return self


class JoinApproveIn(BaseModel):
    name: str = Field(default="", max_length=60)   # list it under another name than the agent's own


class HostIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = ""     # empty for this machine; e.g. http://mac-studio.local:8090 for an agent
    token: str = ""   # agent token; empty on update = keep the stored one

    @model_validator(mode="after")
    def check(self):
        self.name, self.url, self.token = self.name.strip(), self.url.strip().rstrip("/"), self.token.strip()
        if self.url and not re.match(r"^https?://[^\s/]+(:\d+)?(/.*)?$", self.url):
            raise ValueError("url must look like http://hostname:8090")
        return self


def seed_examples(db: Database) -> None:
    if db.list_recipes():
        return
    if sys.platform == "darwin":
        seed_mac_examples(db)
        return
    vllm = FRAMEWORKS["vllm"]
    db.create_recipe({
        "name": "Qwen3-Coder-30B-AWQ (vLLM)",
        "description": "Example recipe - edit to match your setup.",
        "framework": "vllm", "image": vllm["image"],
        "model": "cpatonn/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
        "container_name": "vllm-qwen3-coder-30b-awq", "host_port": 8104, "container_port": 8000,
        "command": vllm["command"],
        "args": ("--max-model-len 65536\n--max-num-seqs 8\n--gpu-memory-utilization 0.4\n"
                 "--enable-prefix-caching\n--enable-auto-tool-choice\n--tool-call-parser qwen3_coder"),
        "env": vllm["env"], "volumes": vllm["volumes"], "docker_args": vllm["docker_args"],
        "gpus": "all", "health_path": "/health", "est_memory_gb": 45, "tags": ["qwen3-coder", "example"],
    })
    sg = FRAMEWORKS["sglang"]
    db.create_recipe({
        "name": "Qwen3-8B (SGLang)",
        "description": "Example recipe - edit to match your setup.",
        "framework": "sglang", "image": sg["image"], "model": "Qwen/Qwen3-8B",
        "container_name": "sglang-qwen3-8b", "host_port": 8110, "container_port": 30000,
        "command": sg["command"], "args": sg["args"], "env": sg["env"], "volumes": sg["volumes"],
        "docker_args": sg["docker_args"], "gpus": "all", "health_path": "/health", "est_memory_gb": 30,
        "tags": ["qwen3-8b", "example"],
    })


def seed_mac_examples(db: Database) -> None:
    db.create_recipe({
        "name": "Qwen3-8B (MLX)", "description": "Example recipe - needs `pip install mlx-lm`.",
        "runtime": "process", "framework": "process-custom", "image": "", "model": "mlx-community/Qwen3-8B-4bit",
        "container_name": "mlx-qwen3-8b", "host_port": 8080, "container_port": 8080,
        "command": "mlx_lm.server --model {model} --host {bind_host} --port {port}",
        "args": "", "env": "", "volumes": "", "docker_args": "", "gpus": "",
        "health_path": "/v1/models", "est_memory_gb": 6, "tags": ["qwen3-8b", "example"],
    })
    db.create_recipe({
        "name": "GGUF model (llama.cpp)", "description": "Example recipe - needs `brew install llama.cpp`.",
        "runtime": "process", "framework": "process-custom", "image": "", "model": "~/models/model.gguf",
        "container_name": "llamacpp-gguf", "host_port": 8081, "container_port": 8081,
        "command": "llama-server -m {model} --host {bind_host} --port {port}",
        "args": "-ngl 999\n-c 32768", "env": "", "volumes": "", "docker_args": "",
        "gpus": "", "health_path": "/health", "est_memory_gb": None, "tags": ["gguf", "example"],
    })


def hub_timezone() -> str:
    """Zone the scheduler uses, e.g. "CET (UTC+01:00)". In Docker this is UTC unless the host's is mounted."""
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    return f"{now.tzname()} (UTC{offset[:3]}:{offset[3:]})"


SECURITY_HEADERS = {
    # The dashboard must not be framed by other sites (clickjacking with the browser's saved login).
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

MIN_PASSWORD_LENGTH = 12
MIN_AGENT_TOKEN_LENGTH = 32
COMMON_PASSWORDS = {"change-me", "changeme", "password", "passwort", "admin", "administrator", "letmein",
                    "qwerty", "secret", "123456", "12345678", "123456789", "1234567890", "dgxspark", "nvidia"}
# Brute-force protection: after this many wrong logins from one address within the window, it gets 429.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 600


def password_problem(password: str, user: str = "") -> Optional[str]:
    """Why a dashboard password is too weak, or None if it is acceptable."""
    if password.lower() in COMMON_PASSWORDS:
        return "is a well-known example/default password"
    if user and len(user) >= 3 and user.lower() in password.lower():
        return "contains the user name"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"is shorter than {MIN_PASSWORD_LENGTH} characters"
    if len(set(password)) < 4:
        return "uses fewer than 4 different characters"
    return None


class LoginThrottle:
    """Counts failed logins per client address in a sliding window (in memory, per process)."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW_SECONDS):
        self.max_failures, self.window = max_failures, window
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def retry_after(self, key: str) -> int:
        """Seconds until this address may try again (0 = not locked)."""
        now = time.monotonic()
        recent = self._recent(key, now)
        return int(self.window - (now - recent[0])) + 1 if len(recent) >= self.max_failures else 0

    def failed(self, key: str) -> None:
        now = time.monotonic()
        self._failures[key] = [*self._recent(key, now), now]
        if len(self._failures) > 10_000:  # bound memory under a flood of distinct addresses
            self._failures.pop(next(iter(self._failures)))

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)


def _hostname(netloc: str) -> str:
    """Host part of a Host header / URL netloc, without port or IPv6 brackets."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc[1:netloc.find("]")] if "]" in netloc else netloc
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def _is_private_name(host: str) -> bool:
    """IP addresses, localhost, and names that only resolve inside your own network: single-label names
    (MagicDNS `spark`), mDNS `*.local` and Tailscale `*.ts.net`. A DNS-rebinding attacker needs a public
    domain of their own, which never looks like these."""
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".ts.net")) or "." not in host:
        return bool(host)
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def request_guard(request: Request) -> Optional[Response]:
    """Reject requests a browser could be tricked into sending on the user's behalf.

    - Cross-site requests that change something (CSRF): browsers send the saved Basic-auth login with
      a form POST from any website, so state-changing requests must come from the dashboard's own origin.
      Clients without an Origin header (curl, scripts, the hub's agent calls) are not affected.
    - DNS rebinding when no password is set: an attacker's domain that resolves to this machine. Without
      a password only IP addresses, localhost and local names (MagicDNS, *.local, *.ts.net) are accepted.
    """
    host = request.headers.get("host", "")
    if not config.AUTH_PASSWORD and host and not _is_private_name(_hostname(host)):
        return PlainTextResponse("Open the dashboard by IP address or local/Tailscale name, or set "
                                 "DGXM_PASSWORD to use other host names.", status_code=403)
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None:
            parsed = urlsplit(origin)
            # Behind a reverse proxy the browser's host may only be in X-Forwarded-Host.
            forwarded = request.headers.get("x-forwarded-host", "").split(",")
            allowed = {h.strip().lower() for h in (host, *forwarded)} - {""}
            if parsed.netloc.lower() not in allowed:
                return PlainTextResponse("Cross-site request blocked", status_code=403)
        elif request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return PlainTextResponse("Cross-site request blocked", status_code=403)
    return None


def create_app(db: Optional[Database] = None, docker: Optional[Docker] = None,
               run_scheduler: bool = True, procs: Optional[ProcessRunner] = None,
               remote_transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    db = db or Database(config.DB_PATH)
    if os.environ.get("DGXM_SEED", "1") != "0":
        seed_examples(db)
    manager = Manager(db, docker, procs, remote_transport)
    scheduler = Scheduler(db, manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_scheduler:
            scheduler.start()
        yield
        await scheduler.stop()

    app = FastAPI(title="Local LLM Manager", version=__version__, lifespan=lifespan)
    app.state.db, app.state.manager, app.state.scheduler = db, manager, scheduler
    app.state.metrics = Sink()

    throttle = LoginThrottle()
    app.state.login_throttle = throttle

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        # Liveness / self-metrics for external scrapers: no login, no request guards, not counted.
        # Shares are read by other machines copying weights: they carry their own ticket (shares.py).
        # Agents asking to join prove nothing yet: they only land in the list someone has to approve.
        if request.url.path in ("/healthz", "/metrics") or (
                request.url.path.startswith("/agent/v1/shares/") and request.method in ("GET", "HEAD")) or (
                request.url.path == "/api/join" and request.method == "POST"):
            return await call_next(request)
        if blocked := request_guard(request):
            return blocked
        if config.AUTH_PASSWORD:
            client = request.client.host if request.client else "unknown"
            if wait := throttle.retry_after(client):
                return PlainTextResponse(f"Too many failed logins - try again in {wait} s", status_code=429,
                                         headers={"Retry-After": str(wait)})
            ok = False
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = (secrets.compare_digest(user.encode(), config.AUTH_USER.encode())
                          & secrets.compare_digest(pw.encode(), config.AUTH_PASSWORD.encode()))
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                if header:  # a wrong login, not just the browser's first request without credentials
                    throttle.failed(client)
                    logging.warning("Failed dashboard login from %s", client)
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Local LLM Manager"'})
            throttle.succeeded(client)
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        route = request.scope.get("route")  # the matched API path, with ids already collapsed
        if route is not None and getattr(route, "path", None):
            app.state.metrics.add("dgxm_api_requests_total", "API requests, by method, route and status.",
                                  method=request.method, path=route.path, status=str(response.status_code))
        return response

    @app.exception_handler(ActionError)
    async def action_error(_: Request, exc: ActionError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    # This machine as a copy source for the others (weights copied from the hub's own host).
    add_share_routes(app, manager.local.shares)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(_: Request, exc: sqlite3.IntegrityError):
        msg = str(exc)
        if "recipes.name" in msg:
            msg = "A recipe with this name already exists"
        elif "recipes.container_name" in msg:
            msg = "Another recipe already uses this container name"
        elif "hosts.name" in msg:
            msg = "A host with this name already exists"
        return JSONResponse(status_code=409, content={"detail": msg})

    def get_recipe_or_404(recipe_id: int) -> dict:
        r = db.get_recipe(recipe_id)
        if not r:
            raise HTTPException(404, "Recipe not found")
        return r

    # ---- self-monitoring (no login: liveness for scrapers, no sensitive data) ----

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """Process is up and the database answers. 503 (not 4xx) means "retry", for monitors."""
        if not db.ping():
            return PlainTextResponse("database unreachable", status_code=503)
        return PlainTextResponse("ok")

    @app.get("/metrics", include_in_schema=False)
    async def self_metrics():
        """The manager's own metrics (Prometheus text), enabled with DGXM_METRICS=1."""
        if not config.METRICS_ENABLED:
            raise HTTPException(404)
        sink = app.state.metrics
        sink.gauge("dgxm_build_info", "Manager build.", 1.0, version=__version__, mode="hub")
        sink.gauge("dgxm_uptime_seconds", "Seconds since the manager started.", sink.uptime())
        counts = db.counts()
        sink.gauge("dgxm_recipes_total", "Stored recipes.", counts["recipes"])
        sink.gauge("dgxm_hosts_total", "Configured hosts.", counts["hosts"])
        sink.gauge("dgxm_schedules_total", "Stored schedules.", counts["schedules"])
        sink.gauge("dgxm_schedules_enabled_total", "Enabled schedules.", counts["schedules_enabled"])
        for (action, result), n in manager.actions.items():
            sink.counter("dgxm_actions_total", "Recipe actions since start, by action and result.",
                         n, action=action, result=result)
        for result, n in scheduler.runs.items():
            sink.counter("dgxm_schedule_runs_total", "Scheduled runs since start, by result.", n, result=result)
        return PlainTextResponse(sink.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    # ---- meta ----------------------------------------------------------

    @app.get("/api/meta")
    async def meta():
        return {
            "version": __version__,
            "frameworks": FRAMEWORKS,
            "colors": CARD_COLORS,
            "runtimes": RUNTIMES,
            "kinds": KINDS,
            "platform": sys.platform,
            "actions": ACTIONS,
            "timezone": hub_timezone(),
        }

    @app.get("/api/images")
    async def images():
        return await manager.images_overview()

    @app.get("/api/images/check")
    async def image_check(host_id: int, image: str):
        """Whether a Docker image is present on one host (the recipe editor asks this as you type)."""
        get_host_or_404(host_id)
        try:
            image = check_image(image)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return await manager.image_status(image, host_id)

    @app.get("/api/models")
    async def models():
        return await manager.models_overview()

    @app.get("/api/system")
    async def system_info():
        return await system.snapshot()

    @app.post("/api/system/free-memory")
    async def free_memory(host_id: Optional[int] = None):
        if host_id is not None:
            get_host_or_404(host_id)
        return {"message": await manager.free_memory(host_id=host_id)}

    @app.post("/api/system/stop-all")
    async def stop_all(host_id: Optional[int] = None):
        if host_id is not None:
            get_host_or_404(host_id)
        return {"message": await manager.stop_all(host_id=host_id)}

    # ---- hosts ---------------------------------------------------------

    def get_host_or_404(host_id: int) -> dict:
        h = db.get_host(host_id)
        if not h:
            raise HTTPException(404, "Host not found")
        return h

    @app.get("/api/hosts")
    async def list_hosts():
        """All hosts with online state and memory/GPU readings. Tokens are never returned."""
        return await manager.host_overview()

    @app.post("/api/hosts", status_code=201)
    async def create_host(body: HostIn):
        if not body.url:
            raise HTTPException(422, "url is required (this machine is already listed)")
        if not body.token:
            raise HTTPException(422, "token is required: the agent's DGXM_AGENT_TOKEN")
        hid = db.create_host(body.name, body.url, body.token)
        return next(h for h in await manager.host_overview() if h["id"] == hid)

    @app.put("/api/hosts/{host_id}")
    async def update_host(host_id: int, body: HostIn):
        host = get_host_or_404(host_id)
        if not host["url"] and body.url:
            raise HTTPException(422, "This machine has no URL; add other machines as new hosts")
        if host["url"] and not body.url:
            raise HTTPException(422, "url is required for a remote host")
        db.update_host(host_id, body.name, body.url, body.token or host["token"])
        return next(h for h in await manager.host_overview() if h["id"] == host_id)

    # ---- agents asking to join -----------------------------------------

    MAX_PENDING_JOINS = 20

    def _expire_joins() -> None:
        now = datetime.now().astimezone()
        db.expire_join_requests((now - timedelta(hours=1)).isoformat(timespec="seconds"),
                                (now - timedelta(days=7)).isoformat(timespec="seconds"))

    @app.post("/api/join")
    async def join(body: JoinIn, request: Request):
        """Called by agents (no login). Unknown agents land in the list under Hosts until approved there."""
        _expire_joins()
        known = next((h for h in db.list_hosts() if h["url"] and secrets.compare_digest(h["token"], body.token)), None)
        if known:
            # Only the approved agent knows its token: it may tell its new address.
            if known["url"].rstrip("/") != body.url:
                db.update_host(known["id"], known["name"], body.url, known["token"])
                db.add_event("agent", "join", True, f"{known['name']} moved to {body.url}", target=known["name"])
            return {"state": "approved"}
        existing = next((r for r in db.list_join_requests() if secrets.compare_digest(r["token"], body.token)), None)
        if existing and existing["state"] == "rejected":
            return {"state": "rejected"}
        if not existing and sum(r["state"] == "pending" for r in db.list_join_requests()) >= MAX_PENDING_JOINS:
            raise HTTPException(429, "Too many open join requests on the hub: approve or reject some there")
        code = join_code(body.token)
        ip = request.client.host if request.client else ""
        db.upsert_join_request(body.name, body.url, body.token, code, {**body.info, "version": body.version}, ip)
        if not existing:
            db.add_event("agent", "join", True, f"{body.name} ({body.url}, from {ip}) asks to be added - code {code}",
                         target=body.name)
        return {"state": "pending", "code": code}

    @app.get("/api/join-requests")
    async def join_requests():
        _expire_joins()
        return [{k: v for k, v in r.items() if k != "token"} | {"transport": transport_of(r["url"])}
                for r in db.list_join_requests()]

    def get_join_or_404(request_id: int) -> dict:
        r = db.get_join_request(request_id)
        if not r:
            raise HTTPException(404, "Join request not found (expired after an hour without news from the agent?)")
        return r

    @app.post("/api/join-requests/{request_id}/approve")
    async def approve_join(request_id: int, body: JoinApproveIn):
        """Add the agent as a host. Only approve when the agent shows the same code as listed here."""
        r = get_join_or_404(request_id)
        if r["state"] != "pending":
            raise HTTPException(409, "This request was rejected")
        name = body.name.strip() or r["name"]
        same = next((h for h in db.list_hosts() if h["name"].lower() == name.lower()), None)
        if same and same["url"].rstrip("/") != r["url"]:
            raise HTTPException(409, f"A host named '{name}' exists already: approve under another name")
        if same:
            db.update_host(same["id"], same["name"], r["url"], r["token"])  # the same machine, reinstalled
            host_id = same["id"]
        else:
            host_id = db.create_host(name, r["url"], r["token"])
        db.delete_join_request(request_id)
        db.add_event("user", "join", True, f"Added {name} ({r['url']}), code {r['code']}", target=name)
        return next(h for h in await manager.host_overview() if h["id"] == host_id)

    @app.post("/api/join-requests/{request_id}/reject", status_code=204)
    async def reject_join(request_id: int):
        r = get_join_or_404(request_id)
        db.set_join_request_state(request_id, "rejected")
        db.add_event("user", "join", False, f"Rejected {r['name']} ({r['url']}), code {r['code']}", target=r["name"])
        return Response(status_code=204)

    @app.delete("/api/hosts/{host_id}", status_code=204)
    async def delete_host(host_id: int):
        host = get_host_or_404(host_id)
        if not host["url"]:
            raise HTTPException(409, "This machine cannot be removed")
        n = db.count_host_recipes(host_id)
        if n:
            raise HTTPException(409, f"Move or delete the {n} recipe(s) on this host first")
        db.delete_host(host_id)
        return Response(status_code=204)

    # ---- weights -------------------------------------------------------

    def get_weight_or_404(weight_id: int) -> dict:
        w = db.get_weight(weight_id)
        if not w:
            raise HTTPException(404, "Weights not found")
        return w

    def weight_locations(body: WeightIn) -> dict[int, str]:
        for host_id in body.locations:
            get_host_or_404(host_id)
        return body.locations

    @app.get("/api/weights")
    async def list_weights(check: bool = True):
        """Registered weights; with check (default) also whether each host's path exists."""
        if not check:
            return {"weights": db.list_weights()}
        return await manager.weights_overview()

    @app.post("/api/weights", status_code=201)
    async def create_weight(body: WeightIn):
        try:
            wid = db.create_weight(body.name, body.source, weight_locations(body))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Weights named '{body.name}' already exist")
        return db.get_weight(wid)

    @app.put("/api/weights/{weight_id}")
    async def update_weight(weight_id: int, body: WeightIn):
        get_weight_or_404(weight_id)
        try:
            db.update_weight(weight_id, body.name, body.source, weight_locations(body))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Weights named '{body.name}' already exist")
        return db.get_weight(weight_id)

    @app.get("/api/weights/discover")
    async def discover_weights():
        """Search every host's model dirs (DGXM_MODEL_DIR) and match the folders to registered weights."""
        return await manager.discover_weights()

    @app.post("/api/weights/adopt")
    async def adopt_weights(body: AdoptIn):
        """Put recipes that mount their model folder directly onto a registered weights entry."""
        try:
            return manager.adopt_weights(body.recipe_ids, body.name)
        except ActionError as e:
            raise HTTPException(e.status_code, e.message)

    @app.put("/api/weights/{weight_id}/hosts/{host_id}")
    async def set_weight_path(weight_id: int, host_id: int, body: WeightPathIn):
        """Set (or, with an empty path, remove) the path of one entry on one host."""
        get_weight_or_404(weight_id)
        get_host_or_404(host_id)
        path = body.path.strip()
        if ":" in path or "\n" in path:
            raise HTTPException(422, "a path may not contain ':' or line breaks (it becomes a docker volume)")
        db.set_weight_location(weight_id, host_id, path)
        return await manager.check_weight(weight_id, host_id)

    @app.get("/api/weights/{weight_id}/hosts/{host_id}")
    async def check_weight(weight_id: int, host_id: int):
        """Whether an entry has a path on a host and whether it exists there (the recipe editor asks
        this when the host or the weights change)."""
        get_weight_or_404(weight_id)
        get_host_or_404(host_id)
        return await manager.check_weight(weight_id, host_id)

    @app.delete("/api/weights/{weight_id}", status_code=204)
    async def delete_weight(weight_id: int):
        w = get_weight_or_404(weight_id)
        if w["recipe_ids"]:
            n = len(w["recipe_ids"])
            raise HTTPException(409, f"{n} recipe{'s use' if n != 1 else ' uses'} these weights: pick other weights there first")
        db.delete_weight(weight_id)
        return Response(status_code=204)

    # ---- recipes -------------------------------------------------------

    @app.get("/api/recipes")
    async def list_recipes():
        return annotate_copies(await manager.recipes_with_status())

    @app.get("/api/recipes/{recipe_id}")
    async def get_recipe(recipe_id: int):
        return await manager.recipe_status(recipe_id)

    def recipe_data(body: RecipeIn) -> dict:
        data = body.model_dump()
        data["host_id"] = get_host_or_404(body.host_id)["id"] if body.host_id else db.local_host_id()
        if body.weights_id is not None and not db.get_weight(body.weights_id):
            raise HTTPException(422, "The selected weights do not exist (any more)")
        return data

    @app.post("/api/recipes", status_code=201)
    async def create_recipe(body: RecipeIn):
        rid = db.create_recipe(recipe_data(body))
        return db.get_recipe(rid)

    @app.put("/api/recipes/{recipe_id}")
    async def update_recipe(recipe_id: int, body: RecipeIn):
        old = get_recipe_or_404(recipe_id)
        data = recipe_data(body)
        status = await manager.recipe_status(recipe_id)
        running = status["status"] in ("ready", "loading", "starting")
        if running and old["container_name"] != body.container_name:
            raise HTTPException(409, "Stop the model before renaming its container")
        if running and old["host_id"] != data["host_id"]:
            raise HTTPException(409, "Stop the model before moving it to another host")
        db.update_recipe(recipe_id, data)
        return db.get_recipe(recipe_id)

    @app.delete("/api/recipes/{recipe_id}", status_code=204)
    async def delete_recipe(recipe_id: int):
        get_recipe_or_404(recipe_id)
        status = await manager.recipe_status(recipe_id)
        if status["status"] in ("ready", "loading", "starting"):
            raise HTTPException(409, "Stop the model before deleting its recipe")
        db.delete_recipe(recipe_id)
        return Response(status_code=204)

    @app.post("/api/recipes/{recipe_id}/duplicate", status_code=201)
    async def duplicate_recipe(recipe_id: int):
        r = get_recipe_or_404(recipe_id)
        names = {x["name"] for x in db.list_recipes()}
        containers = {x["container_name"] for x in db.list_recipes()}
        i = 1
        while f"{r['name']} (copy {i})" in names or f"{r['container_name']}-copy{i}" in containers:
            i += 1
        r["name"] = f"{r['name']} (copy {i})"
        r["container_name"] = f"{r['container_name']}-copy{i}"
        return db.get_recipe(db.create_recipe(r))

    # ---- copies on other hosts: the same recipe on several machines, kept in sync -------------

    def original_of(recipe_id: int) -> dict:
        """The recipe copies are made from: the recipe itself, or the original a copy came from."""
        r = get_recipe_or_404(recipe_id)
        return (db.get_recipe(r["copy_of"]) if r.get("copy_of") else None) or r

    def copy_hosts(original: dict, host_ids: list[int]) -> list[int]:
        for hid in host_ids:
            get_host_or_404(hid)
        if original["host_id"] in host_ids:
            raise HTTPException(422, "The original already runs on that host: pick other hosts")
        return list(dict.fromkeys(host_ids))

    @app.post("/api/recipes/{recipe_id}/copies/check")
    async def check_copies(recipe_id: int, body: CopyIn):
        """Per host: are the weights and the image there, who else uses the port, is there a copy already."""
        original = original_of(recipe_id)
        return {"original_id": original["id"], "hosts": await manager.copy_check(original, copy_hosts(original, body.host_ids))}

    @app.post("/api/recipes/{recipe_id}/copies", status_code=201)
    async def create_copies(recipe_id: int, body: CopyIn):
        """Put a copy of the recipe on each host (a host that has one already gets it updated). Copies keep
        their own name, container name and host; everything else follows the original on "Sync"."""
        original = original_of(recipe_id)
        created, updated = [], []
        for hid in copy_hosts(original, body.host_ids):
            recipes = db.list_recipes()
            existing = next((x for x in recipes if x["host_id"] == hid and x.get("copy_of") == original["id"]), None)
            if existing:
                db.update_recipe(existing["id"], synced_copy(existing, original))
                updated.append(db.get_recipe(existing["id"]))
                continue
            host = db.get_host(hid)["name"]
            names, containers = {x["name"].lower() for x in recipes}, {x["container_name"] for x in recipes}
            name, container, i = f"{original['name']} · {host}", f"{original['container_name']}-{slug(host)}", 2
            while name.lower() in names or container in containers:
                name, container = f"{original['name']} · {host} {i}", f"{original['container_name']}-{slug(host)}-{i}"
                i += 1
            rid = db.create_recipe(synced_copy({"name": name, "container_name": container, "host_id": hid,
                                                "pinned": False}, original))
            db.set_copy_of(rid, original["id"])
            created.append(db.get_recipe(rid))
        return {"created": created, "updated": updated}

    @app.post("/api/recipes/{recipe_id}/sync")
    async def sync_copies(recipe_id: int):
        """Give copies the original's current settings: all copies of an original, or just this copy.
        Running ones keep running with the old settings until restarted (listed in `running`)."""
        r = get_recipe_or_404(recipe_id)
        original = original_of(recipe_id)
        targets = [r] if original["id"] != r["id"] else [x for x in db.list_recipes() if x.get("copy_of") == r["id"]]
        statuses = {x["id"]: x["status"] for x in await manager.recipes_with_status()}
        updated, running = [], []
        for c in targets:
            db.update_recipe(c["id"], synced_copy(c, original))
            updated.append(c["id"])
            if statuses.get(c["id"]) in ("ready", "loading", "starting"):
                running.append(c["id"])
        return {"updated": updated, "running": running}

    # ---- background jobs: keep running on the hub when the browser is closed -------------------

    @app.post("/api/jobs/restart", status_code=202)
    async def restart_job(body: RestartJobIn):
        """Restart several recipes (e.g. the copies of one) in the background: hosts in parallel."""
        for rid in body.recipe_ids:
            get_recipe_or_404(rid)
        return manager.start_restart_job(body.recipe_ids, title=body.title)

    @app.post("/api/jobs/pull", status_code=202)
    async def pull_job(body: PullJobIn):
        """`docker pull` the image on each host at the same time; each host pulls from the registry itself."""
        try:
            image = check_image(body.image)
            return manager.start_transfer_job("pull", image, body.host_ids)
        except ValueError as e:
            raise HTTPException(422, str(e))
        except ActionError as e:
            raise HTTPException(e.status_code, e.message)

    def hub_url(request: Request) -> str:
        """Where other machines reach this hub (to copy weights from its own host)."""
        if config.PUBLIC_URL:
            return config.PUBLIC_URL
        url = str(request.base_url).rstrip("/")
        return "" if transport_of(url) == "local" else url

    @app.post("/api/jobs/copy", status_code=202)
    async def copy_job(body: CopyJobIn, request: Request):
        """Copy registered weights to these hosts, host to host over the network, checked with SHA-256."""
        get_weight_or_404(body.weight_id)
        try:
            return manager.start_copy_job(body.weight_id, body.host_ids, hub_url(request))
        except ActionError as e:
            raise HTTPException(e.status_code, e.message)

    @app.post("/api/jobs/download", status_code=202)
    async def download_job(body: DownloadJobIn, request: Request):
        """Download Hugging Face weights on each host into its model dir (DGXM_MODEL_DIR) and register the
        folders in the Weights tab. Gated models need HF_TOKEN in that host's .env."""
        if body.weight_id is not None:
            get_weight_or_404(body.weight_id)
        try:
            repo, name = check_repo(body.repo), check_name(body.name) if body.name.strip() else ""
            return manager.start_transfer_job("download", repo, body.host_ids, name, body.weight_id,
                                              once=body.once, hub_url=hub_url(request))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except ActionError as e:
            raise HTTPException(e.status_code, e.message)

    @app.get("/api/jobs")
    async def list_jobs():
        return list(reversed(manager.jobs.values()))

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        job = manager.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found (jobs are kept until the manager restarts)")
        return job

    @app.post("/api/recipes/{recipe_id}/unlink")
    async def unlink_copy(recipe_id: int):
        """Make a copy an original of its own: syncing no longer changes it."""
        get_recipe_or_404(recipe_id)
        db.set_copy_of(recipe_id, None)
        return db.get_recipe(recipe_id)

    @app.post("/api/recipes/quick")
    async def quick(body: QuickIn):
        """Create a model recipe in one step: the settings of an existing recipe (or a preset), other
        weights, a name, port and served model name filled in. With save=false only the draft."""
        host_id = get_host_or_404(body.host_id)["id"] if body.host_id else db.local_host_id()
        recipes = db.list_recipes()
        if body.base_recipe_id:
            base = get_recipe_or_404(body.base_recipe_id)
        else:
            preset = FRAMEWORKS[body.framework]
            base = {**preset, "framework": body.framework, "model": "",
                    "gpus": "all" if preset["runtime"] == "docker" else ""}

        # The model: registered weights, a found folder (registered when saving) or a Hugging Face id.
        weight, path = None, ""
        if body.weights_id:
            weight = db.get_weight(body.weights_id)
            if not weight:
                raise HTTPException(422, "The selected weights do not exist (any more)")
            path = weight["locations"].get(host_id, "")
        elif body.path:
            path = body.path
            weight = next((w for w in db.list_weights()
                           if w["locations"].get(host_id, "").rstrip("/") == path.rstrip("/")), None)
        folder = path.rstrip("/").rsplit("/", 1)[-1]
        default_name = (weight["name"] if weight else re.sub(r"\.gguf$", "", folder, flags=re.I) if path
                        else body.model.rstrip("/").rsplit("/", 1)[-1])
        names = {r["name"].lower() for r in recipes}
        name, i = body.name or default_name, 2
        while name.lower() in names:
            name, i = f"{body.name or default_name} {i}", i + 1
        try:
            r, notes = quick_recipe(base, name=name, weights=bool(path or weight), model=body.model,
                                    context=body.context)
        except ValueError as e:
            raise HTTPException(422, str(e))

        # Same port as the base recipe on this host: "Switch to" swaps the two, the endpoint stays.
        if body.port:
            port = body.port
        elif base.get("id") and base.get("host_id") == host_id:
            port = base["host_port"]
            notes.append(f"Port {port} as “{base['name']}”: Switch to swaps them, the endpoint stays the same.")
        else:
            used, port = {x["host_port"] for x in recipes if x["host_id"] == host_id}, 8100
            while port in used:
                port += 1
        containers, container, i = {x["container_name"] for x in recipes}, slug(name), 2
        while container in containers:
            container, i = f"{slug(name)}-{i}", i + 1
        r.update(host_id=host_id, host_port=port, container_name=container,
                 description=f"Settings from {base['name'] if base.get('id') else base['label']}.")
        try:
            data = recipe_data(RecipeIn(**r))
        except ValidationError as e:
            raise HTTPException(422, "; ".join(err["msg"] for err in e.errors()))

        preview = fill_weights(data, path or None)
        command = (format_process_command(preview) if data["runtime"] == "process"
                   else format_command(build_run_argv(preview)))
        if path and not weight:
            notes.insert(0, f"“{folder}” will be registered in the Weights tab.")
        elif weight and not path:
            notes.insert(0, "These weights have no path on this host yet: set it in the Weights tab.")
        if not body.save:
            return {"recipe": data, "command": command, "notes": notes}

        if path and not weight:
            wname, i = re.sub(r"\.gguf$", "", folder, flags=re.I) or "model", 2
            existing = {w["name"].lower(): w for w in db.list_weights()}
            # The same name without a path here is most likely the same weights on another host.
            while wname.lower() in existing and host_id in existing[wname.lower()]["locations"]:
                wname, i = f"{folder} {i}", i + 1
            if wname.lower() in existing:
                weight = existing[wname.lower()]
                db.set_weight_location(weight["id"], host_id, path)
            else:
                weight = db.get_weight(db.create_weight(wname, "", {host_id: path}))
        data["weights_id"] = weight["id"] if weight else None
        return {"recipe": db.get_recipe(db.create_recipe(data)), "command": command, "notes": notes}

    @app.post("/api/recipes/preview")
    async def preview(body: RecipeIn, recipe_id: Optional[int] = None):
        recipe = body.model_dump()
        if recipe_id is not None:
            recipe["id"] = recipe_id  # an edited recipe's label, as `start` would set it
        # {weights} as it would be filled on the recipe's host (still shown as {weights} when unset there).
        host_id = body.host_id or db.local_host_id()
        recipe = fill_weights(recipe, db.weight_path(body.weights_id, host_id))
        if body.runtime == "process":
            return {"command": format_process_command(recipe)}
        return {"command": format_command(build_run_argv(recipe))}

    @app.post("/api/recipes/import/check")
    async def check_import(body: ImportIn):
        """Static review of a script before importing it. Nothing is executed."""
        if parse_recipe_file(body.text) is not None:
            return {"count": 0, "findings": []}
        return check_script(body.text)

    @app.post("/api/recipes/import")
    async def import_recipe(body: ImportIn):
        """Parse a starter script, `docker run` command or compact JSON/YAML recipe into a draft."""
        data = parse_recipe_file(body.text)
        if data is not None:
            try:
                recipe, warnings = recipe_from_file(data, db.list_weights())
            except RecipeFileError as e:
                raise HTTPException(422, str(e))
            result = {"recipe": recipe, "warnings": warnings, "docker_argv": None, "process_argv": None,
                      "skipped_calls": [], "check": {"count": 0, "findings": []}}
        else:
            check = check_script(body.text)
            if check["count"] and not body.trust:
                raise HTTPException(422, "The script contains risky calls. Review them and confirm that you trust the script.")
            try:
                result = await import_script(body.text)
            except ScriptImportError as e:
                raise HTTPException(422, str(e))
            result["check"] = check
            if check["count"]:
                db.add_event("user", "import", True, "Imported a script with risky calls after confirmation:\n"
                             + "\n".join(f"line {f['line']}: {f['message']}" for f in check["findings"]))
        recipe = result["recipe"]
        existing = db.list_recipes()
        names = {r["name"] for r in existing}
        containers = {r["container_name"] for r in existing}
        base, i = recipe["name"], 2
        while recipe["name"] in names:
            recipe["name"], i = f"{base} ({i})", i + 1
        if recipe["container_name"] in containers:
            base, i = recipe["container_name"], 2
            while recipe["container_name"] in containers:
                recipe["container_name"], i = f"{base}-{i}", i + 1
            result["warnings"].append(f"Container name changed to {recipe['container_name']} (already used)")
        return result

    def export_text(recipe: dict[str, Any], fmt: str) -> str:
        try:
            weight = db.get_weight(recipe.get("weights_id"))
            data = compact_recipe(recipe, weight["name"] if weight else None)
            return dump_recipe_file(data, fmt)
        except RecipeFileError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/recipes/{recipe_id}/export")
    async def export_recipe(recipe_id: int, format: str = "yaml"):
        """The saved recipe as a compact JSON/YAML file (omitted settings come from its preset)."""
        fmt = format.strip().lower()
        text = export_text(get_recipe_or_404(recipe_id), fmt)
        return Response(text, media_type="application/json" if fmt == "json" else "text/yaml")

    @app.post("/api/recipes/export")
    async def export_recipe_body(body: RecipeIn, recipe_id: Optional[int] = None, format: str = "yaml"):
        """Export the editor's current settings, also before the recipe is saved."""
        recipe = body.model_dump()
        if recipe_id is not None:
            recipe["id"] = recipe_id
        fmt = format.strip().lower()
        text = export_text(recipe, fmt)
        return Response(text, media_type="application/json" if fmt == "json" else "text/yaml")

    @app.post("/api/recipes/{recipe_id}/start")
    async def start(recipe_id: int, force: bool = False):
        return {"message": await manager.start(recipe_id, force=force)}

    @app.post("/api/recipes/{recipe_id}/stop")
    async def stop(recipe_id: int):
        return {"message": await manager.stop(recipe_id)}

    @app.post("/api/recipes/{recipe_id}/restart")
    async def restart(recipe_id: int, force: bool = False):
        return {"message": await manager.restart(recipe_id, force=force)}

    @app.post("/api/recipes/{recipe_id}/switch")
    async def switch(recipe_id: int):
        return {"message": await manager.switch(recipe_id)}

    @app.get("/api/recipes/{recipe_id}/logs", response_class=PlainTextResponse)
    async def logs(recipe_id: int, tail: int = 500):
        r = get_recipe_or_404(recipe_id)
        return await manager.logs(r, tail=max(1, min(tail, 20000)))

    @app.get("/api/recipes/{recipe_id}/logs/stream")
    async def logs_stream(recipe_id: int, tail: int = 200):
        r = get_recipe_or_404(recipe_id)

        async def events():
            async for line in manager.follow_logs(r, tail=max(0, min(tail, 20000))):
                yield f"data: {line}\n\n"
            yield "event: end\ndata: \n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- schedules -----------------------------------------------------

    def check_recipe_ref(body: ScheduleIn) -> None:
        if body.recipe_id is not None:
            get_recipe_or_404(body.recipe_id)
        if body.host_id is not None:
            get_host_or_404(body.host_id)

    def next_run_for(body: ScheduleIn) -> Optional[str]:
        return compute_next_run(body.model_dump()) if body.enabled else None

    @app.get("/api/schedules")
    async def list_schedules():
        return db.list_schedules()

    @app.get("/api/schedules/timeline")
    async def schedule_timeline(hours: int = 168):
        """Which model runs when over the next `hours`, if every scheduled action succeeds."""
        start = local_now().replace(second=0, microsecond=0)
        end = start + timedelta(hours=max(1, min(hours, 24 * 31)))
        recipes = await manager.recipes_with_status()
        running = {r["id"] for r in recipes if r["status"] in ("ready", "loading", "starting")}
        hosts = {h["id"]: h["name"] for h in db.list_hosts()}
        return build_timeline(db.list_schedules(), recipes, running, start, end, hosts)

    @app.post("/api/schedules", status_code=201)
    async def create_schedule(body: ScheduleIn):
        check_recipe_ref(body)
        data = body.model_dump()
        data["enabled"] = int(body.enabled)
        sid = db.create_schedule(data, next_run_for(body))
        return db.get_schedule(sid)

    @app.put("/api/schedules/{schedule_id}")
    async def update_schedule(schedule_id: int, body: ScheduleIn):
        if not db.get_schedule(schedule_id):
            raise HTTPException(404, "Schedule not found")
        check_recipe_ref(body)
        data = body.model_dump()
        data["enabled"] = int(body.enabled)
        db.update_schedule(schedule_id, data, next_run_for(body))
        return db.get_schedule(schedule_id)

    @app.delete("/api/schedules/{schedule_id}", status_code=204)
    async def delete_schedule(schedule_id: int):
        db.delete_schedule(schedule_id)
        return Response(status_code=204)

    @app.post("/api/schedules/{schedule_id}/run")
    async def run_schedule(schedule_id: int):
        s = db.get_schedule(schedule_id)
        if not s:
            raise HTTPException(404, "Schedule not found")
        ok, msg = await scheduler.run_schedule(s, source=f"manual:{s['name']}")
        if not ok:
            raise HTTPException(409, msg)
        return {"message": msg}

    # ---- settings ------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings():
        return {"schedules_paused": db.get_setting("schedules_paused") == "1"}

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn):
        db.set_setting("schedules_paused", "1" if body.schedules_paused else "0")
        # On resume, realign next runs so a long pause does not fire every missed run at once.
        scheduler.resync()
        return {"schedules_paused": body.schedules_paused}

    # ---- start times ----------------------------------------------------

    @app.get("/api/start-times")
    async def start_times():
        """Seconds from start trigger to ready, newest first, grouped per recipe."""
        out: dict[str, list[float]] = {}
        for row in db.list_start_times():
            out.setdefault(str(row["recipe_id"]), []).append(row["seconds"])
        return out

    # ---- activity ------------------------------------------------------

    @app.get("/api/events")
    async def events(limit: int = 200):
        return db.list_events(max(1, min(limit, 2000)))

    # ---- UI ------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


# Exit status for configuration errors (sysexits EX_CONFIG). The systemd unit does not restart on it,
# since retrying cannot fix a weak password; a missing Tailscale address (exit 1) is retried.
EX_CONFIG = 78


def config_error(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(EX_CONFIG)


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    os.umask(0o077)  # database, WAL and process logs: owner only
    config.resolve_network()
    if config.MODE == "agent":
        if not config.AGENT_TOKEN:
            config_error("DGXM_MODE=agent needs DGXM_AGENT_TOKEN (a long random secret shared with the hub)")
        if len(config.AGENT_TOKEN) < MIN_AGENT_TOKEN_LENGTH:
            config_error(f"DGXM_AGENT_TOKEN is shorter than {MIN_AGENT_TOKEN_LENGTH} characters; "
                         "generate one with: openssl rand -hex 32")
        uvicorn.run(create_agent_app(), host=config.HOST, port=config.PORT, log_level="info")
        return
    if config.AUTH_PASSWORD and (problem := password_problem(config.AUTH_PASSWORD, config.AUTH_USER)):
        config_error(f"DGXM_PASSWORD {problem}. Set a stronger one (at least {MIN_PASSWORD_LENGTH} characters, "
                     "e.g. from: openssl rand -base64 18) in .env, then restart the service")
    if not config.AUTH_PASSWORD:
        logging.warning("DGXM_PASSWORD is not set - the dashboard is open to anyone who can reach port %s "
                        "(only by IP address or local/Tailscale name)", config.PORT)
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    run()
