"""Model lifecycle across hosts: start / stop / restart / switch recipes, with status and health.

The Manager owns the rules (port conflicts, memory check, what "switch" stops, the activity log);
executors (executor.py) do the actual work on this machine or on a remote agent.
"""

import asyncio
import logging
import secrets
from collections import Counter
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import httpx

from .db import Database
from .docker import Docker, split_image
from .executor import (RUNNING_STATES, transport_of, ActionError, LocalExecutor, RemoteExecutor, agent_payload_recipe,
                       resolve_model_target)
from .processes import ProcessRunner
from .recipes import WEIGHTS, fill_weights, model_mount, use_weights_mount, uses_weights

log = logging.getLogger(__name__)

__all__ = ["ActionError", "Manager"]


class Manager:
    def __init__(self, db: Database, docker: Optional[Docker] = None, procs: Optional[ProcessRunner] = None,
                 remote_transport: Optional[httpx.AsyncBaseTransport] = None):
        self.db = db
        self.local = LocalExecutor(docker, procs)
        # Only for tests: route remote calls to an in-process agent app.
        self.remote_transport = remote_transport
        # One lock per host: actions on the same machine (scheduler, UI, bulk jobs) run one after another,
        # while a slow start on one host (e.g. pulling an image) does not hold up the others.
        self._host_locks: dict[int, asyncio.Lock] = {}
        # Background jobs (e.g. restarting the copies of a recipe on several hosts), newest last.
        self.jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task] = set()
        self.local.transfers.transport = remote_transport  # copies from other machines (tests: in-process)
        # Seconds between asking a host how its pull / download is going.
        self.transfer_poll = 3.0
        # Self-metrics: (action, ok/error) -> how many times since the manager started.
        self.actions: Counter = Counter()

    # ---- hosts ---------------------------------------------------------

    def executor_for(self, host_id: int) -> LocalExecutor | RemoteExecutor:
        host = self.db.get_host(host_id)
        if host is None:
            raise ActionError(f"Host #{host_id} does not exist", 404)
        if not host["url"]:
            return self.local
        return RemoteExecutor(host, self.remote_transport)

    def _executor(self, recipe: dict) -> LocalExecutor | RemoteExecutor:
        return self.executor_for(recipe["host_id"])

    async def host_overview(self) -> list[dict[str, Any]]:
        """Every host with online state, platform info and memory/GPU readings (never the token)."""

        async def one(host: dict) -> dict[str, Any]:
            out = {"id": host["id"], "name": host["name"], "url": host["url"], "is_local": not host["url"],
                   "has_token": bool(host["token"]), "online": False, "info": None, "system": None, "error": None,
                   "transport": await asyncio.to_thread(transport_of, host["url"])}
            try:
                ex = self.executor_for(host["id"])
                out["info"], out["system"] = await asyncio.gather(ex.info(), ex.system())
                out["online"] = True
            except ActionError as e:
                out["error"] = e.message
            return out

        return list(await asyncio.gather(*(one(h) for h in self.db.list_hosts())))

    async def images_overview(self) -> dict[str, Any]:
        """Docker images used by recipes: which recipes use each, whether each of their hosts has it locally
        (age, size), and other local tags of the same repository there - e.g. a newer nightly to switch to."""
        recipes = [r for r in self.db.list_recipes() if r.get("runtime", "docker") == "docker" and r.get("image")]

        async def fetch(host: dict) -> tuple[int, Optional[list[dict]], Optional[str]]:
            try:
                return host["id"], await self.executor_for(host["id"]).images(), None
            except ActionError as e:
                return host["id"], None, e.message

        needed = {r["host_id"] for r in recipes}
        results = await asyncio.gather(*(fetch(h) for h in self.db.list_hosts() if h["id"] in needed))
        local = {hid: imgs for hid, imgs, _ in results if imgs is not None}
        errors = {hid: err for hid, _, err in results if err}

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for r in recipes:
            repo, tag = split_image(r["image"])
            g = groups.setdefault((repo, tag), {"image": r["image"].strip(), "repository": repo, "tag": tag,
                                                "recipes": [], "host_ids": set()})
            g["recipes"].append({"id": r["id"], "name": r["name"], "host_id": r["host_id"]})
            g["host_ids"].add(r["host_id"])

        images = []
        for (repo, tag), g in sorted(groups.items()):
            hosts = []
            for hid in sorted(g.pop("host_ids")):
                if hid not in local:
                    hosts.append({"host_id": hid, "state": "unknown", "error": errors.get(hid), "other_tags": []})
                    continue
                same_repo = [i for i in local[hid] if i["repository"] == repo]
                mine = next((i for i in same_repo if i["tag"] == tag), None)
                others = sorted((i for i in same_repo if i["tag"] != tag), key=lambda i: _created(i), reverse=True)
                hosts.append({"host_id": hid, "state": "present" if mine else "missing", "local": mine,
                              "other_tags": [{**i, "newer": bool(mine) and _created(i) > _created(mine)}
                                             for i in others]})
            images.append({**g, "hosts": hosts})
        native = sum(1 for r in self.db.list_recipes() if r.get("runtime") == "process")
        return {"images": images, "native_recipes": native}

    async def image_status(self, image: str, host_id: int) -> dict[str, Any]:
        """Whether one Docker image is present on one host (recipe editor checks this as you type)."""
        repo, tag = split_image(image)
        try:
            images = await self.executor_for(host_id).images()
        except ActionError as e:
            return {"image": image, "repository": repo, "tag": tag, "host_id": host_id,
                    "state": "unknown", "local": None, "other_tags": [], "error": e.message}
        same_repo = [i for i in images if i["repository"] == repo]
        mine = next((i for i in same_repo if i["tag"] == tag), None)
        others = sorted((i for i in same_repo if i["tag"] != tag), key=lambda i: _created(i), reverse=True)
        return {"image": image, "repository": repo, "tag": tag, "host_id": host_id,
                "state": "present" if mine else "missing", "local": mine,
                "other_tags": others, "error": None}

    async def copy_check(self, recipe: dict, host_ids: list[int]) -> list[dict[str, Any]]:
        """What copying a recipe to other hosts would meet there: whether its weights and image are on
        each host, other recipes on the same port, and a copy that is there already."""
        repo, tag = split_image(recipe.get("image") or "") if recipe.get("runtime") == "docker" else ("", "")
        recipes = self.db.list_recipes()

        async def one(hid: int) -> dict[str, Any]:
            out: dict[str, Any] = {"host_id": hid, "weights": None, "image": None, "error": None,
                                   "port_users": [x["name"] for x in recipes if x["host_id"] == hid
                                                  and x["host_port"] == recipe["host_port"]
                                                  and x["id"] != recipe["id"] and x.get("copy_of") != recipe["id"]],
                                   "existing_copy": next((x["id"] for x in recipes if x["host_id"] == hid
                                                          and x.get("copy_of") == recipe["id"]), None)}
            if recipe.get("weights_id"):
                out["weights"] = await self.check_weight(recipe["weights_id"], hid)
            if repo:
                try:
                    images = await self.executor_for(hid).images()
                    out["image"] = "present" if any(i["repository"] == repo and i["tag"] == tag for i in images) \
                        else "missing"
                except ActionError as e:
                    out["image"], out["error"] = "unknown", e.message
            return out

        return list(await asyncio.gather(*(one(h) for h in host_ids)))

    async def models_overview(self) -> dict[str, Any]:
        """Models referenced by model recipes that are not on registered weights: which recipes use each,
        and whether each of their hosts has the weights locally (Hugging Face cache, a mounted host path,
        or a model dir)."""
        groups: dict[str, dict[str, Any]] = {}
        for r in self.db.list_recipes():
            if r.get("kind", "model") != "model" or not (r.get("model") or "").strip():
                continue
            if r.get("weights_id") and uses_weights(r):
                continue  # on registered weights: the registry above it has their per-host paths
            target = resolve_model_target(r["model"], r.get("volumes", ""))
            ref = target["reference"]
            g = groups.setdefault(ref, {"model": ref, "kind": target["kind"], "target": target,
                                         "recipes": [], "host_ids": set()})
            g["recipes"].append({"id": r["id"], "name": r["name"], "host_id": r["host_id"]})
            g["host_ids"].add(r["host_id"])
            # Can move onto a registry entry (adopt_weights) when every recipe mounts the folder itself.
            g["adoptable"] = g.get("adoptable", True) and model_mount(r["model"], r.get("volumes", "")) is not None

        # Build per-host item lists: one resolved target per distinct reference on that host.
        by_host: dict[int, set[str]] = {}
        for ref, g in groups.items():
            for hid in g["host_ids"]:
                by_host.setdefault(hid, set()).add(ref)

        async def fetch(host: dict) -> tuple[int, list[dict[str, Any]], Optional[str]]:
            items = [groups[ref]["target"] for ref in sorted(by_host[host["id"]])]
            try:
                return host["id"], await self.executor_for(host["id"]).check_models(items), None
            except ActionError as e:
                return host["id"], [], e.message

        results = await asyncio.gather(*(fetch(h) for h in self.db.list_hosts() if h["id"] in by_host))
        presence = {hid: {d["reference"]: d for d in data} for hid, data, _ in results if data is not None}
        errors = {hid: err for hid, _, err in results if err}

        models = []
        for ref, g in sorted(groups.items()):
            hosts = []
            for hid in sorted(g.pop("host_ids")):
                d = presence.get(hid, {}).get(ref)
                if d is None:
                    hosts.append({"host_id": hid, "state": "unknown", "error": errors.get(hid), "location": None})
                elif d["present"]:
                    hosts.append({"host_id": hid, "state": "present", "error": None, "location": d.get("location")})
                else:
                    hosts.append({"host_id": hid, "state": "missing", "error": None, "location": d.get("expected")})
            g.pop("target")  # don't serialise the internal target dict
            models.append({**g, "hosts": hosts})
        return {"models": models}

    # ---- weights -------------------------------------------------------

    def _payload(self, recipe: dict) -> dict:
        """What an executor gets: the recipe with {weights} filled in for its host (older agents need
        nothing new for this, they only see the resolved paths)."""
        path = self.db.weight_path(recipe.get("weights_id"), recipe["host_id"])
        return agent_payload_recipe(fill_weights(recipe, path))

    async def _check_paths(self, host_id: int, paths: list[str]) -> tuple[dict[str, dict], Optional[str]]:
        """path -> {present, location, expected} on one host, or ({}, error) when the host can't tell."""
        items = [{"kind": "volume", "reference": p, "host_path": p} for p in paths]
        try:
            res = await self.executor_for(host_id).check_models(items)
        except ActionError as e:
            return {}, e.message
        return {d["reference"]: d for d in res}, None

    async def check_weight(self, weight_id: int, host_id: int) -> dict[str, Any]:
        """Whether a weights entry is usable on a host: "unset" (no path there), "present", "missing" or
        "unknown" (the host could not be asked)."""
        path = self.db.weight_path(weight_id, host_id)
        if not path:
            return {"host_id": host_id, "state": "unset", "path": None, "location": None, "error": None}
        found, err = await self._check_paths(host_id, [path])
        d = found.get(path)
        if d is None:
            return {"host_id": host_id, "state": "unknown", "path": path, "location": None, "error": err}
        return {"host_id": host_id, "state": "present" if d["present"] else "missing", "path": path,
                "location": d.get("location") or d.get("expected"), "error": None}

    async def weights_overview(self) -> dict[str, Any]:
        """Registered weights with, for every host, the path set there and whether it exists."""
        weights = self.db.list_weights()
        hosts = self.db.list_hosts()

        async def fetch(host: dict) -> tuple[int, dict[str, dict], Optional[str]]:
            paths = sorted({w["locations"][host["id"]] for w in weights if host["id"] in w["locations"]})
            if not paths:
                return host["id"], {}, None
            found, err = await self._check_paths(host["id"], paths)
            return host["id"], found, err

        results = {hid: (found, err) for hid, found, err in await asyncio.gather(*(fetch(h) for h in hosts))}
        out = []
        for w in weights:
            cells = []
            for h in hosts:
                path = w["locations"].get(h["id"])
                found, err = results[h["id"]]
                d = found.get(path) if path else None
                state = "unset" if not path else "unknown" if d is None else "present" if d["present"] else "missing"
                cells.append({"host_id": h["id"], "state": state, "path": path, "error": err if state == "unknown" else None,
                              "location": (d.get("location") or d.get("expected")) if d else None,
                              "size_gb": d.get("size_gb") if (d and d.get("present")) else None})
            out.append({"id": w["id"], "name": w["name"], "source": w["source"], "recipe_ids": w["recipe_ids"],
                        "hosts": cells})
        return {"weights": out}

    async def discover_weights(self) -> dict[str, Any]:
        """Ask every host for the model folders in its model dirs. Returns, per host, what it found;
        suggestions (a registered entry without a path on a host, and a folder there with the same name
        as the entry's folder elsewhere, its name or its source repo); and found folders nobody registered."""
        hosts = self.db.list_hosts()
        weights = self.db.list_weights()

        async def scan(host: dict) -> tuple[int, Optional[dict], Optional[str]]:
            try:
                return host["id"], await self.executor_for(host["id"]).scan_weights(), None
            except ActionError as e:
                return host["id"], None, e.message

        scans = await asyncio.gather(*(scan(h) for h in hosts))
        registered = {(hid, _norm_path(p)) for w in weights for hid, p in w["locations"].items()}
        keys = {w["id"]: {_norm_name(n) for n in [w["name"], w["source"].rsplit("/", 1)[-1],
                                                    *(p.rstrip("/").rsplit("/", 1)[-1] for p in w["locations"].values())] if n}
                for w in weights}
        out_hosts, suggestions, unregistered = [], [], []
        for hid, res, err in scans:
            found = (res or {}).get("found", [])
            out_hosts.append({"host_id": hid, "error": err, "dirs": (res or {}).get("dirs", []), "count": len(found)})
            for f in found:
                if (hid, _norm_path(f["path"])) not in registered:
                    unregistered.append({**f, "host_id": hid})
            for w in weights:
                if hid in w["locations"]:
                    continue
                match = next((f for f in found if _norm_name(f["name"]) in keys[w["id"]]), None)
                if match:
                    suggestions.append({"weight_id": w["id"], "name": w["name"], "host_id": hid,
                                        "path": match["path"], "size_gb": match.get("size_gb")})
        # Found folders matching a suggestion are not "unregistered" news as well.
        suggested = {(s["host_id"], s["path"]) for s in suggestions}
        unregistered = [f for f in unregistered if (f["host_id"], f["path"]) not in suggested]
        return {"hosts": out_hosts, "suggestions": suggestions, "unregistered": unregistered}

    def adopt_weights(self, recipe_ids: list[int], name: Optional[str] = None) -> dict[str, Any]:
        """Move recipes whose model is a volume mount of its own onto a registry entry: the mount's host
        side becomes {weights}, and its path is stored for the recipe's host. Recipes mounting the same
        folder name share one entry (found by name, created when missing)."""
        recipes = [self._get(rid) for rid in recipe_ids]
        mounts = {}
        for r in recipes:
            m = model_mount(r.get("model", ""), r.get("volumes", ""))
            if not m or WEIGHTS in m[1]:
                raise ActionError(f"{r['name']}: its model is not mounted from a folder of its own "
                                  f"(a volume like ~/models/x:{r.get('model') or '/model'})", 422)
            mounts[r["id"]] = m
        name = (name or "").strip() or mounts[recipes[0]["id"]][1].rstrip("/").rsplit("/", 1)[-1]
        weight = next((w for w in self.db.list_weights() if w["name"].lower() == name.lower()), None)
        wid = weight["id"] if weight else self.db.create_weight(name, "", {})
        locations = dict(weight["locations"]) if weight else {}
        for r in recipes:
            index, path = mounts[r["id"]]
            if r["host_id"] not in locations:
                locations[r["host_id"]] = path
                self.db.set_weight_location(wid, r["host_id"], path)
            elif _norm_path(locations[r["host_id"]]) != _norm_path(path):
                raise ActionError(f"{r['name']} mounts {path}, but '{name}' is already at "
                                  f"{locations[r['host_id']]} on that host", 409)
            self.db.update_recipe(r["id"], {**r, "volumes": use_weights_mount(r["volumes"], index), "weights_id": wid})
        return self.db.get_weight(wid)

    # ---- status --------------------------------------------------------

    async def _statuses(self, recipes: list[dict]) -> dict[int, dict[str, Any]]:
        """Status per recipe id, asking each host once. Unreachable hosts give status "offline"."""
        by_host: dict[int, list[dict]] = {}
        for r in recipes:
            by_host.setdefault(r["host_id"], []).append(r)

        async def one(host_id: int, rs: list[dict]) -> dict[int, dict[str, Any]]:
            try:
                return await self.executor_for(host_id).statuses([self._payload(r) for r in rs])
            except ActionError as e:
                return {r["id"]: {"status": "offline", "container": None, "error": e.message} for r in rs}

        out: dict[int, dict[str, Any]] = {}
        for part in await asyncio.gather(*(one(h, rs) for h, rs in by_host.items())):
            out.update(part)
        return out

    async def recipes_with_status(self) -> list[dict[str, Any]]:
        recipes = self.db.list_recipes()
        statuses = await self._statuses(recipes)
        self._check_start_times(statuses)
        for r in recipes:
            r.update(statuses.get(r["id"], {"status": "offline", "container": None}))
        return recipes

    def _check_start_times(self, statuses: dict[int, dict[str, Any]]) -> None:
        """Close pending start measurements once the model is ready (or note that it never was)."""
        for rid, (ts, source) in self.db.pending_starts().items():
            status = statuses.get(rid, {}).get("status")
            if status == "ready":
                try:
                    seconds = (datetime.now().astimezone() - datetime.fromisoformat(ts)).total_seconds()
                except ValueError:
                    seconds = -1
                if 0 < seconds < 24 * 3600:
                    self.db.add_start_time(rid, seconds, source)
                self.db.clear_pending_start(rid)
            elif status in ("stopped", "failed", "offline"):
                # Stopped or crashed before becoming ready: drop the measurement.
                self.db.clear_pending_start(rid)

    async def recipe_status(self, recipe_id: int) -> dict[str, Any]:
        recipe = self._get(recipe_id)
        recipe.update((await self._statuses([recipe]))[recipe_id])
        return recipe

    def _get(self, recipe_id: int) -> dict:
        recipe = self.db.get_recipe(recipe_id)
        if not recipe:
            raise ActionError("Recipe not found", 404)
        return recipe

    # ---- logs ----------------------------------------------------------

    async def logs(self, recipe: dict, tail: int = 500) -> str:
        return await self._executor(recipe).logs(self._payload(recipe), tail)

    def follow_logs(self, recipe: dict, tail: int = 200) -> AsyncIterator[str]:
        return self._executor(recipe).follow_logs(self._payload(recipe), tail)

    # ---- actions -------------------------------------------------------

    def _count(self, action: str, ok: bool) -> None:
        self.actions[(action, "ok" if ok else "error")] += 1

    def host_lock(self, host_id: int) -> asyncio.Lock:
        return self._host_locks.setdefault(host_id, asyncio.Lock())

    async def _on_hosts(self, host_id: Optional[int], fn) -> list[str]:
        """Run fn(host_id) under that host's lock: on one host, or on every host at the same time. Hosts
        that fail don't stop the others; their errors are raised together at the end."""
        host_ids = [host_id] if host_id is not None else [h["id"] for h in self.db.list_hosts()]

        async def one(hid: int) -> str:
            async with self.host_lock(hid):
                return await fn(hid)

        results = await asyncio.gather(*(one(h) for h in host_ids), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            for e in errors:
                if not isinstance(e, ActionError):
                    raise e
            if len(errors) == 1 and host_id is not None:
                raise errors[0]
            done = [r for r in results if isinstance(r, str) and r]
            raise ActionError("; ".join([*(e.message for e in errors), *done]), errors[0].status_code)
        return [r for r in results if r]

    async def _locked_recipe(self, recipe_id: int, fn, action: str) -> str:
        """fn(recipe) under the lock of the recipe's host (read again once the lock is held)."""
        host_id = self._get(recipe_id)["host_id"]
        async with self.host_lock(host_id):
            try:
                result = await fn(self._get(recipe_id))
            except ActionError:
                self._count(action, False)
                raise
            self._count(action, True)
            return result

    async def start(self, recipe_id: int, source: str = "user", force: bool = False) -> str:
        return await self._locked_recipe(recipe_id, lambda r: self._start(r, source, force), "start")

    async def stop(self, recipe_id: int, source: str = "user") -> str:
        return await self._locked_recipe(recipe_id, lambda r: self._stop(r, source), "stop")

    async def restart(self, recipe_id: int, source: str = "user", force: bool = False) -> str:
        """Stop, remove and re-create the container/process so edits to the recipe take effect."""
        return await self._locked_recipe(recipe_id, lambda r: self._restart(r, source, force), "restart")

    async def stop_all(self, source: str = "user", except_id: Optional[int] = None,
                       host_id: Optional[int] = None) -> str:
        try:
            msgs = await self._on_hosts(host_id, lambda hid: self._stop_all(source, except_id, hid))
        except ActionError:
            self._count("stop_all", False)
            raise
        self._count("stop_all", True)
        stopped = [m for m in msgs if m.startswith("Stopped")]
        return "; ".join(stopped) if stopped else msgs[0] if host_id is not None else "No other models running"

    async def switch(self, recipe_id: int, source: str = "user", drop_caches: bool = True) -> str:
        """Stop every other model on the same host, free memory there, then start this one.

        Agents on the host keep running: they use a model rather than compete with it for memory.
        """
        async def run(recipe: dict) -> str:
            if recipe.get("kind") == "agent":
                msg = f"{recipe['name']} is an agent: start it instead of switching to it"
                self.db.add_event(source, "switch", False, msg, recipe["id"], self._target(recipe))
                raise ActionError(msg, 409)
            msgs = [await self._stop_all(source, except_id=recipe_id, host_id=recipe["host_id"], keep_agents=True)]
            if drop_caches:
                msgs.append(await self._drop_caches(source, recipe["host_id"]))
            msgs.append(await self._start(recipe, source, force=False, action="switch"))
            return "; ".join(m for m in msgs if m)

        return await self._locked_recipe(recipe_id, run, "switch")

    async def free_memory(self, source: str = "user", host_id: Optional[int] = None) -> str:
        """Stop all models and drop the page cache, on one host or (host_id None) on all of them."""
        async def run(hid: int) -> str:
            msgs = [await self._stop_all(source, host_id=hid), await self._drop_caches(source, hid)]
            return "; ".join(m for m in msgs if m)

        try:
            msgs = await self._on_hosts(host_id, run)
        except ActionError:
            self._count("free_memory", False)
            raise
        self._count("free_memory", True)
        return "; ".join(msgs)

    # ---- background jobs: run on the hub, so closing the browser does not stop them -----------

    def _new_job(self, kind: str, title: str, items: list[dict[str, Any]], runner=None, whole=None) -> dict[str, Any]:
        """A background job: runner(item) for every item at once, or whole(items) deciding the order itself.
        Errors end up on their item; the job always finishes."""
        job = {"id": secrets.token_hex(4), "kind": kind, "title": title, "done": False, "items": items,
               "created": datetime.now().astimezone().isoformat(timespec="seconds")}
        self.jobs[job["id"]] = job
        while len(self.jobs) > 20:  # keep the last few for the dashboard
            self.jobs.pop(next(iter(self.jobs)))

        async def run() -> None:
            try:
                if whole:
                    await whole(items)
                else:
                    await asyncio.gather(*(self._guarded(job, i, runner) for i in items))
            finally:
                for i in items:
                    if i["state"] in ("waiting", "running"):
                        i["state"], i["message"] = "failed", i["message"] or "not done"
                job["done"] = True

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    @staticmethod
    async def _guarded(job: dict[str, Any], item: dict[str, Any], coro_fn) -> None:
        try:
            await coro_fn(item)
        except ActionError as e:
            item["state"], item["message"] = "failed", e.message
        except Exception as e:  # keep the other items going
            log.exception("job %s: %s failed", job["id"], item.get("name") or item["host_id"])
            item["state"], item["message"] = "failed", f"{type(e).__name__}: {e}"

    async def _follow(self, ex, task: dict[str, Any], item: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        """Ask the host how its transfer goes until it is done; its progress goes onto the item."""
        item["state"] = "running"
        lost = 0
        while task["state"] == "running":
            item["progress"] = prefix + (task.get("progress") or "")
            await asyncio.sleep(self.transfer_poll)
            try:
                task = await ex.transfer(task["id"])
                lost = 0
            except ActionError as e:
                if e.status_code == 404:
                    raise
                lost += 1  # the host is busy or the network hiccups: the transfer goes on there
                item["progress"] = f"{prefix}no answer from the host ({lost}×), still waiting"
                if lost * self.transfer_poll > 600:
                    raise ActionError(f"No answer from the host for 10 minutes: {e.message}", 502)
        item["progress"] = prefix + (task.get("progress") or "")
        if task["state"] != "ok":
            raise ActionError(task["message"] or "failed", 502)
        item["message"] = task["message"]
        return task

    def _host_items(self, host_ids: list[int]) -> list[dict[str, Any]]:
        for hid in host_ids:
            self.executor_for(hid)  # 404 for unknown hosts before anything starts
        return [{"host_id": hid, "name": "", "state": "waiting", "message": "", "progress": ""}
                for hid in dict.fromkeys(host_ids)]

    def start_transfer_job(self, kind: str, target: str, host_ids: list[int], name: str = "",
                           weight_id: Optional[int] = None, once: bool = False, hub_url: str = "") -> dict[str, Any]:
        """Pull an image ("pull") or download Hugging Face weights ("download") on several hosts at once.
        Each host runs its transfer itself; the hub only follows it. A finished download is registered in
        the Weights tab (entry weight_id, or one found / created by name). With `once`, only the first host
        downloads and the others copy from it over the local network (see start_copy_job)."""
        items = self._host_items(host_ids)
        state: dict[str, Any] = {"weight_id": weight_id}

        async def runner(item: dict[str, Any]) -> None:
            ex = self.executor_for(item["host_id"])
            task = await self._follow(ex, await ex.start_transfer(kind, target, name), item)
            if kind == "download":
                self._register_download(state, target, name, item["host_id"], task["path"])
            item["state"] = "ok"

        async def download_then_copy(all_items: list[dict[str, Any]]) -> None:
            first, rest = all_items[0], all_items[1:]
            for i in rest:
                i["progress"] = f"waiting for the download on {self._host_name(first['host_id'])}"
            await self._guarded(job, first, runner)
            if first["state"] != "ok":
                for i in rest:
                    i["state"], i["message"] = "failed", "the download it would copy from failed"
                return
            await self._distribute(job, state["weight_id"], rest, [first["host_id"]], hub_url)

        what = target if kind == "pull" else (name or target)
        title = f"{'Pulling' if kind == 'pull' else 'Downloading'} {what}"
        if kind == "download" and once and len(items) > 1:
            job = self._new_job(kind, title, items, whole=download_then_copy)
        else:
            job = self._new_job(kind, title, items, runner)
        return job

    def _register_download(self, state: dict[str, Any], repo: str, name: str, host_id: int, path: str) -> None:
        """Set the downloaded folder as the host's path of the weights entry (created on the first host)."""
        if not state["weight_id"]:
            wname = name or repo.rsplit("/", 1)[-1]
            w = next((w for w in self.db.list_weights() if w["name"].lower() == wname.lower()
                      or (w["source"] and w["source"] == repo)), None)
            state["weight_id"] = w["id"] if w else self.db.create_weight(wname, repo, {})
        self.db.set_weight_location(state["weight_id"], host_id, path)

    # ---- copying weights between hosts ---------------------------------------------------------

    def start_copy_job(self, weight_id: int, host_ids: list[int], hub_url: str = "") -> dict[str, Any]:
        """Copy registered weights to these hosts over the local network, straight from host to host (the
        hub only arranges it). Like a snowball: every host that has them serves one copy at a time, and
        each finished host becomes a source too - 5 machines are done in 3 rounds. Files are checked
        with SHA-256; files a host has already are skipped."""
        w = self.db.get_weight(weight_id)
        if not w:
            raise ActionError("Weights not found", 404)
        items = self._host_items(host_ids)
        job: dict[str, Any] = {}

        async def whole(all_items: list[dict[str, Any]]) -> None:
            await self._distribute(job, weight_id, all_items, None, hub_url)

        job = self._new_job("copy", f"Copying {w['name']}", items, whole=whole)
        return job

    async def _distribute(self, job: dict[str, Any], weight_id: int, items: list[dict[str, Any]],
                          sources: Optional[list[int]], hub_url: str) -> None:
        w = self.db.get_weight(weight_id)
        if sources is None:  # every host whose path exists (asked, not just registered)
            checks = await asyncio.gather(*(self.check_weight(weight_id, hid) for hid in w["locations"]))
            sources = [c["host_id"] for c in checks if c["state"] == "present"]
        targets = []
        for item in items:
            if item["host_id"] in sources:
                item["state"], item["message"] = "ok", "already there"
            else:
                targets.append(item)
        if not sources:
            for item in targets:
                item["state"], item["message"] = "failed", "No host has these weights yet: download them first"
            return
        free, pending, running = list(sources), list(targets), {}
        while pending or running:
            while free and pending:
                src, item = free.pop(0), pending.pop(0)
                t = asyncio.create_task(self._guarded(job, item, lambda i, s=src: self._copy_one(weight_id, s, i, hub_url)))
                running[t] = (src, item)
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                src, item = running.pop(t)
                if not item.get("source_failed"):
                    free.append(src)
                if item["state"] == "ok":
                    free.append(item["host_id"])  # the snowball: it can serve the next one
            if not free and pending and not running:
                for item in pending:
                    item["state"], item["message"] = "failed", "No working source left"
                break

    async def _copy_one(self, weight_id: int, src: int, item: dict[str, Any], hub_url: str) -> None:
        w = self.db.get_weight(weight_id)
        path = w["locations"][src]
        src_name = self._host_name(src)
        prefix = f"from {src_name}: "
        item["state"], item["progress"] = "running", f"{prefix}starting"
        url = self.db.get_host(src)["url"] or hub_url
        try:
            if not url:
                raise ActionError("The hub's own address is unknown: open the dashboard by the hub's network "
                                  "name or IP (not localhost), or set DGXM_PUBLIC_URL", 409)
            share = await self.executor_for(src).create_share(path)
        except ActionError:
            item["source_failed"] = True
            raise
        ex = self.executor_for(item["host_id"])
        name = path.rstrip("/").rsplit("/", 1)[-1]
        task = await ex.start_transfer("copy", url, name, {"id": share["id"], "ticket": share["ticket"]})
        task = await self._follow(ex, task, item, prefix)
        self.db.set_weight_location(weight_id, item["host_id"], task["path"])
        item["state"] = "ok"

    def start_restart_job(self, recipe_ids: list[int], source: str = "user", title: str = "") -> dict[str, Any]:
        """Restart these recipes in the background: hosts in parallel, one after another on the same host.
        Returns the job; GET it again (jobs[id]) to follow its progress."""
        items = []
        for rid in dict.fromkeys(recipe_ids):
            r = self._get(rid)
            items.append({"recipe_id": rid, "name": r["name"], "host_id": r["host_id"], "state": "waiting",
                          "message": "", "progress": ""})

        async def runner(item: dict[str, Any]) -> None:
            async def run(recipe: dict) -> str:
                item["state"] = "running"  # "waiting" until this host's lock is free
                return await self._restart(recipe, source, force=False)

            item["message"] = await self._locked_recipe(item["recipe_id"], run, "restart")
            item["state"] = "ok"

        return self._new_job("restart", title or "Restart", items, runner)

    # ---- internals (caller holds the host's lock) ----------------------

    async def _restart(self, recipe: dict, source: str, force: bool) -> str:
        await self._stop(recipe, source, record=False)
        return await self._start(recipe, source, force, action="restart")

    def _host_name(self, host_id: int) -> str:
        host = self.db.get_host(host_id)
        return host["name"] if host else f"#{host_id}"

    def _target(self, recipe: dict) -> str:
        if len(self.db.list_hosts()) > 1:
            return f"{self._host_name(recipe['host_id'])}: {recipe['container_name']}"
        return recipe["container_name"]

    async def _start(self, recipe: dict, source: str, force: bool, action: str = "start") -> str:
        target = self._target(recipe)
        ex = self._executor(recipe)

        def fail(msg: str, code: int):
            self.db.add_event(source, action, False, msg, recipe["id"], target)
            raise ActionError(msg, code)

        same_host = [r for r in self.db.list_recipes() if r["host_id"] == recipe["host_id"]]
        try:
            statuses = await self._statuses(same_host)
        except ActionError as e:
            fail(e.message, e.status_code)
        own = statuses[recipe["id"]]
        if own["status"] == "offline":
            fail(own.get("error") or f"Host {self._host_name(recipe['host_id'])} is offline", 502)
        if own["status"] in RUNNING_STATES:
            msg = f"{recipe['name']} is already running"
            self.db.add_event(source, action, True, msg, recipe["id"], target)
            return msg

        # Refuse to start if another recipe on the same host already holds the port.
        for other in same_host:
            if other["id"] != recipe["id"] and other["host_port"] == recipe["host_port"] \
                    and statuses[other["id"]]["status"] in RUNNING_STATES:
                fail(f"Port {recipe['host_port']} is in use by running recipe '{other['name']}'", 409)

        est = recipe.get("est_memory_gb")
        if est and not force:
            try:
                available = (await ex.system())["memory"]["available_gb"]
            except ActionError as e:
                fail(e.message, e.status_code)
            if available and est > available:
                fail(f"Not enough free memory for {recipe['name']}: needs ~{est:g} GB, "
                     f"{available:g} GB available. Stop other models or start with force.", 409)

        if uses_weights(recipe):
            await self._require_weights(recipe, fail)

        try:
            text = await ex.start(self._payload(recipe))
        except ActionError as e:
            fail(e.message, e.status_code)
        self.db.set_pending_start(recipe["id"], datetime.now().astimezone().isoformat(timespec="seconds"), source)
        self.db.add_event(source, action, True, text, recipe["id"], target)
        return text.split("\n", 1)[0]

    async def _require_weights(self, recipe: dict, fail) -> None:
        """Refuse to start a recipe whose {weights} has no path on its host, or whose path is not there."""
        host = self._host_name(recipe["host_id"])
        weight = self.db.get_weight(recipe["weights_id"]) if recipe.get("weights_id") else None
        if weight is None:
            fail(f"{recipe['name']} uses {{weights}} but has no weights selected: pick them in the recipe", 409)
        check = await self.check_weight(weight["id"], recipe["host_id"])
        if check["state"] == "unset":
            fail(f"Weights '{weight['name']}' have no path on {host}: set it in the Weights tab", 409)
        if check["state"] == "missing":
            fail(f"Weights '{weight['name']}' not found on {host} at {check['location'] or check['path']}: "
                 f"copy them there or fix the path in the Weights tab", 409)
        # "unknown" (e.g. an agent too old to check): let the container report it.

    async def _stop(self, recipe: dict, source: str, record: bool = True) -> str:
        target = self._target(recipe)
        status = (await self._statuses([recipe]))[recipe["id"]]
        if status["status"] not in RUNNING_STATES:
            msg = f"{recipe['name']} is not running"
            if status["status"] == "offline":
                msg = status.get("error") or f"Host {self._host_name(recipe['host_id'])} is offline"
            if record:
                self.db.add_event(source, "stop", status["status"] != "offline", msg, recipe["id"], target)
            return msg
        try:
            msg = await self._executor(recipe).stop(self._payload(recipe))
        except ActionError as e:
            self.db.add_event(source, "stop", False, e.message, recipe["id"], target)
            raise
        if record:
            self.db.add_event(source, "stop", True, msg, recipe["id"], target)
        return msg

    async def _stop_all(self, source: str, except_id: Optional[int] = None, host_id: Optional[int] = None,
                        keep_agents: bool = False) -> str:
        recipes = [r for r in self.db.list_recipes() if (host_id is None or r["host_id"] == host_id)
                   and not (keep_agents and r.get("kind") == "agent")]
        statuses = await self._statuses(recipes)
        stopped = []
        for recipe in recipes:
            if recipe["id"] != except_id and statuses[recipe["id"]]["status"] in RUNNING_STATES:
                await self._stop(recipe, source)
                stopped.append(recipe["name"])
        where = f" on {self._host_name(host_id)}" if host_id is not None else ""
        return f"Stopped {', '.join(stopped)}" if stopped else f"No other models running{where}"

    async def _drop_caches(self, source: str, host_id: int) -> str:
        name = self._host_name(host_id)
        try:
            ok, msg = await self.executor_for(host_id).drop_caches()
        except ActionError as e:
            ok, msg = False, e.message
        if len(self.db.list_hosts()) > 1:
            msg = f"{name}: {msg}"
        self.db.add_event(source, "drop_caches", ok, msg, target=name)
        return msg


def _norm_name(name: str) -> str:
    """Folder / model names compared loosely: "NVIDIA-Nemotron_VL" == "nvidia nemotron vl"."""
    return "".join(c for c in name.lower() if c.isalnum())


def _norm_path(path: str) -> str:
    return path.strip().rstrip("/")


def _created(image: dict) -> datetime:
    """Creation time of a local image for comparisons; unknown values sort as oldest."""
    try:
        value = datetime.fromisoformat(image.get("created") or "")
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
