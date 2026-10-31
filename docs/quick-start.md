# Quick start

This page sets up the manager on one machine and starts a first model. For more machines, continue with
[Several machines](several-machines.md) afterwards.

## Requirements

- Python 3.10 or newer.
- For Docker recipes: Docker, usable by your user without `sudo`, with the NVIDIA container runtime for
  GPUs.
- The model weights and Docker images you want to run, already on the machine.

## 1. Install

=== "Linux"

    Installs a systemd service that runs as your user.

    ```bash
    git clone https://github.com/einfachllm/local-llm-manager.git
    cd local-llm-manager
    ./install.sh
    ```

    The installer prints a generated password and stores it in `.env` in this folder.

=== "macOS"

    Installs a launchd agent. Docker on a Mac cannot use the GPU, so use native recipes (framework
    *Custom (native)*) with e.g. MLX (`pip install mlx-lm`) or llama.cpp (`brew install llama.cpp`). For importing start scripts, also
    run `brew install bash`.

    ```bash
    git clone https://github.com/einfachllm/local-llm-manager.git
    cd local-llm-manager
    ./install-macos.sh
    ```

    The installer prints a generated password and stores it in `.env`.

=== "Docker Compose"

    Linux only, and only for Docker recipes. Uses the image `ghcr.io/einfachllm/local-llm-manager:latest`
    (amd64 and arm64).

    ```bash
    git clone https://github.com/einfachllm/local-llm-manager.git
    cd local-llm-manager
    cp .env.example .env
    ```

    Set `DGXM_PASSWORD` in `.env` (at least 12 characters, e.g. from `openssl rand -base64 18`), then:

    ```bash
    docker compose up -d
    ```

    Add `--build` to build the image yourself.

## 2. Log in

Open `http://<machine>:8090` in a browser and log in as **admin** with the password from the installer
(or the one you set in `.env`).

To change the password, edit `DGXM_PASSWORD` in `.env` (at least 12 characters, no well-known passwords)
and restart the service.

## 3. Start your first model

1. Click **＋ Add recipe** (shortcut `i`), then **use Quick recipe** in the dialog (shortcut `q`).
2. Pick the **Model**: a folder or `.gguf` found in `DGXM_MODEL_DIR` on the host, registered weights, or a
   Hugging Face id such as `Qwen/Qwen3-8B`.
3. Pick **Settings from**: an engine default (vLLM, SGLang, llama.cpp, MLX on a Mac) or, once you have
   one, a recipe that already works - its image, flags, env and caches are copied, only the model, name,
   served model name and (optionally) the context length change.
4. Check the **Command** shown below, then **Create**.
5. Click **Start** on the new card. The status goes from *starting* to *loading* to *ready*.

Every field by hand: **＋ Add recipe** → **Start from scratch**.

!!! tip "Already have a start script?"
    **＋ Add recipe** opens the import dialog directly. Paste the script, `docker run` command or a
    compact JSON/YAML recipe file; scripts are checked for risky calls first and nothing in them is
    executed. In the editor, **Recipe file…** exports the current settings the same way.

When the model is ready, its OpenAI-compatible API is at the endpoint shown on the card:

```bash
curl http://<machine>:<port>/v1/models
```

## Next steps

- Run models on more than one machine: [Several machines](several-machines.md).
- Upgrading, uninstalling, schedules and all settings are described in the
  [README](https://github.com/einfachllm/local-llm-manager#readme).
