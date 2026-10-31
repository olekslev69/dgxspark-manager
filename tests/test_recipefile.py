import json

import pytest

from conftest import recipe_body
from local_llm_manager.recipefile import (RecipeFileError, compact_recipe, dump_recipe_file,
                                          parse_recipe_file, recipe_from_file)
from local_llm_manager.recipes import FRAMEWORKS


def test_yaml_recipe_takes_missing_settings_from_the_preset():
    text = """
# Qwen3.8 on vLLM
name: qwen3.8
framework: vllm
image: vllm/vllm-openai:v0.32.0
weights: Qwen3.8-27B
port: 8101
args:
  - --gpu-memory-utilization 0.4
  - --max-model-len 226000
  - --speculative-config '{"method":"mtp","num_speculative_tokens":4}'
"""
    data = parse_recipe_file(text)
    assert data is not None
    weights = [{"id": 7, "name": "Qwen3.8-27B", "locations": {}}]
    r, warnings = recipe_from_file(data, weights)
    assert warnings == []
    assert r["runtime"] == "docker"
    assert r["model"] == "/model"
    assert r["weights_id"] == 7
    assert r["container_name"] == "qwen3.8"
    assert (r["host_port"], r["container_port"]) == (8101, 8000)
    assert r["command"] == FRAMEWORKS["vllm"]["command"]
    assert r["docker_args"] == FRAMEWORKS["vllm"]["docker_args"]
    assert r["env"] == FRAMEWORKS["vllm"]["env"]
    assert r["volumes"].splitlines() == ["{weights}:/model:ro",
                                         "~/.cache/huggingface:/root/.cache/huggingface"]
    assert r["args"].splitlines()[0] == "--gpu-memory-utilization 0.4"
    assert r["health_path"] == "/health"
    assert r["gpus"] == "all"


def test_json_recipe_keeps_every_complex_setting():
    data = parse_recipe_file(json.dumps({
        "name": "complex",
        "framework": "vllm",
        "runtime": "docker",
        "image": "eugr/spark-vllm-b12x:latest",
        "model": "/model",
        "weights": "complex-weights",
        "port": 8081,
        "command": "serve {model} --host {bind_host} --port {port}",
        "args": ["--served-model-name complex", "--max-model-len 8192"],
        "env": ["HF_TOKEN", "VLLM_USE_V1=1"],
        "volumes": ["{weights}:/model:ro", "~/.cache/vllm:/root/.cache/vllm"],
        "docker_args": ["--network host", "--ipc host", "--shm-size 16g",
                        "--ulimit memlock=-1", "--entrypoint vllm"],
        "gpus": "all",
    }))
    r, warnings = recipe_from_file(data, [{"id": 1, "name": "complex-weights"}])
    assert warnings == []
    assert r["weights_id"] == 1
    assert r["model"] == "/model"
    assert r["args"].splitlines() == ["--served-model-name complex", "--max-model-len 8192"]
    assert r["env"].splitlines() == ["HF_TOKEN", "VLLM_USE_V1=1"]
    assert r["volumes"].splitlines() == ["{weights}:/model:ro", "~/.cache/vllm:/root/.cache/vllm"]
    assert r["docker_args"].splitlines() == ["--network host", "--ipc host", "--shm-size 16g",
                                             "--ulimit memlock=-1", "--entrypoint vllm"]


def test_unregistered_weights_still_gives_a_draft():
    data = parse_recipe_file("name: x\nframework: vllm\nweights: Missing\n")
    r, warnings = recipe_from_file(data, [])
    assert r["model"] == "/model" and r["weights_id"] is None
    assert any("not registered" in w for w in warnings)


def test_parse_recipe_file_ignores_scripts():
    assert parse_recipe_file("docker run -d --name x img") is None
    assert parse_recipe_file("#!/usr/bin/env bash\necho hi") is None
    assert parse_recipe_file("") is None


def test_recipe_file_errors_are_clear():
    with pytest.raises(RecipeFileError, match="Unknown framework"):
        recipe_from_file({"framework": "nope"})
    with pytest.raises(RecipeFileError, match="host_port"):
        recipe_from_file({"framework": "vllm", "host_port": 0})


