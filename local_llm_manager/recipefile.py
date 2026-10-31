"""Compact recipe files: a recipe as JSON or YAML instead of a long `docker run` command.

A file uses the same fields as the recipe editor. Settings missing from the file come from the
framework preset, so only what differs has to be written down - but everything a complicated start
script needs (volumes, environment, extra Docker flags, command and server arguments) has a field.
`args`, `env`, `volumes` and `docker_args` may be a multi-line string or a list of lines. `weights`
names a registered Weights-tab entry (its per-host path fills `{weights}`) and `port` is shorthand
for the host port.
"""

import json
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is installed with the manager
    yaml = None

from .recipes import CARD_COLORS, FRAMEWORKS, KINDS, WEIGHTS, slug

RECIPE_FILE_KEYS = frozenset({
    "name", "description", "runtime", "framework", "image", "model", "container_name",
    "host_port", "container_port", "port", "command", "args", "env", "volumes", "docker_args",
    "gpus", "health_path", "est_memory_gb", "tags", "color", "pinned", "kind", "weights",
    "weights_id",
})


class RecipeFileError(ValueError):
    """A recipe file that cannot be turned into a recipe."""


def parse_recipe_file(text: str) -> Optional[dict[str, Any]]:
    """The mapping when `text` is a JSON/YAML recipe file, otherwise None (e.g. a shell script)."""
    stripped = (text or "").lstrip("\ufeff \t\r\n")
    if not stripped:
        return None
    data: Any = None
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            pass
    if data is None and yaml is not None:
        try:
            data = yaml.safe_load(stripped)
        except yaml.YAMLError:
            return None
    if data is None:
        return None
    if not isinstance(data, dict) or not (set(data) & RECIPE_FILE_KEYS):
        return None
    return data


