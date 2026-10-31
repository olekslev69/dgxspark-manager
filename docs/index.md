# Local LLM Manager

A small web dashboard to run local LLMs on your own machines – an NVIDIA DGX Spark, a Mac with Apple
silicon, or any Linux box – from any browser on your network.

![Model recipes, dark theme](screenshots/recipes-dark.png)

- **Recipes** save how to start a model server (vLLM, SGLang, llama.cpp, TensorFold or any image /
  native binary), as a Docker container or a native process. You can also import an existing start script,
  `docker run` command or compact JSON/YAML recipe file, and export any recipe the same way.
- **Start, stop, restart or "switch to"** a model. Switching stops the other models on that machine and
  frees memory first.
- **Live status, logs and stats:** loading / ready / failed, tokens per second, running requests and
  KV-cache usage.
- **Schedules** start, stop or switch models at set times.
- **Several machines, one dashboard:** e.g. a DGX Spark and a Mac Studio side by side.

!!! note
    Model weights and Docker images must already be on the machine that runs them; the manager does not
    download them.

## Where to start

<div class="grid cards" markdown>

-   **[Quick start](quick-start.md)**

    Install the manager on one machine, log in and start your first model.

-   **[Several machines](several-machines.md)**

    Add more machines as agents and control them all from one dashboard.

</div>

Everything else – recipes in detail, agents, schedules, security and configuration – is in the
[README on GitHub](https://github.com/einfachllm/local-llm-manager#readme) for now.
