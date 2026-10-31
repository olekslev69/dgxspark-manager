# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Compact recipe files (JSON/YAML):** Recipes → Import now accepts the editor's own fields as a JSON or
  YAML file, not just a starter script / `docker run` command. Missing settings come from the framework
  preset, so a file only lists what differs; `weights:` names a registered entry and `port:` sets the host
  port. Volumes, environment, extra Docker flags, command and server arguments are all fields, so even a
  complicated start script can be represented completely. **Recipe file…** in the editor exports the
  current recipe in the same format, ready for another manager or `examples/recipes`. Importing a
  `docker run` now turns a simple `--mount type=bind` into an editable volume line (more complex mounts
  stay Docker arguments), drops the manager's own labels, and warns about `--env-file` and non-bind
  mounts. New `GET /api/recipes/{id}/export` and `POST /api/recipes/export`.
- **Machines can ask to be added:** with `DGXM_HUB_URL` in an agent's `.env`, the agent announces itself to
  the hub, which lists it under **Hosts → Asking to be added** (with a counter on the Hosts tab). Approve
  it with **Codes match – add** when the hub shows the same join code as the machine (installer output,
  log, its start page); the code comes from the agent's token. Rejected machines stay blocked for a week,
  unanswered requests expire after an hour, and an approved agent reports a changed address itself. New
  `POST /api/join` (no login), `GET /api/join-requests`, `POST /api/join-requests/{id}/approve|reject`,
  and `DGXM_HUB_URL`, `DGXM_AGENT_URL`, `DGXM_AGENT_NAME`.
