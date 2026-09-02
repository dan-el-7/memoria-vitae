# Providers and data boundaries

VMA has two independently configurable inference stages:

| Stage | Default | Input | Output |
|---|---|---|---|
| Vision | Ollama `qwen3-vl:2b` | JPEG plus the perception prompt | schema-constrained observation JSON |
| Reasoning | Ollama `huihui_ai/qwen3-abliterated:8b` | run history, user question, tools | answer and optional tool calls |

The Ollama provider uses `/api/chat`. Vision requests use a JSON schema through
`format`; reasoning requests use tools. The two modes are intentionally kept
separate because the verified Ollama behavior is unreliable when `format` and
`tools` are combined. Model residency is visible through `/api/ps`; the vision
model defaults to numeric `keep_alive = -1`, while reasoning uses a finite residency.

On the tested 8 GB RTX 5050 laptop, Ollama's automatic full-GPU scheduler evicts
the VLM when the 8.2B reasoning model is loaded. The default uses
`options.num_gpu = 20` for reasoning, leaving the VLM fully GPU-resident while
the LLM uses hybrid CPU/GPU placement. GPU layers are adjustable in the Models
panel for machines with different memory headroom.

An OpenAI-compatible provider can be selected independently for either stage by
setting its kind, base URL, model, and API key in the model editor or config.
When either stage is cloud-backed, the run metadata and dashboard expose that
fact and the dashboard shows a data-egress warning. API keys are configuration
values and should be supplied through the local configuration mechanism rather
than committed to source control.

The desktop stores raw frames according to `save_frames` (`none`, `important`,
or `all`). Observations, locations, notes, chat, reports, and metrics remain in
the per-run SQLite directory. Agent tools resolve paths within the run and only
write under `reports/` or `exports/`.
