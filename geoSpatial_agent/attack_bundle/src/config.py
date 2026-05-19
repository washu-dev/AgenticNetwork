"""Configuration and API key management for DeepSearch-Geo."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

_ENV_LOADED = False


def _ensure_env() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(env_path)
        _ENV_LOADED = True


def get_google_api_key() -> str:
    _ensure_env()
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not key:
        raise EnvironmentError(
            "GOOGLE_API_KEY not set. "
            "Create a .env file from .env.example or export the variable."
        )
    return key


def get_openai_api_key() -> str:
    _ensure_env()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise EnvironmentError(
            "OPENAI_API_KEY not set. "
            "Add it to .env or export the variable."
        )
    return key


def get_maps_api_key() -> str:
    _ensure_env()
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        key = get_google_api_key()
    return key


# Provider constants
PROVIDER_GEMINI = "gemini"
PROVIDER_OPENAI = "openai"

# Known model shortcuts
GEMINI_MODELS = {
    "gemini-3-pro-preview": "models/gemini-3-pro-preview",
    "gemini-3-flash-preview": "models/gemini-3-flash-preview",
    "gemini-2.5-pro": "models/gemini-2.5-pro",
    "gemini-2.5-flash": "models/gemini-2.5-flash",
}

OPENAI_MODELS = {
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.4": "gpt-5.4",
    "gpt-4.1-mini": "gpt-4.1-mini",
    "gpt-4.1": "gpt-4.1",
}


def detect_provider(model: str) -> str:
    """Infer provider from model name."""
    if model.startswith("gpt-") or model.startswith("o"):
        return PROVIDER_OPENAI
    return PROVIDER_GEMINI


@dataclass
class GeoConfig:
    model: str = "gemini-3-flash-preview"
    image_condition: str = ""
    verbose: bool = True
    # Cost controls
    max_image_dim: int = 0             # resize longest side before upload (0 = no resize; default off — preserves sign legibility)
    image_in_step2: bool = True         # include image in Step 2 call (False = big cost save)
    compact_clue_json: bool = True      # minified JSON when forwarding clues to Step 2
    send_raw_clue_json: bool = True     # forward raw Stage-1 JSON to Stage-2 (default ON)
    openai_image_detail: str = "auto"   # "auto" | "low" | "high" for OpenAI image input
    # Always-on pipeline features (A: sort clues, C: multi-candidate at L2/L3).
    # The confidence threshold below which up to 3 alternative candidates are
    # returned in the final GeoResult. Pure prompt/parse, no extra API calls.
    alternatives_threshold: float = 0.75
    # (D) Forward-geocode sanity check — snaps coords to geocoded description
    # when they disagree. REQUIRES Google Maps API. Default OFF to keep the
    # pipeline Maps-free on OpenAI. ~$0.005 per image when enabled.
    enable_forward_geocode_check: bool = False
    # (E) Optional final-adjustment pass (third LLM call). Gated by precision
    # level — only runs when the answer is at street-level or finer.
    enable_final_adjustment: bool = False
    final_adjustment_min_level: str = "street"
    final_adjustment_no_regression: bool = True
    # When E is on, additionally inject Google Maps evidence (reverse-geocode +
    # nearby POIs) into the adjustment prompt. Requires Maps API. Default OFF
    # so E works without Maps by default.
    enable_maps_in_final_adjustment: bool = False
    # Reasoning-trace capture (zero-cost when off). When `save_trace` is True,
    # the pipeline populates `GeoResult.trace` with structured artifacts from
    # every stage (parsed clue JSON, parsed chain-of-thought, final answer,
    # optional 3a/3b dumps). When `save_trace_raw` is also True, the raw model
    # output text is included for each LLM call as well — useful for debugging
    # parse failures, but ~5x larger on disk.
    save_trace: bool = False
    save_trace_raw: bool = False
    progress_callback: Callable[[dict[str, Any]], None] | None = None

    @property
    def provider(self) -> str:
        return detect_provider(self.model)
