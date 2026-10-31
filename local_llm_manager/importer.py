"""Turn a pasted starter script or `docker run` command into a recipe draft.

Rather than parsing bash (defaults, conditionals, arrays, computed JSON), the text is executed by
bash with a PATH holding only a few read-only text tools and a fake `docker` that records the argv
of `docker run`; every other command is recorded and skipped. The argv is then split into the
recipe fields.
"""

import asyncio
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .recipes import FRAMEWORKS, MANAGED_LABEL, image_serves

RUN_TIMEOUT = 20

# The only external programs the script may really run during import: read-only text tools that
# starter scripts use to compute values. Anything else found by PATH lookup is recorded and skipped
# by bash's command_not_found_handle (it returns 0, so `set -e` scripts keep going). `docker` is a
# fake that records `docker run`. Programs called by absolute path bypass this, which is why the
# static check (script_check.py) flags them before import.
ALLOWED_TOOLS = (
    "awk", "gawk", "mawk", "sed", "grep", "egrep", "fgrep", "cut", "tr", "head", "tail", "wc",
    "sort", "uniq", "basename", "dirname", "realpath", "readlink", "date", "printf", "echo",
    "cat", "seq", "expr", "bc", "id", "whoami", "hostname", "uname", "nproc", "getconf", "true",
    "false", "test", "[", "bash", "numfmt", "rev", "paste", "column",
)

DOCKER_SHIM = """#!{bash}
if [ "$1" = "run" ]; then
  shift
  : > "$DGXM_CAPTURE"
  for a in "$@"; do printf '%s\\0' "$a" >> "$DGXM_CAPTURE"; done
else
  printf '%s\\0' docker "$@" >> "$DGXM_SKIPPED"; printf '\\036' >> "$DGXM_SKIPPED"
fi
exit 0
"""

# Loaded by every bash started for the import (BASH_ENV), including nested `bash -c`.
PRELUDE = """command_not_found_handle() {
  printf '%s\\0' "$@" >> "$DGXM_SKIPPED"; printf '\\036' >> "$DGXM_SKIPPED"
  return 0
}
# PATH stays the restricted one: e.g. `source venv/bin/activate` must not make real tools reachable.
readonly PATH BASH_ENV
# `exec some-server ...` would bypass the handler above; run it as a normal (recorded) call instead.
exec() {
  [ $# -eq 0 ] && return 0
  "$@"
  builtin exit $?
}
# Starter scripts abort when a probe like `command -v nvidia-smi` fails. Unknown commands are
# harmless here anyway (the handler above records and skips them), so report every tool as
# installed to let the script reach its `docker run`.
command() {
  case "${1:-}" in
    -v | -V)
      local found
      found="$(type -P "${2:-}" 2>/dev/null)" || found="/usr/local/bin/${2:-}"
      [ "${1:-}" = "-v" ] && printf '%s\n' "$found"
      return 0
      ;;
  esac
  builtin command "$@"
}
# Resource preflights (free memory, GPU busy) must not block imports: the manager checks memory
# again when the recipe actually starts. Scripts see an idle machine with plenty of memory.
dgxm_meminfo() {
  printf 'MemTotal: 134217728 kB\nMemFree: 134217728 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\nMemAvailable: 134217728 kB\n'
}
awk() {
  local a fake=0 args=()
  for a in "$@"; do
    if [ "$a" = /proc/meminfo ]; then fake=1; else args+=("$a"); fi
  done
  if [ "$fake" = 1 ]; then dgxm_meminfo | builtin command awk "${args[@]}"; else builtin command awk "$@"; fi
}
grep() {
  local a fake=0 args=()
  for a in "$@"; do
    if [ "$a" = /proc/meminfo ]; then fake=1; else args+=("$a"); fi
  done
  if [ "$fake" = 1 ]; then dgxm_meminfo | builtin command grep "${args[@]}"; else builtin command grep "$@"; fi
}
cat() {
  local a
  for a in "$@"; do [ "$a" = /proc/meminfo ] && { dgxm_meminfo; return 0; }; done
  builtin command cat "$@"
}
"""

# Second attempt when a script stops before its `docker run` (typically preflight checks such as
# `[ -f "$MODEL_DIR/config.json" ] || exit 1` that fail because the model lives on another machine):
# `exit` only reports, and the script is sourced as an `if` condition, where bash ignores `set -e`.
LENIENT_PRELUDE = """exit() {
  echo "[import] ignored: exit ${1:-0}" >&2
  return 0
}
"""
LENIENT_RUNNER = 'if source "$0" "$@"; then :; fi'


