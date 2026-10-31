import asyncio
import os
from pathlib import Path

import pytest

from local_llm_manager import importer
from local_llm_manager.importer import import_script, recipe_from_argv
from local_llm_manager.recipes import build_run_argv

FIXTURES = Path(__file__).parent / "fixtures"


def run_import(text):
    return asyncio.run(import_script(text))


def test_starter_script_round_trip():
    text = (FIXTURES / "starter-qwen38-flash-next-fp4.sh").read_text()
    res = run_import(text)
    r = res["recipe"]
    assert r["framework"] == "vllm"
    assert r["image"] == "eugr/spark-vllm-b12x:latest"
    assert r["model"] == "/model"
    assert r["name"] == "qwen38-flash-next-fp4"
    assert r["container_name"] == "qwen38-flash-next"
    assert (r["host_port"], r["container_port"]) == (8081, 8081)
    assert r["command"] == "serve {model} --host 0.0.0.0 --port {port}"
    assert r["description"].startswith("Starter: nvidia/Qwen3.8-Flash-Next-FP4-Local")
    assert "--entrypoint vllm" in r["docker_args"] and "--network host" in r["docker_args"]
    assert "~/models/Qwen3.8-Flash-Next-FP4-Local:/model:ro" in r["volumes"].splitlines()
    assert "CUTE_DSL_ARCH=sm_121a" in r["env"].splitlines()
    assert any("--rm" in w for w in res["warnings"])

    # Computed values (yarn factor, MTP config) are resolved by running the script.
    assert '"factor":2,' in r["args"]
    assert "--speculative-config '{\"method\":\"mtp\",\"num_speculative_tokens\":4}'" in r["args"].splitlines()

    # Rebuilding gives exactly the container command of the script, and no -p with host networking.
    original = res["docker_argv"]
    rebuilt = build_run_argv({**r, "id": 1})
    img = r["image"]
    assert rebuilt[rebuilt.index(img) + 1:] == original[original.index(img) + 1:]
    assert "-p" not in rebuilt
    opts = lambda argv: sorted(a for a in argv[:argv.index(img)] if a not in ("--rm", "-d", "-p"))
    extra = {"docker", "run", "--label", "local-llm-manager=1", "local-llm-manager.recipe=1"}
    assert [a for a in opts(rebuilt) if a not in extra] == [os.path.expanduser(a) for a in opts(original)]


def test_preflight_probes_do_not_block_import():
    # Starter scripts often abort when their preflight probes fail (nvidia-smi missing, little
    # free memory). The sandbox reports tools as installed and the machine as idle, so the
    # import still reaches the `docker run`.
    text = """#!/usr/bin/env bash
# Starter: probe-heavy starter
set -euo pipefail
command -v nvidia-smi >/dev/null || { echo "ERROR: nvidia-smi not found" >&2; exit 1; }
BUSY="$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null || true)"
AVAIL=$(awk '/MemAvailable/ {printf "%.0f", $2 / 1048576}' /proc/meminfo)
[ "${AVAIL}" -gt 120 ] || { echo "ERROR: only ${AVAIL} GiB available" >&2; exit 1; }  # fake reports ~128
docker run --rm --gpus all -p 8123:8000 vllm/vllm-openai:latest --model org/m --port 8000
"""
    res = run_import(text)
    r = res["recipe"]
    assert r["framework"] == "vllm" and r["model"] == "org/m"
    assert (r["host_port"], r["container_port"]) == (8123, 8000)
    assert r["gpus"] == "all"


def test_plain_docker_run_command():
    text = """docker run -d --rm --gpus all -p 8200:8000 \\
      -e HF_TOKEN="" -e FOO=bar -v $HOME/.cache/huggingface:/root/.cache/huggingface \\
      --ipc=host vllm/vllm-openai:latest --model Qwen/Qwen3-8B --port 8000 --max-model-len=8192 \\
      --override-generation-config '{"temperature": 0.6}'"""
    res = run_import(text)
    r = res["recipe"]
    assert r["framework"] == "vllm" and r["model"] == "Qwen/Qwen3-8B"
    assert r["command"] == "--model {model} --port {port}"
    assert (r["host_port"], r["container_port"]) == (8200, 8000)
    assert r["env"].splitlines() == ["HF_TOKEN", "FOO=bar"]  # empty value -> pass-through
    assert r["volumes"] == "~/.cache/huggingface:/root/.cache/huggingface"
    assert r["args"].splitlines() == [
        "--max-model-len=8192", "--override-generation-config '{\"temperature\": 0.6}'"]
    assert r["name"] == "Qwen3-8B"


