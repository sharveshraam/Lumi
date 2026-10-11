# Models

LUMI asks Ollama for an answer. Everything about which model, how big its
context window is and how long an answer may be lives in Settings → Model
(or `settings.json`).

## Honest expectations

A model that fits in a laptop's memory is a *small* model. On this hardware:

* a 3B model writes decent study notes, explains concepts, drafts emails and
  drives tools reliably;
* it makes factual mistakes, especially with numbers and recent events;
* it is slower than a cloud model and much slower without a GPU.

LUMI's system prompt tells the model to say "I am not sure" rather than guess,
and the UI labels every answer as coming from a local model. Nothing here will
turn a 3B model into a frontier model — treat it as a competent study partner,
and check anything that matters.

## Choosing by hardware

| Your machine | Start with | Why |
| --- | --- | --- |
| 8 GB RAM, no GPU | `llama3.2:1b` or `qwen2.5:3b` | small enough to leave everything else usable |
| 16 GB RAM, no GPU | `llama3.2:3b` or `qwen2.5:3b` | the sweet spot: fast enough to be pleasant |
| 16 GB RAM + RTX 3050 6 GB | `llama3.2:3b`, then try `qwen2.5:7b` or `llama3.1:8b` at Q4 | good quality, still interactive |
| 16 GB RAM + RTX 4060 8 GB | `qwen2.5:7b` | better reasoning, ~40–60 tokens/s |
| Coding questions especially | `qwen2.5-coder:7b`, `codegemma:2b` | trained on code |
| Screenshots and diagrams | `llava:7b` | the only vision model in the catalogue |
| Semantic memory search | `nomic-embed-text` | embeddings, not chat |

LUMI's own recommendation is in **Settings → Model** via the `recommend_models`
tool: it reads your RAM and VRAM and marks each catalogue entry as fitting,
partial, CPU-only or too big.

## The catalogue LUMI ships with

| Model | Size | Roles | Notes |
| --- | --- | --- | --- |
| `llama3.2:1b` | ~1.3 GB | chat, fast | lowest quality, highest speed |
| `llama3.2:3b` | ~2.0 GB | chat, default | the recommended starting point |
| `qwen2.5:3b` | ~1.9 GB | chat | strong for its size, good at lists |
| `gemma2:2b` | ~1.6 GB | chat | good writing, smaller context |
| `phi3:mini` | ~2.3 GB | chat | strong reasoning for 3.8B |
| `llama3.1:8b` | ~4.9 GB | chat, quality | needs ~8 GB free |
| `qwen2.5:7b` | ~4.7 GB | chat, quality | excellent all-rounder |
| `mistral:7b` | ~4.1 GB | chat | older but fast and predictable |
| `qwen2.5-coder:7b` | ~4.7 GB | code | for programming help |
| `codegemma:2b` | ~1.6 GB | code, fast | tiny coding model |
| `llava:7b` | ~4.7 GB | vision | can look at images |
| `nomic-embed-text` | ~0.27 GB | embeddings | for semantic memory search |

Sizes are approximate Q4 downloads. Your free disk space needs to cover the
model plus, briefly, the download.

## Pulling and switching models

Pull from the terminal:

```bat
ollama pull qwen2.5:7b
ollama list
```

Or ask LUMI to do it: *"pull qwen2.5:3b"*. Pulling is treated as installing
software and always asks for confirmation first. The download can be
cancelled with the Stop button, and LUMI estimates how long it will take at
your connection speed before it starts.

Switch the active model in the chat header, or in Settings → Model.

## Context and speed

Two settings have the biggest effect:

* **Context window** (`generation.num_ctx`, default 4096). Bigger means LUMI
  remembers more of the conversation but uses more memory. 8192 is a reasonable
  upper bound for a 7B model on 6 GB of VRAM.
* **Maximum answer length** (`generation.num_predict`, default 512). Long
  answers are slower; 1024 is enough for most study notes.

Quality profiles set both at once:

| Profile | Context | Answer limit | Temperature |
| --- | --- | --- | --- |
| `fast` | 2048 | 256 | 0.5 |
| `balanced` | 4096 | 512 | 0.6 |
| `quality` | 8192 | 1024 | 0.7 |

## Unloading models

Loading a model takes a few seconds and holds memory. Two options:

* **Keep model loaded for** (`ollama.keep_alive`, default `10m`) — how long
  Ollama keeps it resident after the last answer.
* **Unload the model after it has been idle** (`ollama.unload_on_idle`) — LUMI
  asks Ollama to release it after `idle_unload_after_s` (10 minutes), which
  gives the memory back to your other apps.

Ask *"unload the model"* at any time, or use the `unload_model` tool.

## When something goes wrong

| Symptom | Fix |
| --- | --- |
| "Ollama is not reachable" | start Ollama (Start menu → Ollama) and press the ↻ button in the chat header |
| "no models installed" | `ollama pull llama3.2:3b` |
| The model answers very slowly the first time | it is loading into memory; subsequent answers are faster |
| Answers stop mid-sentence | the context window is full — start a new chat or raise `num_ctx` |
| "model requires more system memory" | the model is too big for this machine; pick a smaller one |
| Answers are poor quality | try `qwen2.5:7b` if your hardware allows, and be specific in the question |

Ollama's own troubleshooting guide: <https://github.com/ollama/ollama/blob/main/docs/faq.md>.
