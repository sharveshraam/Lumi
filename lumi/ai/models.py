"""Model catalogue and recommendations.

LUMI never downloads a model on its own.  What this module does is answer the
question the user actually has: *"what can this laptop run, and what will it
cost?"*  Every size is an approximation of the default Q4 tag, and everything
that could not be measured is described as an estimate rather than a promise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from lumi.ai.hardware import HardwareInfo, fit_for_model

GB = 1024 ** 3
MB = 1024 ** 2

ROLE_FAST = "fast"
ROLE_BALANCED = "balanced"
ROLE_CODING = "coding"
ROLE_REASONING = "reasoning"
ROLE_VISION = "vision"
ROLE_EMBEDDING = "embeddings"

#: Bytes per billion parameters for common quantisations (~Q4_K_M default).
_BYTES_PER_PARAM = {
    "q2": 0.40 * GB,
    "q3": 0.50 * GB,
    "q4": 0.62 * GB,
    "q5": 0.75 * GB,
    "q6": 0.85 * GB,
    "q8": 1.10 * GB,
    "f16": 2.00 * GB,
}
_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)
_SIZE_TAG_RE = re.compile(r":(\d+(?:\.\d+)?)b", re.IGNORECASE)
_QUANT_RE = re.compile(r"(q\d(?:_[a-z0-9]+)?|f16)", re.IGNORECASE)


@dataclass
class ModelSuggestion:
    """One catalogue entry."""

    name: str
    size_bytes: int
    parameters: str
    roles: tuple[str, ...]
    purpose: str
    strengths: tuple[str, ...] = ()
    weaknesses: tuple[str, ...] = ()
    context_tokens: int = 8192
    supports_tools: bool = True

    # ------------------------------------------------------------- rendering
    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / GB, 2)

    @property
    def size_human(self) -> str:
        value = self.size_bytes / GB
        return f"{value:.1f} GB" if value < 10 else f"{value:.0f} GB"

    def fit(self, hardware: HardwareInfo) -> dict[str, str]:
        level = fit_for_model(hardware, self.size_gb)
        message = {
            "good": f"Fits in GPU memory ({hardware.vram_budget_gb:.1f} GB usable).",
            "partial": "Fits partly on the GPU; expect some layers on the CPU.",
            "cpu": "Runs on the CPU only - slower, but it works.",
            "too_big": "Bigger than this machine can hold comfortably.",
        }[level]
        return {"level": level, "label": level.replace("_", " "), "message": message}

    def to_dict(self, hardware: Optional[HardwareInfo] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "size_gb": self.size_gb,
            "size_human": self.size_human,
            "parameters": self.parameters,
            "roles": list(self.roles),
            "purpose": self.purpose,
            "strengths": list(self.strengths),
            "weaknesses": list(self.weaknesses),
            "context_tokens": self.context_tokens,
            "supports_tools": self.supports_tools,
        }
        if hardware is not None:
            payload["fit"] = self.fit(hardware)
        return payload


#: Catalogue of models worth recommending.  Sizes are the published default tags.
CATALOGUE: tuple[ModelSuggestion, ...] = (
    ModelSuggestion(
        name="llama3.2:3b",
        size_bytes=int(2.0 * GB),
        parameters="3B",
        roles=(ROLE_FAST, ROLE_BALANCED),
        purpose="The everyday default: quick, good enough for study help and everyday questions.",
        strengths=("runs comfortably in about 4 GB of VRAM", "follows instructions well for its size"),
        weaknesses=("weaker at maths and longer code",),
        context_tokens=8192,
    ),
    ModelSuggestion(
        name="llama3.2:1b",
        size_bytes=int(1.3 * GB),
        parameters="1B",
        roles=(ROLE_FAST,),
        purpose="Very small and quick, for short answers and slower laptops.",
        strengths=("fast even on CPU", "small download"),
        weaknesses=("shallow reasoning", "loses track of long instructions"),
        context_tokens=8192,
    ),
    ModelSuggestion(
        name="qwen2.5:3b",
        size_bytes=int(1.9 * GB),
        parameters="3B",
        roles=(ROLE_FAST, ROLE_CODING),
        purpose="Compact model that punches above its weight on maths and short code.",
        strengths=("strong numeracy for the size",),
        weaknesses=("short context on the default tag",),
        context_tokens=8192,
    ),
    ModelSuggestion(
        name="gemma2:2b",
        size_bytes=int(1.6 * GB),
        parameters="2B",
        roles=(ROLE_FAST,),
        purpose="Small and tidy writing style; good for notes and explanations.",
        strengths=("fluent wording", "small footprint"),
        weaknesses=("limited technical depth",),
        context_tokens=8192,
    ),
    ModelSuggestion(
        name="phi3:mini",
        size_bytes=int(2.2 * GB),
        parameters="3.8B",
        roles=(ROLE_FAST, ROLE_REASONING),
        purpose="Trained on textbook-style material; decent step-by-step answers for its size.",
        strengths=("structured explanations",),
        weaknesses=("sometimes overconfident",),
        context_tokens=4096,
    ),
    ModelSuggestion(
        name="llama3.1:8b",
        size_bytes=int(4.7 * GB),
        parameters="8B",
        roles=(ROLE_BALANCED, ROLE_REASONING),
        purpose="The best all-round quality that still fits a 6 GB laptop GPU (partly on the CPU).",
        strengths=("solid reasoning", "reliable tool calling"),
        weaknesses=("slower on 6 GB of VRAM - some layers offload to the CPU",),
        context_tokens=8192,
    ),
    ModelSuggestion(
        name="qwen2.5:7b",
        size_bytes=int(4.7 * GB),
        parameters="7B",
        roles=(ROLE_BALANCED, ROLE_REASONING),
        purpose="Strong maths and reasoning for a 7B model.",
        strengths=("good at multi-step problems",),
        weaknesses=("4.7 GB download", "needs about 6 GB of free memory to feel fast"),
        context_tokens=8192,
    ),
    ModelSuggestion(
        name="mistral:7b",
        size_bytes=int(4.4 * GB),
        parameters="7B",
        roles=(ROLE_BALANCED,),
        purpose="Dependable, plain-spoken 7B model; a safe alternative to llama 8B.",
        strengths=("stable output", "wide tooling support"),
        weaknesses=("slower than 3B models on this class of laptop",),
        context_tokens=8192,
    ),
    ModelSuggestion(
        name="qwen2.5-coder:7b",
        size_bytes=int(4.7 * GB),
        parameters="7B",
        roles=(ROLE_CODING,),
        purpose="The best local choice for reading, writing and explaining code.",
        strengths=("code-aware", "follows conventions given in a prompt"),
        weaknesses=("weaker at general chat than a general model",),
        context_tokens=16384,
    ),
    ModelSuggestion(
        name="codegemma:2b",
        size_bytes=int(1.6 * GB),
        parameters="2B",
        roles=(ROLE_CODING, ROLE_FAST),
        purpose="Small code helper for quick snippets when speed matters more than depth.",
        strengths=("very fast", "fills in small functions"),
        weaknesses=("not for long-context project work",),
        context_tokens=8192,
    ),
    ModelSuggestion(
        name="llava:7b",
        size_bytes=int(4.7 * GB),
        parameters="7B",
        roles=(ROLE_VISION,),
        purpose="Reads images - useful for photographs of circuit diagrams or handwritten notes.",
        strengths=("local image questions",),
        weaknesses=("slow on CPU", "describes rather than solves"),
        context_tokens=4096,
    ),
    ModelSuggestion(
        name="nomic-embed-text",
        size_bytes=int(0.27 * GB),
        parameters="137M",
        roles=(ROLE_EMBEDDING,),
        purpose="Text embeddings for local search over your notes (optional extra).",
        strengths=("tiny", "fast"),
        weaknesses=("not a chat model",),
        context_tokens=8192,
        supports_tools=False,
    ),
)

#: Order in which a default model is chosen from what is installed.
DEFAULT_LADDER = ("llama3.2:3b", "qwen2.5:3b", "llama3.2:1b", "gemma2:2b", "llama3.1:8b", "qwen2.5:7b")


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------
def _normalise(name: str) -> str:
    return (name or "").strip().lower()


def find_suggestion(name: str) -> Optional[ModelSuggestion]:
    """Catalogue entry for a model name, tolerating tags and quantisation suffixes."""
    lowered = _normalise(name)
    if not lowered:
        return None
    for entry in CATALOGUE:
        if entry.name == lowered:
            return entry
    base = lowered.split(":")[0]
    for entry in CATALOGUE:
        if entry.name.split(":")[0] == base:
            return entry
    return None


def estimate_size_from_name(name: str) -> int:
    """Estimated download size in **bytes** for a model tag.

    Catalogue entries use their published size; anything else is estimated from
    the parameter count in the tag (``7b`` -> about 4.3 GB at Q4).
    """
    suggestion = find_suggestion(name)
    if suggestion is not None:
        return suggestion.size_bytes
    lowered = _normalise(name)
    if not lowered:
        return 0
    match = _SIZE_TAG_RE.search(lowered) or _PARAM_RE.search(lowered)
    if not match:
        return 0
    billions = float(match.group(1))
    quant = "q4"
    quant_match = _QUANT_RE.search(lowered)
    if quant_match:
        key = quant_match.group(1).lower()[:2]
        quant = key if key in _BYTES_PER_PARAM else "q4"
    return int(billions * _BYTES_PER_PARAM[quant])


def describe_hardware_limits(hardware: HardwareInfo) -> list[str]:
    """Plain sentences about what this machine can and cannot run."""
    lines: list[str] = []
    gpu = hardware.primary_gpu
    if gpu is not None:
        lines.append(
            f"About {hardware.vram_budget_gb:.1f} GB of the GPU's {gpu.vram_gb:.1f} GB is realistically usable "
            "for a model (the desktop needs the rest)."
        )
    else:
        lines.append("No usable GPU was found, so models run on the CPU: slower, but they still work.")
    lines.append(
        f"RAM budget for a model: about {hardware.ram_budget_gb:.1f} GB "
        f"({hardware.ram_available_gb:.1f} GB free of {hardware.ram_total_gb:.1f} GB)."
    )
    if gpu is not None and hardware.vram_budget_gb >= 4.5:
        lines.append("3B models run well; 7-8B models at Q4 are usable with some layers on the CPU.")
    elif hardware.vram_budget_gb >= 2.5:
        lines.append("1-3B models are comfortable; 7B models will be slow.")
    else:
        lines.append("Stick to 1-3B models, and expect them to be slow if they spill into RAM.")
    lines.append(
        f"Free disk space: {hardware.disk_free_gb:.0f} GB ({hardware.disk_total_gb:.0f} GB total) - "
        "a 4.7 GB model download needs room to spare."
    )
    if hardware.ram_total_gb < 12:
        lines.append("With this much RAM, running other heavy apps while a model is loaded will be tight.")
    return lines


def recommendations(
    hardware: HardwareInfo,
    installed: Sequence[str] = (),
    *,
    limit: int = 4,
    roles: Sequence[str] | None = None,
    include_installed: bool = False,
) -> list[dict[str, Any]]:
    """Suggest models that suit this machine.

    Returns at least one entry whenever the catalogue has anything to say, so the
    caller never has to fall back to a misleading "everything is installed".
    """
    have = {_normalise(name).split(":")[0] for name in installed}
    wanted = {role.lower() for role in roles or ()}
    scored: list[tuple[int, ModelSuggestion, str]] = []

    for entry in CATALOGUE:
        if not include_installed and entry.name.split(":")[0] in have:
            continue
        if ROLE_EMBEDDING in entry.roles and not wanted:
            continue
        if wanted and not wanted.intersection(entry.roles):
            continue
        level = fit_for_model(hardware, entry.size_gb)
        score = {"good": 0, "partial": 1, "cpu": 2, "too_big": 3}[level]
        if ROLE_BALANCED in entry.roles:
            score -= 1
        scored.append((max(0, score), entry, level))

    if not scored:
        return []

    nothing_fits = all(level == "too_big" for _score, _entry, level in scored)
    if nothing_fits:
        # Suggest the *smallest* options first and say so, rather than leading
        # with the biggest thing the machine cannot hold.
        scored.sort(key=lambda item: item[1].size_bytes)
        note = (
            "Nothing in the catalogue fits this machine comfortably. These are the smallest "
            "options; expect them to be slow, and close other heavy apps first."
        )
    else:
        scored.sort(key=lambda item: (item[0], item[1].size_bytes))
        note = ""

    results: list[dict[str, Any]] = []
    for _score, entry, _level in scored[: max(1, int(limit))]:
        payload = entry.to_dict(hardware)
        payload["why"] = _why(entry, hardware, payload["fit"]["level"])
        if note:
            payload["caution"] = note
        results.append(payload)
    return results


def _why(entry: ModelSuggestion, hardware: HardwareInfo, level: str) -> str:
    base = {
        "good": f"Runs inside your GPU memory ({hardware.vram_budget_gb:.1f} GB usable).",
        "partial": "Partly on the GPU, partly on the CPU - still usable.",
        "cpu": "Runs on the CPU, so expect a wait between question and answer.",
        "too_big": "This is larger than this machine handles well; pick it only if you upgrade.",
    }[level]
    if entry.strengths:
        base += f" {entry.strengths[0].capitalize()}."
    return base


def pick_default_model(hardware: HardwareInfo, installed: Sequence[str]) -> str:
    """Choose a sensible default from what is already installed."""
    available = {_normalise(name): name for name in installed}
    for preferred in DEFAULT_LADDER:
        base = preferred.split(":")[0]
        for lowered, original in available.items():
            if lowered.split(":")[0] == base:
                return original
    for lowered, original in available.items():
        entry = find_suggestion(lowered)
        if entry is None or ROLE_EMBEDDING not in entry.roles:
            return original
    suggestion = recommendations(hardware, installed, limit=1)
    return suggestion[0]["name"] if suggestion else ""


def size_warning(hardware: HardwareInfo, size_bytes: float) -> str:
    """Honest warning when a model will not run well here, else an empty string."""
    size_gb = float(size_bytes or 0) / GB
    level = fit_for_model(hardware, size_gb)
    if level == "too_big":
        return (
            f"That model needs about {size_gb:.1f} GB, more than this machine can spare "
            f"({hardware.vram_budget_gb:.1f} GB VRAM / {hardware.ram_budget_gb:.1f} GB RAM budget). "
            "It will be very slow, or it may fail to load."
        )
    if level == "cpu":
        return "This model will run on the CPU: usable, but noticeably slower than a smaller one."
    if level == "partial":
        return "Only part of this model fits in GPU memory, so some layers will run on the CPU."
    return ""


def by_role(role: str) -> list[ModelSuggestion]:
    return [entry for entry in CATALOGUE if role in entry.roles]


def all_names() -> list[str]:
    return [entry.name for entry in CATALOGUE]


def catalogue_summary() -> str:
    """One paragraph for the docs and the settings screen."""
    return (
        f"{len(CATALOGUE)} models are described in the catalogue, from {min(e.size_gb for e in CATALOGUE):.1f} GB "
        f"to {max(e.size_gb for e in CATALOGUE):.1f} GB. Sizes are the default Q4 tags; other quantisations differ."
    )