def test_vllm_openai_image_runs_serve_itself():
    # vllm/vllm-openai's entrypoint is `vllm serve`, so the first word after the image is the model.
    r, _ = recipe_from_argv([
        "--network", "host", "-v", "/m/q:/model:ro", "vllm/vllm-openai:v0.32.0", "/model",
        "--host", "0.0.0.0", "--port", "8000", "--served-model-name", "qwen3.8",
    ])
    assert r["framework"] == "vllm" and r["model"] == "/model"
    assert r["command"] == "{model} --host 0.0.0.0 --port {port}"
    rebuilt = build_run_argv({**r, "id": 1})
    assert rebuilt[rebuilt.index("vllm/vllm-openai:v0.32.0") + 1:][:2] == ["/model", "--host"]


@pytest.mark.parametrize("image, docker_args, dropped", [
    ("vllm/vllm-openai:v0.32.0", "", True),
    ("docker.io/vllm/vllm-openai", "", True),
    ("vllm/vllm-openai@sha256:abc", "", True),
    ("vllm/vllm-openai:latest", "--entrypoint vllm", False),  # then `serve` is needed again
    ("nvcr.io/nvidia/vllm:25.11-py3", "", False),
])
def test_preset_vllm_serve_dropped_only_where_the_image_runs_it(image, docker_args, dropped):
    recipe = {"id": 1, "container_name": "m", "image": image, "model": "/model", "host_port": 8000,
              "container_port": 8000, "command": "vllm serve {model} --host {bind_host} --port {port}",
              "docker_args": docker_args}
    argv = build_run_argv(recipe)
    after = argv[argv.index(image) + 1:]
    assert after[:2] == (["/model", "--host"] if dropped else ["vllm", "serve"])


def test_sglang_python_module():
    r, _ = recipe_from_argv([
        "--gpus", "all", "lmsysorg/sglang:spark", "python3", "-m", "sglang.launch_server",
        "--model-path", "Qwen/Qwen3-8B", "--host", "0.0.0.0", "--port", "30000", "--tp", "1",
    ])
    assert r["framework"] == "sglang"
    assert r["command"] == "python3 -m sglang.launch_server --model-path {model} --host 0.0.0.0 --port {port}"
    assert r["args"] == "--tp 1"
    assert (r["host_port"], r["container_port"]) == (30000, 30000)


def test_no_docker_run(client):
    res = client.post("/api/recipes/import", json={"text": "echo hello"})
    assert res.status_code == 422
    assert "neither ran `docker run`" in res.json()["detail"] and "hello" in res.json()["detail"]


def test_import_endpoint_avoids_name_clashes(client):
    text = "docker run --name m1 -p 9000:8000 vllm/vllm-openai --model org/m1"
    first = client.post("/api/recipes/import", json={"text": text}).json()["recipe"]
    assert client.post("/api/recipes", json=first).status_code == 201
    second = client.post("/api/recipes/import", json={"text": text}).json()
    assert second["recipe"]["name"] == "m1 (2)" and second["recipe"]["container_name"] == "m1-2"
    assert client.post("/api/recipes", json=second["recipe"]).status_code == 201


def test_native_mlx_script():
    text = """#!/usr/bin/env bash
# Starter: Qwen3 on the Mac Studio with MLX
MODEL="${MODEL:-mlx-community/Qwen3-30B-A3B-4bit}"
PORT="${PORT:-8085}"
source ~/venvs/mlx/bin/activate
exec mlx_lm.server --model "$MODEL" --host 0.0.0.0 --port "$PORT" --max-tokens 4096 --log-level INFO
"""
    res = run_import(text)
    r = res["recipe"]
    assert r["runtime"] == "process" and r["framework"] == "process-custom"
    assert r["model"] == "mlx-community/Qwen3-30B-A3B-4bit"
    assert r["command"] == "mlx_lm.server --model {model} --host 0.0.0.0 --port {port}"
    assert (r["host_port"], r["container_port"]) == (8085, 8085)
    assert r["args"].splitlines() == ["--max-tokens 4096", "--log-level INFO"]
    assert r["description"] == "Starter: Qwen3 on the Mac Studio with MLX"
    assert any("native process" in w for w in res["warnings"])


