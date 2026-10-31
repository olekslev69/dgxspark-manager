import asyncio

import pytest
from conftest import recipe_body
from test_hosts import setup  # noqa: F401  (hub + "mac" agent fixture)

from local_llm_manager import docker as docker_mod
from local_llm_manager.docker import Docker, split_image


@pytest.mark.parametrize("ref,expected", [
    ("vllm/vllm-openai:nightly-3f1bcfd8", ("vllm/vllm-openai", "nightly-3f1bcfd8")),
    ("nvcr.io/nvidia/vllm:25.11-py3", ("nvcr.io/nvidia/vllm", "25.11-py3")),
    ("docker.io/lmsysorg/sglang:spark", ("lmsysorg/sglang", "spark")),
    ("busybox", ("busybox", "latest")),
    ("docker.io/library/busybox:1.36", ("busybox", "1.36")),
    ("localhost:5000/team/llm", ("localhost:5000/team/llm", "latest")),
    ("vllm/vllm-openai@sha256:abc", ("vllm/vllm-openai", "@sha256:abc")),
])
def test_split_image(ref, expected):
    assert split_image(ref) == expected


def test_list_images_parses_docker_output(monkeypatch):
    out = "\n".join([
        '{"Repository":"vllm/vllm-openai","Tag":"v0.11.0","ID":"sha256:0123456789abcdef","CreatedAt":"2026-10-20 14:03:11 +0200 CEST","Size":"18.3GB"}',
        '{"Repository":"<none>","Tag":"<none>","ID":"sha256:dead","CreatedAt":"2026-10-01 10:00:00 +0000 UTC","Size":"1GB"}',
        "not json",
    ])

    async def fake_run(argv, timeout=60.0):
        assert argv[1:3] == ["image", "ls"]
        return docker_mod.CommandResult(0, out, "")

    monkeypatch.setattr(docker_mod, "run", fake_run)
    images = asyncio.run(Docker("docker").list_images())
    assert images == [{"repository": "vllm/vllm-openai", "tag": "v0.11.0", "id": "456789abcdef",
                       "created": "2026-10-20T14:03:11+02:00", "size": "18.3GB"}]


def img(repo, tag, created, size="10GB"):
    return {"repository": repo, "tag": tag, "id": tag[:12], "created": created, "size": size}


def test_images_overview_across_hosts(setup):  # noqa: F811
    client, _, hub_docker, mac_docker, mac = setup
    hub_docker.images = [
        img("vllm/vllm-openai", "v0.11.0", "2026-10-01T10:00:00+00:00"),
        img("vllm/vllm-openai", "nightly-new", "2026-10-25T10:00:00+00:00"),
        img("vllm/vllm-openai", "v0.10.0", "2026-09-01T10:00:00+00:00"),
        img("lmsysorg/sglang", "spark", "2026-10-10T10:00:00+00:00"),
    ]
    mac_docker.images = []
    a = client.post("/api/recipes", json=recipe_body(name="A", image="docker.io/vllm/vllm-openai:v0.11.0")).json()
    client.post("/api/recipes", json=recipe_body(name="B", image="vllm/vllm-openai:v0.11.0", host_port=8101))
    client.post("/api/recipes", json=recipe_body(name="C", image="vllm/vllm-openai:v0.11.0", host_id=mac["id"]))
    client.post("/api/recipes", json={**recipe_body(name="N"), "runtime": "process", "framework": "process-custom",
                                      "image": "", "command": "mlx_lm.server --port {port}"})

    data = client.get("/api/images").json()
    assert data["native_recipes"] == 1
    [vllm] = [i for i in data["images"] if i["repository"] == "vllm/vllm-openai"]
    assert vllm["tag"] == "v0.11.0" and sorted(r["name"] for r in vllm["recipes"]) == ["A", "B", "C"]
    hub_row, mac_row = sorted(vllm["hosts"], key=lambda h: h["host_id"])
    assert hub_row["state"] == "present" and hub_row["local"]["created"].startswith("2026-10-01")
    tags = {t["tag"]: t["newer"] for t in hub_row["other_tags"]}
    assert tags == {"nightly-new": True, "v0.10.0": False}
    assert hub_row["other_tags"][0]["tag"] == "nightly-new"  # newest first
    assert mac_row["host_id"] == mac["id"] and mac_row["state"] == "missing" and mac_row["other_tags"] == []
    assert a["image"] == "docker.io/vllm/vllm-openai:v0.11.0"  # the recipe itself is unchanged


def test_images_overview_reports_unreachable_host(setup, monkeypatch):  # noqa: F811
    client, _, hub_docker, mac_docker, mac = setup

    async def broken():
        raise RuntimeError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(mac_docker, "list_images", broken)
    client.post("/api/recipes", json=recipe_body(name="C", image="vllm/vllm-openai:v0.11.0", host_id=mac["id"]))
    [row] = client.get("/api/images").json()["images"][0]["hosts"]
    assert row["state"] == "unknown" and "Docker daemon" in row["error"]


def test_image_check_endpoint(setup):  # noqa: F811
    client, _, hub_docker, mac_docker, mac = setup
    local = client.get("/api/hosts").json()[0]
    hub_docker.images = [img("lmsysorg/sglang", "qwen38-27b", "2026-10-01T10:00:00+00:00")]
    data = client.get("/api/images/check", params={"host_id": local["id"], "image": "lmsysorg/sglang:qwen38-27b"}).json()
    assert data["state"] == "present" and data["local"]["tag"] == "qwen38-27b"
    data = client.get("/api/images/check", params={"host_id": local["id"], "image": "lmsysorg/sglang:spark"}).json()
    assert data["state"] == "missing" and [t["tag"] for t in data["other_tags"]] == ["qwen38-27b"]
    mac_docker.images = [img("lmsysorg/sglang", "spark", "2026-10-02T10:00:00+00:00")]
    data = client.get("/api/images/check", params={"host_id": mac["id"], "image": "lmsysorg/sglang:spark"}).json()
    assert data["state"] == "present"
    assert client.get("/api/images/check", params={"host_id": 999, "image": "busybox"}).status_code == 404
    assert client.get("/api/images/check", params={"host_id": local["id"], "image": "not an image"}).status_code == 422


def test_image_check_reports_unreachable_host(setup, monkeypatch):  # noqa: F811
    client, _, _, mac_docker, mac = setup

    async def broken():
        raise RuntimeError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(mac_docker, "list_images", broken)
    data = client.get("/api/images/check", params={"host_id": mac["id"], "image": "vllm/vllm-openai:v0.32.0"}).json()
    assert data["state"] == "unknown" and "Docker daemon" in data["error"]