def test_compact_recipe_round_trip_keeps_a_complex_recipe():
    recipe = {
        "name": "qwen38-flash-next-fp4",
        "description": "Starter: nvidia/Qwen3.8-Flash-Next-FP4-Local",
        "runtime": "docker",
        "framework": "vllm",
        "image": "eugr/spark-vllm-b12x:latest",
        "model": "/model",
        "container_name": "qwen38-flash-next",
        "host_port": 8081,
        "container_port": 8081,
        "command": "serve {model} --host {bind_host} --port {port}",
        "args": "--served-model-name qwen38-flash-next-fp4\n--max-model-len 524288\n--kv-cache-dtype fp8",
        "env": "HF_TOKEN\nHF_HUB_OFFLINE=1\nVLLM_USE_V1=1",
        "volumes": ("{weights}:/model:ro\n~/.cache/huggingface:/root/.cache/huggingface\n"
                    "~/.cache/vllm:/root/.cache/vllm"),
        "docker_args": "--network host --ipc host --shm-size 16g --ulimit memlock=-1 --entrypoint vllm",
        "gpus": "all",
        "health_path": "/health",
        "kind": "model",
        "weights_id": 3,
    }
    weight = {"id": 3, "name": "Qwen3.8-Flash-Next-FP4-Local"}
    compact = compact_recipe(recipe, weight["name"])
    text = dump_recipe_file(compact, "yaml")
    assert "docker run" not in text
    assert compact["weights"] == weight["name"]
    assert compact["docker_args"].endswith("--entrypoint vllm")
    # The usual {weights}:/model:ro mount is implicit when weights: is given.
    assert "{weights}:/model:ro" not in compact["volumes"]
    assert compact["volumes"].splitlines() == ["~/.cache/huggingface:/root/.cache/huggingface",
                                               "~/.cache/vllm:/root/.cache/vllm"]

    again, warnings = recipe_from_file(parse_recipe_file(text), [weight])
    assert warnings == []
    for field in ("runtime", "framework", "image", "model", "container_name", "host_port",
                  "container_port", "command", "args", "env", "volumes", "docker_args", "gpus",
                  "health_path", "kind", "weights_id", "description"):
        assert again[field] == recipe[field], field


def test_compact_recipe_keeps_an_explicit_model_without_a_weights_mount():
    recipe = {
        "name": "no-mount", "framework": "vllm", "runtime": "docker",
        "image": FRAMEWORKS["vllm"]["image"], "model": "/model", "container_name": "no-mount",
        "host_port": 8100, "container_port": 8000, "command": FRAMEWORKS["vllm"]["command"],
        "args": "", "env": FRAMEWORKS["vllm"]["env"], "volumes": FRAMEWORKS["vllm"]["volumes"],
        "docker_args": FRAMEWORKS["vllm"]["docker_args"], "gpus": "all", "health_path": "/health",
        "kind": "model", "weights_id": 3,
    }
    compact = compact_recipe(recipe, "w")
    # Without the mount the model has to stay explicit, or importing would add one.
    assert compact["model"] == "/model"
    again, _ = recipe_from_file(parse_recipe_file(dump_recipe_file(compact, "yaml")),
                                [{"id": 3, "name": "w"}])
    assert again["model"] == "/model" and "{weights}" not in again["volumes"]


def test_import_and_export_endpoints(client):
    text = ("name: yaml-recipe\nframework: vllm\nimage: vllm/vllm-openai:v0.32.0\n"
            "model: org/model\nport: 8123\nargs: ['--max-model-len 4096']\n")
    res = client.post("/api/recipes/import", json={"text": text})
    assert res.status_code == 200
    draft = res.json()["recipe"]
    assert draft["name"] == "yaml-recipe" and draft["host_port"] == 8123
    assert client.post("/api/recipes/import/check", json={"text": text}).json()["count"] == 0

    saved = client.post("/api/recipes", json=draft)
    assert saved.status_code == 201
    rid = saved.json()["id"]

    exported = client.get(f"/api/recipes/{rid}/export?format=yaml")
    assert exported.status_code == 200 and "name: yaml-recipe" in exported.text
    as_json = client.get(f"/api/recipes/{rid}/export?format=json")
    assert json.loads(as_json.text)["name"] == "yaml-recipe"
    assert client.delete(f"/api/recipes/{rid}").status_code == 204

    # Export the unsaved draft and import it back: no name clash, same settings.
    round_trip = client.post("/api/recipes/export?format=yaml", json=draft)
    again = client.post("/api/recipes/import", json={"text": round_trip.text}).json()["recipe"]
    assert again["name"] == "yaml-recipe" and again["args"] == "--max-model-len 4096"

    from_form = client.post("/api/recipes/export?format=json", json=recipe_body(name="From form"))
    assert from_form.status_code == 200 and json.loads(from_form.text)["name"] == "From form"