def test_native_llama_server_and_python_module():
    r = run_import("llama-server -m ~/models/q.gguf --port 8090 -ngl 999 -c 8192")["recipe"]
    assert r["framework"] == "process-custom" and r["model"] == "~/models/q.gguf"
    assert r["command"] == "llama-server -m {model} --port {port}" and r["args"].splitlines() == ["-ngl 999", "-c 8192"]

    r = run_import("python3 -m mlx_lm server --model m/x")["recipe"]
    assert r["framework"] == "process-custom" and r["command"] == "python3 -m mlx_lm server --model {model}"
    assert r["host_port"] == 8000  # process-custom default, no --port given


def test_native_import_saves_as_process_recipe(client):
    res = client.post("/api/recipes/import", json={"text": "mlx_lm.server --model m/y --port 8086"}).json()
    saved = client.post("/api/recipes", json=res["recipe"])
    assert saved.status_code == 201 and saved.json()["runtime"] == "process"


def test_script_with_failing_preflight_checks_still_imports():
    # Typical starter: `set -e`, checks for the model folder / nvidia-smi that fail on the dashboard's machine.
    text = (FIXTURES / "starter-nemotron35-super-vl-120b.sh").read_text()
    res = run_import(text)
    r = res["recipe"]
    assert r["image"].startswith("vllm/vllm-openai:nightly-") and r["container_name"] == "nemotron-super-vl"
    assert r["command"] == "serve {model} --host 0.0.0.0 --port {port}" and r["host_port"] == 8081
    assert "--limit-mm-per-prompt '{\"video\":1,\"image\":4}'" in r["args"].splitlines()
    assert "VLLM_ALLOW_LONG_MAX_MODEL_LEN=1" in r["env"].splitlines()
    assert "stopped before starting the model" in res["warnings"][0]
    assert "no config.json" in res["warnings"][0]


def test_lenient_retry_ignores_exit_and_set_e_only_when_needed():
    text = """set -euo pipefail
set -- --port 9300
[ -d /definitely/not/here ] || { echo "ERROR: model missing" >&2; exit 1; }
false
docker run --name t -p 9301:8000 vllm/vllm-openai --model org/m "$@"
"""
    res = run_import(text)
    assert res["recipe"]["args"] == "" and res["recipe"]["command"].endswith("--port {port}")
    assert res["recipe"]["container_port"] == 9300  # positional args from `set --` survived
    assert "ERROR: model missing" in res["warnings"][0]
    # A script that works on the first try gets no such warning.
    ok = run_import("docker run --name u -p 9302:8000 vllm/vllm-openai --model org/m")
    assert not any("stopped before" in w for w in ok["warnings"])


def test_missing_modern_bash_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(importer, "find_bash", lambda: None)
    with pytest.raises(importer.ScriptImportError, match="brew install bash"):
        asyncio.run(importer.import_script("docker run img"))


def test_mounts_and_manager_labels_are_integrated_into_the_recipe():
    text = """docker run -d --name m \
      --label local-llm-manager=1 --label local-llm-manager.recipe=7 --label team=llm \
      --mount type=bind,source=/models/qwen,target=/model,readonly \
      --mount type=volume,source=cache,target=/cache \
      -e HF_TOKEN vllm/vllm-openai --model /model --port 8000
"""
    res = run_import(text)
    r = res["recipe"]
    # Bind mounts become editable volume lines (so {weights} can replace the host path).
    assert r["volumes"] == "/models/qwen:/model:ro"
    # The manager adds its own labels; other labels and named volumes stay Docker arguments.
    assert "local-llm-manager" not in r["docker_args"]
    assert "--label team=llm" in r["docker_args"]
    assert "--mount type=volume,source=cache,target=/cache" in r["docker_args"]
    assert any("simple bind mounts" in w for w in res["warnings"])


def test_bind_mount_with_extra_options_stays_a_docker_argument():
    text = ("docker run --mount type=bind,source=/models/qwen,target=/model,"
            "bind-propagation=rshared img --model /model\n")
    r = run_import(text)["recipe"]
    assert r["volumes"] == ""
    assert ("--mount type=bind,source=/models/qwen,target=/model,"
            "bind-propagation=rshared") in r["docker_args"]