def _text(value: Any, key: str) -> str:
    """A text field: a string as is, or a list of lines joined with newlines."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        if not all(isinstance(v, (str, int, float)) for v in value):
            raise RecipeFileError(f"{key} must be a string or a list of strings")
        return "\n".join(str(v) for v in value)
    raise RecipeFileError(f"{key} must be a string or a list of strings")


def _int(value: Any, key: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RecipeFileError(f"{key} must be a number")
    if isinstance(value, float) and not value.is_integer():
        raise RecipeFileError(f"{key} must be a whole number")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RecipeFileError(f"{key} must be a number")
    if not 1 <= number <= 65535:
        raise RecipeFileError(f"{key} must be between 1 and 65535")
    return number


def _float(value: Any, key: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RecipeFileError(f"{key} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RecipeFileError(f"{key} must be a number")
    if number < 0:
        raise RecipeFileError(f"{key} must not be negative")
    return number


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        raise RecipeFileError("tags must be a list or a comma-separated string")
    out: list[str] = []
    for tag in value:
        text = str(tag).strip()
        if text and text not in out:
            out.append(text)
    return out


def _find_weight(data: dict[str, Any], weights: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], str]:
    """The registered weights entry named by `weights`/`weights_id`, and the name given in the file."""
    name = str(data.get("weights") or "").strip()
    if name:
        return next((w for w in weights if str(w.get("name", "")).lower() == name.lower()), None), name
    if data.get("weights_id") not in (None, ""):
        wanted = str(data["weights_id"]).strip()
        found = next((w for w in weights if str(w.get("id")) == wanted), None)
        return found, str((found or {}).get("name") or "")
    return None, ""


def recipe_from_file(data: dict[str, Any], registered_weights: Optional[list[dict[str, Any]]] = None
                     ) -> tuple[dict[str, Any], list[str]]:
    """Build an unsaved recipe from a recipe file. Missing fields come from the framework preset.

    Returns (recipe, warnings for the user). `registered_weights` is `db.list_weights()`; a
    `weights:` name that is not there still produces a usable draft, with a warning.
    """
    warnings: list[str] = []
    unknown = sorted(str(key) for key in set(data) - RECIPE_FILE_KEYS)
    if unknown:
        warnings.append("Ignored unknown field(s): " + ", ".join(unknown))

    framework = str(data.get("framework") or "custom").strip().lower()
    if framework not in FRAMEWORKS:
        raise RecipeFileError(f"Unknown framework “{framework}”: use one of {', '.join(sorted(FRAMEWORKS))}")
    preset = FRAMEWORKS[framework]
    runtime = str(data.get("runtime") or preset["runtime"]).strip().lower()
    if runtime not in ("docker", "process"):
        raise RecipeFileError("runtime must be “docker” or “process”")

    kind = str(data.get("kind") or "model").strip().lower()
    if kind not in KINDS:
        raise RecipeFileError(f"kind must be one of {', '.join(KINDS)}")
    color = str(data.get("color") or "").strip().lower()
    if color and color not in CARD_COLORS:
        raise RecipeFileError(f"color must be one of {', '.join(c for c in CARD_COLORS if c)} or empty")

    weight, weights_name = _find_weight(data, registered_weights or [])
    if data.get("weights") and weight is None:
        warnings.append(f"Weights “{data['weights']}” are not registered on this manager: add them in the "
                        "Weights tab and pick them in the recipe.")
    elif data.get("weights_id") not in (None, "") and weight is None:
        warnings.append(f"Weights id {data['weights_id']} does not exist on this manager: pick the weights again.")

    def field(key: str) -> str:
        return _text(data[key], key) if key in data else str(preset.get(key, "") or "")

    model_given = "model" in data
    model = _text(data["model"], "model") if model_given else ""
    if not model.strip() and (weight or weights_name) and kind != "agent":
        model = "/model" if runtime == "docker" else WEIGHTS
        model_given = False
    if not model and kind != "agent":
        warnings.append("No model or weights set: check the Model field.")

    volumes = field("volumes")
    weights_mounts = [line.strip() for line in volumes.splitlines()
                      if line.strip().split(":", 1)[0] == WEIGHTS]

    def mount_target(line: str) -> str:
        parts = line.split(":", 2)
        return parts[1].rstrip("/") if len(parts) > 1 else ""

    def mounted_at(target: str, path: str) -> bool:
        """The model is the mount target or lies inside it (e.g. /models/main under /models)."""
        target, path = target.rstrip("/"), path.rstrip("/")
        return bool(target) and (path == target or path.startswith(target + "/"))

    if ((weight or weights_name) and runtime == "docker" and not model_given
            and kind != "agent" and weights_mounts):
        # `weights:` plus a {weights} volume but no model: use the volume's target.
        target = next((t for t in map(mount_target, weights_mounts) if t), "")
        model = target or model
    if (weight or weights_name) and runtime == "docker" and model.startswith("/"):
        mounted = any(mounted_at(mount_target(line), model) for line in weights_mounts)
        if not mounted and not model_given:
            # `weights:` alone means the usual {weights}:/model:ro mount.
            volumes = "\n".join([f"{WEIGHTS}:{model}:ro"] + volumes.splitlines())
        elif not mounted:
            warnings.append(f"The model is not mounted from {{weights}}: add a volume like "
                            f"{WEIGHTS}:{model}:ro.")

    name = str(data.get("name") or "").strip()
    if not name:
        name = (weight or {}).get("name") or weights_name or model.rstrip("/").rsplit("/", 1)[-1]
    name = (name or "Imported recipe")[:100]

    container_port = _int(data.get("container_port"), "container_port", int(preset["container_port"]))
    host_port = _int(data.get("host_port", data.get("port")), "host_port", container_port)
    if "port" in data and "host_port" in data and str(data["port"]) != str(data["host_port"]):
        warnings.append("Both port and host_port are set: host_port wins.")

    gpus = (_text(data["gpus"], "gpus").strip() if "gpus" in data
            else ("all" if runtime == "docker" and kind != "agent" else ""))

    recipe = {
        "runtime": runtime,
        "name": name,
        "description": _text(data.get("description"), "description").strip(),
        "framework": framework,
        "image": field("image"),
        "model": model,
        "container_name": str(data.get("container_name") or "").strip() or slug(name),
        "host_port": host_port,
        "container_port": container_port,
        "command": field("command"),
        "args": field("args"),
        "env": field("env"),
        "volumes": volumes,
        "docker_args": field("docker_args"),
        "gpus": gpus,
        "health_path": field("health_path"),
        "est_memory_gb": _float(data.get("est_memory_gb"), "est_memory_gb"),
        "tags": _tags(data.get("tags")),
        "color": color,
        "pinned": _bool(data.get("pinned", False)),
        "kind": kind,
        "weights_id": (weight or {}).get("id"),
    }
    return recipe, warnings


def _lines(text: Any) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _normalized(text: Any) -> str:
    return "\n".join(_lines(text))


def compact_recipe(recipe: dict[str, Any], weights_name: Optional[str] = None) -> dict[str, Any]:
    """The recipe as a compact file: everything that differs from its framework preset.

    `weights_name` is the registered entry's name, exported as `weights:` instead of a
    machine-specific id. The result is lossless for the manager: importing it again gives the
    same recipe, with omitted fields filled from the preset.
    """
    framework = str(recipe.get("framework") or "custom")
    if framework not in FRAMEWORKS:
        raise RecipeFileError(f"Unknown framework “{framework}”: use one of {', '.join(sorted(FRAMEWORKS))}")
    preset = FRAMEWORKS[framework]
    runtime = str(recipe.get("runtime") or preset["runtime"])
    name = str(recipe.get("name") or "Recipe")
    model = str(recipe.get("model") or "")

    out: dict[str, Any] = {"name": name}
    if recipe.get("description"):
        out["description"] = recipe["description"]
    out["framework"] = framework
    if runtime != preset["runtime"]:
        out["runtime"] = runtime

    for key in ("image", "command", "health_path", "docker_args"):
        value = str(recipe.get(key) or "")
        if value != str(preset.get(key) or ""):
            out[key] = value
    if _normalized(recipe.get("args")) != _normalized(preset.get("args")):
        out["args"] = _normalized(recipe.get("args"))
    if _normalized(recipe.get("env")) != _normalized(preset.get("env")):
        out["env"] = _normalized(recipe.get("env"))

    default_model = "/model" if runtime == "docker" else WEIGHTS
    implicit_mount = ""
    if weights_name and runtime == "docker" and model.startswith("/"):
        implicit_mount = f"{WEIGHTS}:{model}:ro"
    volumes = _lines(recipe.get("volumes"))
    has_implicit_mount = bool(implicit_mount and implicit_mount in volumes)
    # The model can be omitted only when importing derives the same model and adds the mount.
    omit_model = bool(weights_name and model == default_model and recipe.get("kind") != "agent"
                      and (runtime != "docker" or has_implicit_mount))
    if omit_model and implicit_mount:
        volumes.remove(implicit_mount)
    if volumes != _lines(preset.get("volumes")):
        out["volumes"] = "\n".join(volumes)

    out["host_port"] = recipe["host_port"]
    if int(recipe.get("container_port") or 0) != int(preset.get("container_port") or 0):
        out["container_port"] = recipe["container_port"]

    if weights_name:
        out["weights"] = weights_name
        if model and not omit_model:
            out["model"] = model
    elif model:
        out["model"] = model

    default_gpus = "all" if runtime == "docker" and recipe.get("kind") != "agent" else ""
    if str(recipe.get("gpus") or "") != default_gpus:
        out["gpus"] = recipe.get("gpus") or ""

    if recipe.get("container_name") and recipe["container_name"] != slug(name):
        out["container_name"] = recipe["container_name"]
    if recipe.get("kind") and recipe["kind"] != "model":
        out["kind"] = recipe["kind"]
    if recipe.get("tags"):
        out["tags"] = list(recipe["tags"])
    if recipe.get("color"):
        out["color"] = recipe["color"]
    if recipe.get("est_memory_gb") is not None:
        out["est_memory_gb"] = recipe["est_memory_gb"]
    if recipe.get("pinned"):
        out["pinned"] = True
    return out


def dump_recipe_file(data: dict[str, Any], fmt: str = "yaml") -> str:
    """Serialize a compact recipe as YAML (default) or JSON."""
    fmt = (fmt or "yaml").strip().lower()
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if fmt not in ("yaml", "yml"):
        raise RecipeFileError("format must be yaml or json")
    if yaml is None:  # pragma: no cover - PyYAML is a dependency
        raise RecipeFileError("YAML recipe files need PyYAML (pip install PyYAML)")

    class _Dumper(yaml.SafeDumper):
        pass

    def _represent_text(dumper: Any, value: str) -> Any:
        # Multi-line fields (args, env, volumes, docker_args) read best as `|` blocks.
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)

    _Dumper.add_representer(str, _represent_text)
    header = "# Compact recipe file (Local LLM Manager): paste into Recipes -> Import.\n"
    return header + yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False)