def test_the_published_complex_starter_round_trips_through_a_recipe_file():
    import asyncio
    from pathlib import Path

    from local_llm_manager.importer import import_script

    text = (Path(__file__).parent.parent / "examples/recipes/qwen38-flash-next-fp4.sh").read_text()
    res = asyncio.run(import_script(text))
    recipe = res["recipe"]
    recipe["weights_id"] = 1
    recipe["kind"] = "model"
    recipe["volumes"] = recipe["volumes"].replace(
        "~/models/Qwen3.8-Flash-Next-FP4-Local:/model:ro", "{weights}:/model:ro")
    weight = {"id": 1, "name": "Qwen3.8-Flash-Next-FP4-Local"}

    compact = compact_recipe(recipe, weight["name"])
    again, warnings = recipe_from_file(parse_recipe_file(dump_recipe_file(compact, "yaml")), [weight])
    assert warnings == []
    for field in ("runtime", "framework", "image", "model", "container_name", "host_port",
                  "container_port", "command", "args", "env", "volumes", "docker_args", "gpus",
                  "health_path", "kind", "weights_id", "description"):
        assert again[field] == recipe[field], field


def test_published_yaml_examples_are_valid_recipe_files():
    from pathlib import Path

    folder = Path(__file__).parent.parent / "examples/recipes"
    files = sorted(folder.glob("*.yaml"))
    assert files
    for path in files:
        data = parse_recipe_file(path.read_text())
        assert data is not None, path.name
        weights = [{"id": 1, "name": data["weights"]}] if data.get("weights") else []
        recipe, warnings = recipe_from_file(data, weights)
        assert warnings == [], path.name
        assert recipe["framework"] in FRAMEWORKS


def test_yaml_flow_mapping_is_a_recipe_file():
    data = parse_recipe_file("{name: flow, framework: vllm, port: 8102}")
    assert data is not None and data["name"] == "flow"


def test_an_empty_model_with_weights_uses_the_implicit_mount():
    r, warnings = recipe_from_file({"framework": "vllm", "weights": "w", "model": ""},
                                   [{"id": 1, "name": "w"}])
    assert warnings == []
    assert r["model"] == "/model"
    assert "{weights}:/model:ro" in r["volumes"].splitlines()


def test_an_agent_recipe_does_not_need_a_model():
    r, warnings = recipe_from_file({"name": "bot", "framework": "custom", "kind": "agent",
                                    "image": "ubuntu:24.04", "command": "sleep infinity"})
    assert warnings == []
    assert r["kind"] == "agent" and r["model"] == ""


def test_an_agent_with_weights_does_not_get_an_implicit_model():
    r, warnings = recipe_from_file({"name": "bot", "framework": "custom", "kind": "agent",
                                    "weights": "w", "volumes": "{weights}:/data:ro"},
                                   [{"id": 1, "name": "w"}])
    assert warnings == []
    assert r["model"] == "" and r["weights_id"] == 1
    assert "model" not in compact_recipe(r, "w")


def test_exporting_an_unknown_framework_is_clear():
    with pytest.raises(RecipeFileError, match="Unknown framework"):
        compact_recipe({"name": "x", "framework": "gone", "host_port": 8000})


def test_exporting_a_saved_recipe_with_an_unknown_framework_is_a_422(client):
    rid = client.app.state.db.create_recipe(recipe_body(name="Old", framework="gone",
                                                       container_name="old"))
    res = client.get(f"/api/recipes/{rid}/export")
    assert res.status_code == 422
    assert "Unknown framework" in res.json()["detail"]


def test_a_weights_volume_target_sets_the_model_when_it_is_omitted():
    r, warnings = recipe_from_file({"framework": "vllm", "weights": "w",
                                    "volumes": "{weights}:/models:ro"},
                                   [{"id": 1, "name": "w"}])
    assert warnings == []
    assert r["model"] == "/models"
    assert r["volumes"] == "{weights}:/models:ro"


def test_an_explicit_model_with_a_weights_mount_at_another_target_warns():
    r, warnings = recipe_from_file({"framework": "vllm", "weights": "w", "model": "/custom",
                                    "volumes": "{weights}:/models:ro"},
                                   [{"id": 1, "name": "w"}])
    assert r["model"] == "/custom"
    assert any("not mounted" in w for w in warnings)