- **Copy weights between machines** (⇉ Copy… on a weights entry, and the new default of "Download on
  hosts": download once, copy to the others): machines copy straight from each other over the network,
  snowball-style (every finished machine serves the next one), with a SHA-256 check of every file, files
  already there skipped, interrupted files resumed, a free-space check first and GB / MB/s / time left in
  the jobs panel. The source only grants a read-only, ticket-protected share of that one folder for 6 hours
  (`/agent/v1/shares`); no SSH and no agent token leave the hub. Big files are served and read in 4 MB
  pieces over a plain socket (about 3-4x faster than before in tests). New `POST /api/jobs/copy` and
  `DGXM_PUBLIC_URL`.
- **Encryption shown per host:** the Hosts tab marks each host 🔒 Tailscale, 🔒 HTTPS or ⚠ plain HTTP, and
  the copy / download dialog warns when weights would travel unencrypted.
- **Pull and download on hosts:** "⤓ Pull…" on an image in the Images tab runs `docker pull` on the hosts
  you tick, all at the same time; "⬇ Download on hosts…" in the Weights tab (or "⬇ Download…" on an entry)
  downloads a Hugging Face model on each host into its `DGXM_MODEL_DIR` with `hf download` and registers
  the folders in the Weights tab. Each host runs its transfer itself in the background, so a flaky
  connection or a hub restart does not interrupt it; the hub follows the progress (pull output, GB so far)
  in the jobs panel. Needs the `hf` CLI on each host (`pip install -U huggingface_hub`, or `DGXM_HF_BIN`);
  gated models need `HF_TOKEN` in that host's `.env`. New endpoints `POST /api/jobs/pull`,
  `POST /api/jobs/download` and, on agents, `POST /agent/v1/transfers` / `GET /agent/v1/transfers/{id}`.
- **Background jobs:** restarting the copies of a recipe after a sync runs on the hub - all hosts at the
  same time, one after another on the same host - and keeps going when the browser is closed. A panel
  bottom left shows each copy (waiting / restarting / started / failed with the reason) and comes back after
  a reload. New endpoints `POST /api/jobs/restart`, `GET /api/jobs`, `GET /api/jobs/{id}`.
- **Copy to other hosts** (⇉ on a card): one recipe on several machines - e.g. every employee's DGX
  Spark. The dialog checks per host whether the weights and the Docker image are there and who else uses
  the port; each host gets a copy `<recipe> · <host>` with the same settings and port. Copies stay linked:
  cards show **✓ in sync** / **⟳ differs**, saving the original offers to update every copy (and to restart
  running ones), **sync** catches one up, **unlink** makes a copy a recipe of its own. New endpoints
  `POST /api/recipes/{id}/copies[/check]`, `/sync`, `/unlink`; recipes get a `copy_of` column.
- **⚡ Quick recipe** (shortcut `q`): a new model recipe in one dialog - pick the weights (found on the
  host, registered, or a Hugging Face id) and the settings to copy (a recipe that already works, or an
  engine default), optionally the context length. Image, flags, env and caches come from the base; the
  model is mounted via `{weights}`, the served model name follows the new name, and a recipe on the same
  host keeps its port so **Switch to** swaps the two. Found folders are registered in the Weights tab on
  create; flags that belong to one model (parsers, quantization, `--hf-overrides`, …) are pointed out.
  The dialog shows the resulting command before anything is saved (new `POST /api/recipes/quick`).
- **Native presets** "llama.cpp server (native)" (`llama-server`) and "MLX (native, Apple silicon)"
  (`mlx_lm.server`).
- **Documentation site** (MkDocs + Material) with a Quick start and a Several machines guide, published to
  GitHub Pages by a new `Docs` workflow: `main` updates the `dev` version, release tags add `stable`.
- **Weights registry:** the Weights tab registers model weights with their path on each host. A recipe
  picks an entry under *Weights* and writes `{weights}` (in volumes, model, command, args or env); on
  start it becomes the path on the recipe's host, so a recipe moved to another host mounts the right
  folder without edits. Picking an entry adds `{weights}:/model:ro` (or uses the model's existing
  mount). The editor checks the selected host right away (✓ present / ✗ no path / ✗ not found), the
  command preview shows the host's path, and starting is refused with a clear message when the host has
  no path or nothing is there. Agents need no update: they receive the resolved paths.
- **Finding weights:** "🔍 Search hosts" in the Weights tab lists the model folders and `.gguf` files in
  every host's `DGXM_MODEL_DIR` (new agent endpoint `GET /agent/v1/weights/scan`), suggests paths for
  registered entries by folder name ("Use" / "Use all"), and offers to register the rest. Recipes that
  mount their model folder directly get **→ Register** under *Used by recipes* (`POST /api/weights/adopt`),
  and an imported `docker run` that mounts a registered path is switched to that entry.
- **TensorFold preset** (native process): `tensorfold serve {model} --host {bind_host} --port {port}`,
  health check on `/health`, port 8080. Importing a `tensorfold serve ...` script picks the preset up.
- **Weights tab:** a new "Weights" tab (shortcut `6`) lists every model referenced by model recipes and,
  per host, whether its weights are present locally - mirroring the Images tab. A recipe's `{model}` is
  resolved to a host path when it is a container path backed by a volume mount (e.g.
  `/home/einfachllm/models/...`), to its Hugging Face hub-cache entry for `org/name` ids, or to a file
  under `DGXM_MODEL_DIR` (default `~/models`). Hosts report presence via the new agent endpoint
  `POST /agent/v1/models/check` (agents that predate it show "agent too old" until updated).
- **Agents apart from models:** a recipe is either a *model* (an LLM server) or an *agent* (a program that
  uses one, e.g. a Hermes gateway; new `kind` field, default `model`). The Recipes tab shows two sections,
  🤖 Agents and 🧠 Models, with their own counts and "+ New" buttons, and an All / Models / Agents filter.
  Agents are wide rows instead of tiles: running or not and since when, the model they use, no endpoint or
  engine metrics. **Switch to** keeps agents running and is not offered for them (the API answers 409);
  "stop all" and "free memory" still stop them. A new agent defaults to no GPUs and no health check.
  An agent card shows each passed-through secret name with ✓/✗ (whether it is set in the manager's
  environment on that host; values are never sent) and, for native agents, warns when another copy of
  the same command runs outside the manager (e.g. a detached second gateway on the same bot token).
  Grouped by host or tag, each group stacks its agents above its models, and its "+ New" buttons create
  the recipe on that host.
- **Model rows:** models are shown as wide rows like agents (🧠 icon, the card's facts side by side) by
  default; "Models as: Rows / Tiles" in the filter rail switches back to the card grid (remembered per
  browser).
- **Self-monitoring:** `GET /healthz` (hub and agent, no login) answers `ok` while the process is up and
  the database reachable (503 otherwise), and with `DGXM_METRICS=1` `GET /metrics` serves the manager's
  own metrics in Prometheus format (uptime, request/action counts, stored recipes/hosts) - no recipe or
  host data.
- **Activity-log retention:** events and start-time measurements older than `DGXM_RETENTION_DAYS`
  (default 90 days, `0` = keep all) are pruned on start-up, and the database records its schema version
  (`PRAGMA user_version`).
- [Configuration reference](docs/configuration.md): every `.env` key in one place - dashboard and network,
  hub/agent modes, models and recipes, storage, scheduler and monitoring, and how secret names are passed
  through to recipes.
- `examples/recipes`: paste-ready recipes for **Recipes → Import** - starting with
  `qwen38-flash-next-fp4.sh` (vLLM with b12x flags and tool calling on a DGX Spark); add your own as
  further `.sh` files.
- `examples/hermes-agent`: guide to run Hermes Agent (Telegram, local faster-whisper STT and Piper TTS) on an
  LLM recipe, with the Hermes gateway as a native-process recipe, tool-calling flags and security notes.
  The Docker variant now covers the container-network traps: `base_url` must use `host.docker.internal`
  (not `127.0.0.1`), `config.yaml` wins over recipe environment, and `model.key_env` must name the secret
  the recipe actually passes.
- **Images** tab (shortcut `5`): a matrix of the Docker images used by recipes (rows, tags of one image grouped
  together) and the hosts (columns): `✓ 23d` when the host has the image, `✗ missing`, and `⬆ tag` when a newer
  tag of the same image is already on that host - one click switches that host's recipes to it. Search, host
  filter, "Newer available" / "Missing on a host" filters and a summary; the recipes per image are collapsed
  into a count that expands per host. Scales to ~100 recipes on 5 hosts on one screen. Agents get
  `GET /agent/v1/images` (older agents are reported as too old to list images).

### Changed
- **One lock per host instead of one for the whole hub:** start, stop, restart and switch on one machine
  no longer wait for a slow action on another one (e.g. a start pulling a large image). Actions on the same
  machine still run one after another. "Stop all" and "Free memory" on every host now run on all hosts at
  once, and a host that is offline no longer stops the others from being handled.
- **Framework presets trimmed to what is actually used:** the TensorRT-LLM preset (Docker) and the
  MLX / llama.cpp (native) presets are gone. The native-process section now offers **TensorFold** and
  **Custom (native)**; imported `mlx_lm.server` / `llama-server` / `trtllm-serve` scripts and Mac
  defaults now land on Custom (native). Existing recipes using a removed framework key need a one-time
  framework re-save (the API rejects the old keys).
- **Recipe editor:** the command preview sits in its own column beside the form and stays in view while
  you scroll (stacked below it on narrow screens), shows docker's and the framework's flags in two colours,
  and has a Copy button. The Docker image field checks as you type whether the selected host already has
  the image (and which other tags of it it has). The Recipes tab now has a single **＋ Add recipe** button
  (shortcut `i`) that opens the import dialog; **Start from scratch** and **use Quick recipe** are links
  there, the editor head still has a highlighted import button, and a new recipe shows a prompt at the top.
- **Model cards:** "Model" shows the mounted folder's name instead of a container path like `/model`
  (full path in the tooltip), plus the `--served-model-name` clients use, the image (shortened) and a
  copy button for the endpoint. Start/Stop stay on the left; Duplicate and Delete are icon buttons.
- A stopped Docker container that exits with 137/143 (what `docker stop` usually produces) is now
  recognised as a clean stop only when the manager itself stopped it (recorded in a small state
  file next to the process logs). A container the kernel OOM-killed exits the same way but without
  that record, so it now shows **failed** instead of a misleading "stopped".

### Fixed
- **No doubled `vllm serve` with `vllm/vllm-openai`:** that image already runs `vllm serve` itself, so the
  vLLM preset's leading `vllm serve` is left out of its `docker run` (unless `--entrypoint` replaces it).
  Importing such a command now also recognises the first word after the image as the model.
- The command preview of an existing recipe showed an empty `local-llm-manager.recipe=` label; it now
  shows the recipe's id, as `start` sets it.
- The "KV" figure in a card's stats line no longer drops onto its own line (it picked up the layout of
  the card's fact list).
- On phones, the tab bar wraps instead of making the page wider than the screen.

## [0.1.0] - 2026-10-26

First public release.

### Features
- **Recipes** for model servers - vLLM, SGLang, TensorRT-LLM, llama.cpp, MLX or any image - as Docker
  containers or native processes (e.g. MLX on a Mac), with presets, a live `docker run` preview,
  placeholders (`{model}`, `{port}`, `{host_port}`, `{name}`, `{bind_host}`), tags, colours and pinning.
- **Import** a starter script or `docker run` command into a recipe: a static safety check first, then a
  restricted run that only records the server command.
- **Start, stop, restart, "switch to"** (stop the others on that machine, free memory, start) and duplicate.
- **Live status, logs and stats:** loading/ready/failed, tokens/s with sparkline, running requests,
  KV-cache usage from the server's `/metrics`, typical start time.
- **Schedules** (weekdays/time, cron, one-shot) with a planned-runs timeline, pause all and run now; the
  hub's time zone is shown.
- **Several machines, one dashboard:** a hub plus agents (Linux, macOS, DGX Spark), per-host ports and
  endpoints, host filter and grouping.
- **Tailscale-only mode:** `DGXM_HOST=tailscale` and `DGXM_PUBLISH_IP=tailscale` keep the dashboard,
  the agent API and the model ports inside your tailnet - also for use from a phone.
- Blur toggle for screenshots and screen shares, dark/light/auto themes, keyboard shortcuts.
- Installers for Linux (systemd) and macOS (launchd), Docker Compose, multi-arch image
  `ghcr.io/einfachllm/local-llm-manager` (amd64 + arm64), `pip install .`. All of them read the settings
  from `.env` in the install folder; the installers create it with a random password, check that the
  service started and print its address (also over Tailscale).

### Security
- HTTP Basic auth with a password policy (at least 12 characters, no well-known passwords, not
  containing the user name) and
  lockout after 10 failed logins in 10 minutes; the installers generate a random password and agent token.
- Agents require a token of at least 32 characters.
- Cross-site requests that change something are rejected (CSRF), host names are checked against DNS
  rebinding when no password is set, and security headers prevent framing.
- The database (it stores agent tokens) is readable by its owner only.
- See [SECURITY.md](https://github.com/einfachllm/local-llm-manager/blob/main/SECURITY.md) for the threat model and how to report vulnerabilities.

[Unreleased]: https://github.com/einfachllm/local-llm-manager/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/einfachllm/local-llm-manager/releases/tag/v0.1.0
