# Importable recipes

Ready-made recipes you paste into the manager instead of typing fields: open **Recipes → Import**, paste
a whole file from this folder, review the draft (name, port, paths) and create.

Two formats are accepted:

- **`.yaml` / `.json` - compact recipe files.** The manager's own fields: image, model, ports, `args`,
  `env`, `volumes`, `docker_args`, command, health path, tags. Missing settings come from the framework
  preset, so only what differs is written down. `weights:` names a registered Weights-tab entry (its
  per-host path fills `{weights}`); omit `model:` for the usual `/model` mount, or set it to another path.
  `host_port:` is the port on the host (`port:` is shorthand for it). `args`, `env`, `volumes` and `docker_args` can be multi-line
  strings or lists. Everything a complicated start script needs - bind mounts, environment, extra Docker
  flags, entrypoint, server arguments - has a field, so importing the file gives the same container again.
- **`.sh` - starter scripts / `docker run` commands.** The manager checks the script, runs it with bash in
  a restricted mode (only a fake `docker` and read-only text tools really run) and turns the captured
  `docker run` into the same fields. Use this for scripts that compute values or have preflight checks.

The first comment line of a script becomes the recipe's description. In the recipe editor,
**Recipe file…** exports the current recipe in the compact format, ready for another manager or for this
folder.

| File | What it is |
|---|---|
| [`qwen38-27b.yaml`](qwen38-27b.yaml) | Qwen3.8 27B served by vLLM - a small compact recipe with registered weights. |
| [`qwen38-27b-sglang.yaml`](qwen38-27b-sglang.yaml) | The same 27B model on SGLang: same port and served model name as the vLLM recipe. |
| [`qwen38-flash-next-fp4.yaml`](qwen38-flash-next-fp4.yaml) | Qwen3.8-Flash-Next-FP4 on an NVIDIA DGX Spark (GB10, SM121): b12x image, tuned flags, caches, tool calling. Compact form. |
| [`qwen3.8-flash-next-fp4-local.yaml`](qwen3.8-flash-next-fp4-local.yaml) | The same Flash weights on SGLang: conservative 32k context, own port 8110. |
| [`nemotron-3.5-super-vl.yaml`](nemotron-3.5-super-vl.yaml) | Nemotron-3.5-Super-VL with a draft model for speculative decoding; both models sit in one registered folder. |
| [`qwen38-flash-next-fp4.sh`](qwen38-flash-next-fp4.sh) | The same model as a `docker run` starter script. |
| [`hermes-agent-docker.yaml`](hermes-agent-docker.yaml) | A Hermes Agent gateway as an agent recipe: its own port and data volume, no model or GPU. |

## Before you start

- **Weights:** compact recipes name a registered entry (`weights:`), e.g. `Qwen3.8-Flash-Next-FP4-Local`.
  Create it in the **Weights** tab with the folder on each host that runs the recipe, or change the name in
  the file. If a main model and a draft model share a folder, register that folder once, mount it, and point
  `model:` at the subfolder (see `nemotron-3.5-super-vl.yaml`). Starter scripts mount a concrete folder;
  download the model there, or point the volume at your copy. "Search hosts" in the **Weights** tab finds
  model folders under `DGXM_MODEL_DIR` and registers them.
- **Ports:** the port in `host_port:` / `port:` / `--port` becomes the recipe's port. Keep it the **same across models**
  you want to switch between, and give each `--served-model-name` a stable short name - then **Switch to**
  changes the model and your agents keep the same endpoint `http://<host>:<port>/v1`.
- **Secrets:** files list only bare names (e.g. `HF_TOKEN`); put the values in the manager's `.env` (see
  [docs/configuration.md](../../docs/configuration.md)).
- **vLLM images:** the preset command starts with `vllm serve`. For `vllm/vllm-openai` (whose entrypoint
  already runs `vllm serve`), the manager drops the duplicate; images with a plain `vllm` entrypoint keep
  `serve`. The preview shows the actual command.
- Recipes are a starting point: the flags are tuned for the machine they ran on. Check
  `--gpu-memory-utilization`, `--max-model-len` and `--max-num-seqs` against your GPU's memory.

More of your own? Drop new `.yaml` / `.json` files here and add a row to the table - or export one from
the editor with **Recipe file…**.