# docker run options that take a value (the rest of the options are boolean flags).
VALUE_OPTS = {
    "-e", "--env", "-v", "--volume", "-p", "--publish", "--name", "--gpus", "--entrypoint",
    "--network", "--net", "--ipc", "--shm-size", "--ulimit", "--security-opt", "--env-file",
    "--mount", "-w", "--workdir", "-u", "--user", "-l", "--label", "--device", "--cpus", "-m",
    "--memory", "--memory-swap", "--runtime", "--restart", "-h", "--hostname", "--add-host",
    "--cap-add", "--cap-drop", "--group-add", "--pid", "--uts", "--platform", "--pull",
    "--log-driver", "--log-opt", "--tmpfs", "--cpuset-cpus", "--stop-signal", "--stop-timeout",
    "--health-cmd", "--health-interval", "--health-timeout", "--health-retries", "--dns",
    "--expose", "--label-file", "--cgroupns", "--userns", "--storage-opt", "--sysctl",
}
# Options the manager controls itself.
DROPPED_FLAGS = {"-d", "--detach", "--rm", "-i", "-t", "-it", "-ti", "--interactive", "--tty"}

MODEL_FLAGS = ("--model", "--model-path")


class ScriptImportError(Exception):
    pass


@lru_cache(maxsize=1)
def find_bash() -> Optional[str]:
    """A bash >= 4: command_not_found_handle, which records the skipped calls, is missing in 3.2 (macOS)."""
    candidates = [shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"]
    for bash in dict.fromkeys(c for c in candidates if c):
        try:
            out = subprocess.run([bash, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True,
                                 timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.stdout.strip().isdigit() and int(out.stdout) >= 4:
            return bash
    return None


async def capture_docker_run(script: str, lenient: bool = False) -> tuple[Optional[list[str]], str, list[list[str]]]:
    """Run the script with only ALLOWED_TOOLS available (lenient: ignore `exit` and `set -e`).

    Returns (argv after `docker run` or None, script output, argv of each skipped call).
    """
    bash = find_bash()
    if bash is None:
        raise ScriptImportError("Script import needs bash 4 or newer (macOS ships 3.2): run `brew install bash`, "
                                "then restart the manager.")
    with tempfile.TemporaryDirectory(prefix="dgxm-import-") as tmp:
        tmpdir = Path(tmp)
        bindir = tmpdir / "bin"
        bindir.mkdir()
        for tool in ALLOWED_TOOLS:
            real = shutil.which(tool)
            if real:
                (bindir / tool).symlink_to(real)
        shim = bindir / "docker"
        shim.write_text(DOCKER_SHIM.format(bash=bash))
        shim.chmod(0o755)
        prelude = tmpdir / "prelude.sh"
        prelude.write_text(PRELUDE + (LENIENT_PRELUDE if lenient else ""))
        script_path = tmpdir / "script.sh"
        script_path.write_text(script)
        capture = tmpdir / "capture"
        skipped_log = tmpdir / "skipped"
        work = tmpdir / "work"
        work.mkdir()
        env = {
            "PATH": str(bindir),
            "HOME": os.path.expanduser("~"),
            "USER": os.environ.get("USER", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "BASH_ENV": str(prelude),
            "DGXM_CAPTURE": str(capture),
            "DGXM_SKIPPED": str(skipped_log),
        }
        proc = await asyncio.create_subprocess_exec(
            *([bash, "-c", LENIENT_RUNNER, str(script_path)] if lenient else [bash, str(script_path)]),
            cwd=work, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            out = b""
        output = out.decode(errors="replace")[-4000:]
        skipped: list[list[str]] = []
        if skipped_log.exists():
            # Records are separated by \x1e, arguments by NUL.
            for record in skipped_log.read_bytes().split(b"\x1e"):
                args = [a.decode(errors="replace") for a in record.split(b"\0")[:-1]]
                if args:
                    skipped.append(args)
        if not capture.exists():
            return None, output, skipped
        raw = capture.read_bytes().split(b"\0")
        return [a.decode(errors="replace") for a in raw[:-1]], output, skipped


def _tildify(path: str) -> str:
    home = os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


def _mount_to_volume(value: str) -> Optional[str]:
    """`--mount type=bind,source=...,target=...` as a `-v source:target[:ro]` line, or None."""
    parts: dict[str, str] = {}
    for item in (value or "").split(","):
        key, sep, val = item.partition("=")
        key = key.strip().lower()
        if key:
            parts[key] = val.strip() if sep else "true"
    if parts.get("type", "bind") != "bind":
        return None
    supported = {"type", "source", "src", "target", "dst", "destination", "readonly", "ro"}
    if set(parts) - supported:
        # Keep options such as bind-propagation intact as a Docker argument.
        return None
    source = parts.get("source") or parts.get("src")
    target = parts.get("target") or parts.get("dst") or parts.get("destination")
    if not source or not target:
        return None
    readonly = parts.get("readonly") in ("true", "1", "ro") or parts.get("ro") in ("true", "1")
    return f"{_tildify(source)}:{target}" + (":ro" if readonly else "")


def _split_opt(tok: str) -> tuple[str, Optional[str]]:
    if tok.startswith("--") and "=" in tok:
        k, v = tok.split("=", 1)
        return k, v
    return tok, None


def _detect_framework(image: str, entrypoint: str, program: list[str]) -> str:
    hay = " ".join([image, entrypoint, *program]).lower()
    if "sglang" in hay:
        return "sglang"
    if "llama.cpp" in hay or "llama-server" in hay or "llama_cpp" in hay:
        return "llamacpp"
    if "vllm" in hay:
        return "vllm"
    return "custom"


def _group_flags(tokens: list[str]) -> list[str]:
    """One line per flag with its values, shell-quoted."""
    lines: list[list[str]] = []
    for tok in tokens:
        if not lines or (tok.startswith("-") and not re.fullmatch(r"-\d+(\.\d+)?", tok)):
            lines.append([tok])
        else:
            lines[-1].append(tok)
    return [shlex.join(line) for line in lines]


def _split_command(tokens: list[str], detect, serves: bool = False) -> dict[str, Any]:
    """Split a server command line into a command template and per-flag arguments.

    `detect(program)` picks the framework from the leading positional words. The model (argument
    after `...serve`, or --model / --model-path / llama.cpp's -m), --host and --port go into the
    command template; everything else is returned as `remaining`. With `serves` the image's own
    entrypoint is `vllm serve`, so the first positional word is the model.
    """
    # Leading positional part of the container command, e.g. `serve /model`, `python3 -m sglang.launch_server`.
    program: list[str] = []
    j = 0
    while j < len(tokens):
        t = tokens[j]
        if t == "-m" and program and os.path.basename(program[-1]).startswith("python") and j + 1 < len(tokens):
            program += tokens[j:j + 2]
            j += 2
        elif t.startswith("-"):
            break
        else:
            program.append(t)
            j += 1
    rest = tokens[j:]
    framework = detect(program)

    model, model_flag = "", ""
    for k, t in enumerate(program):
        if (k > 0 and program[k - 1].endswith("serve")) or (k == 0 and serves):
            model = t
            program[k] = "{model}"
            break

    host, port = "", ""
    remaining: list[str] = []
    k = 0
    while k < len(rest):
        key, val = _split_opt(rest[k])
        is_model_flag = key in MODEL_FLAGS or (key == "-m"
                                               and (framework == "llamacpp"
                                                    or (program and os.path.basename(program[-1]) == "llama-server")))
        if (key in ("--host", "--port") or (is_model_flag and not model)) and (val is not None or k + 1 < len(rest)):
            if val is None:
                val = rest[k + 1]
                k += 2
            else:
                k += 1
            if key == "--host":
                host = val
            elif key == "--port":
                port = val
            else:
                model, model_flag = val, key
            continue
        remaining.append(rest[k])
        k += 1

    command = list(program)
    if model_flag:
        command += [model_flag, "{model}"]
    if host:
        command += ["--host", host]
    if port:
        command += ["--port", "{port}"]
    # shlex.join would quote the placeholders; they are plain words, so join manually.
    command_str = " ".join(t if t in ("{model}", "{port}") else shlex.quote(t) for t in command)
    return {"framework": framework, "command": command_str, "model": model, "host": host, "port": port,
            "remaining": remaining}


def recipe_from_argv(argv: list[str], description: str = "") -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    env, volumes, docker_args, dropped = [], [], [], []
    name = gpus = entrypoint = ""
    publish: list[str] = []
    host_network = False

    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        key, val = _split_opt(argv[i])
        if key in VALUE_OPTS and val is None:
            val = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            i += 1
        if key in DROPPED_FLAGS:
            dropped.append(key)
        elif key in ("-e", "--env"):
            # KEY= (empty) becomes a bare KEY, i.e. passed through from the manager's environment.
            env.append(val[:-1] if val.endswith("=") else val)
        elif key in ("-v", "--volume"):
            host, sep, rest = val.partition(":")
            volumes.append(_tildify(host) + sep + rest)
        elif key in ("-p", "--publish"):
            publish.append(val)
        elif key == "--name":
            name = val
        elif key == "--gpus":
            gpus = val
        elif key in ("-l", "--label"):
            # The manager adds its own labels when it starts the container.
            label_key = (val or "").split("=", 1)[0]
            if label_key != MANAGED_LABEL and not label_key.startswith(MANAGED_LABEL + "."):
                docker_args += [key, val]
        elif key == "--mount":
            volume = _mount_to_volume(val or "")
            if volume:
                volumes.append(volume)
            else:
                docker_args += [key, val]
                warnings.append(f"Kept --mount {val} as a Docker argument: only simple bind mounts with a "
                                "source and target become volumes")
        elif key == "--env-file":
            docker_args += [key, val]
            warnings.append(f"--env-file {val} is kept as a Docker argument; the file must exist on the "
                            "recipe's host.")
        else:
            if key == "--entrypoint":
                entrypoint = val
            if key in ("--network", "--net") and val == "host":
                host_network = True
            docker_args += [key, val] if val is not None else [key]
    if i >= len(argv):
        raise ScriptImportError("Could not find the image in the docker run command")
    image = argv[i]
    tokens = argv[i + 1:]

    split = _split_command(tokens, lambda program: _detect_framework(image, entrypoint, program),
                           serves=image_serves(image, docker_args))
    framework, command_str, model, port, remaining = (
        split["framework"], split["command"], split["model"], split["port"], split["remaining"])

    preset = FRAMEWORKS[framework]
    container_port = int(port) if port.isdigit() else None
    host_port = None
    if publish:
        parts = publish[0].split(":")
        if len(parts) >= 2 and parts[-2].isdigit():
            host_port = int(parts[-2])
        cport = parts[-1].split("/")[0]
        if cport.isdigit():
            container_port = container_port or int(cport)
        if len(publish) > 1:
            warnings.append(f"Only the first port mapping is used; ignored: {', '.join(publish[1:])}")
    container_port = container_port or preset["container_port"]
    host_port = host_port or container_port
    if host_network and publish:
        warnings.append("--network host is set, so the -p mapping has no effect")

    if dropped:
        warnings.append(f"Removed {' '.join(sorted(set(dropped)))} (the manager runs containers detached itself)")
    if not model:
        warnings.append("Could not tell which argument is the model; check the Model and Command fields")

    served = ""
    for k, t in enumerate(remaining):
        if t == "--served-model-name" and k + 1 < len(remaining):
            served = remaining[k + 1]
    display_name = served or name or (os.path.basename(model.rstrip("/")) if model else "") or "Imported recipe"

    recipe = {
        "runtime": "docker",
        "name": display_name,
        "description": description,
        "framework": framework,
        "image": image,
        "model": model,
        "container_name": name,
        "host_port": host_port,
        "container_port": container_port,
        "command": command_str,
        "args": "\n".join(_group_flags(remaining)),
        "env": "\n".join(env),
        "volumes": "\n".join(volumes),
        "docker_args": shlex.join(docker_args),
        "gpus": gpus,
        "health_path": preset["health_path"],
        "est_memory_gb": None,
        "tags": [],
        "color": "",
    }
    return recipe, warnings


# Model servers started directly on the host (programs, or `python -m` modules).
NATIVE_SERVERS = {"mlx_lm.server": "process-custom", "llama-server": "process-custom",
                  "tensorfold": "tensorfold", "vllm": "process-custom"}
NATIVE_MODULES = {"mlx_lm.server": "process-custom", "mlx_lm": "process-custom",
                  "sglang.launch_server": "process-custom",
                  "vllm.entrypoints.openai.api_server": "process-custom"}


def _native_server(argv: list[str]) -> Optional[str]:
    """Framework key if argv starts a known model server, else None."""
    prog = os.path.basename(argv[0])
    if prog == "mlx_lm" and len(argv) > 1 and argv[1] == "server":
        return "process-custom"
    if prog == "tensorfold":
        return "tensorfold" if len(argv) > 1 and argv[1] == "serve" else None
    if prog == "vllm":
        return "process-custom" if len(argv) > 1 and argv[1] == "serve" else None
    if prog in NATIVE_SERVERS:
        return NATIVE_SERVERS[prog]
    if prog.startswith("python") and len(argv) > 2 and argv[1] == "-m":
        module = argv[2]
        if module == "mlx_lm":
            return "process-custom" if len(argv) > 3 and argv[3] == "server" else None
        return NATIVE_MODULES.get(module)
    return None


def recipe_from_process_argv(argv: list[str], description: str = "") -> tuple[dict[str, Any], list[str]]:
    framework = _native_server(argv) or "process-custom"
    split = _split_command(argv, lambda program: framework)
    warnings = ["No `docker run` found - imported as a native process that runs directly on this machine"]
    if not split["model"]:
        warnings.append("Could not tell which argument is the model; check the Model and Command fields")
    port = int(split["port"]) if split["port"].isdigit() else FRAMEWORKS[framework]["container_port"]
    if not split["port"]:
        warnings.append(f"The command sets no --port; the server's default is assumed ({port})")
    model = split["model"]
    name = os.path.basename(model.rstrip("/")) if model else os.path.basename(argv[0])
    recipe = {
        "runtime": "process",
        "name": name or "Imported recipe",
        "description": description,
        "framework": framework,
        "image": "",
        "model": _tildify(model),
        "container_name": "",
        "host_port": port,
        "container_port": port,
        "command": split["command"],
        "args": "\n".join(_group_flags(split["remaining"])),
        "env": "",
        "volumes": "",
        "docker_args": "",
        "gpus": "",
        "health_path": FRAMEWORKS[framework]["health_path"],
        "est_memory_gb": None,
        "tags": [],
        "color": "",
    }
    return recipe, warnings


def description_from_script(script: str) -> str:
    """First meaningful comment line of the script, e.g. '# Starter: org/model on DGX Spark'."""
    for line in script.splitlines()[:30]:
        s = line.strip()
        if s.startswith("#!"):
            continue
        if s.startswith("#"):
            text = s.lstrip("#").strip()
            if len(text) > 3 and not text.startswith(("./", "-", "*")):
                return text[:200]
        elif s:
            break
    return ""


async def import_script(script: str) -> dict[str, Any]:
    argv, output, skipped = await capture_docker_run(script)
    description = description_from_script(script)
    stopped_early: list[str] = []
    if argv is None and not any(_native_server(a) for a in skipped):
        # The script ended before starting anything; retry ignoring its own exit / error checks.
        retry = await capture_docker_run(script, lenient=True)
        if retry[0] is not None or any(_native_server(a) for a in retry[2]):
            stopped_early = [line.strip() for line in output.splitlines()
                             if "error" in line.lower() or "warning" in line.lower()][-5:]
            argv, output, skipped = retry
    if argv is not None:
        recipe, warnings = recipe_from_argv(argv, description)
    else:
        # No container: maybe the script starts a server directly (e.g. MLX on a Mac).
        server = next((a for a in reversed(skipped) if _native_server(a)), None)
        if server is None:
            lines = _unique_lines(skipped)
            raise ScriptImportError(
                "The script neither ran `docker run` nor a known model server (mlx_lm.server, "
                "llama-server, vllm serve, python -m sglang.launch_server, ...)."
                + ("\n\nSkipped commands:\n" + "\n".join(lines[:20]) if lines else "")
                + (f"\n\nScript output:\n{output.strip()}" if output.strip() else ""))
        recipe, warnings = recipe_from_process_argv(server, description)
    if stopped_early or argv is not None and output and "[import] ignored: exit" in output:
        warnings.insert(0, "The script stopped before starting the model (its checks run on the dashboard's "
                        "machine, where e.g. the model folder or nvidia-smi may be missing). Imported by "
                        "ignoring its exit/error checks - review the fields."
                        + ("\n    " + "\n    ".join(stopped_early) if stopped_early else ""))
    return {"recipe": recipe, "warnings": warnings, "docker_argv": argv, "process_argv": None if argv else server,
            "skipped_calls": _unique_lines(skipped)}


def _unique_lines(calls: list[list[str]]) -> list[str]:
    out: list[str] = []
    for args in calls:
        line = shlex.join(args)[:300]
        if line not in out:
            out.append(line)
    return out
