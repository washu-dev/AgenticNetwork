"""DeepSearch-Geo: single 2-call geolocation pipeline (provider-agnostic).

Step 1 - Clue extraction (structured visual analysis; no web search)
Step 2 - Coarse-to-fine geolocation with web grounding. The model commits to
         one best hypothesis at each level (country / state / city /
         neighborhood / street), with self-consistency checks and calibrated
         confidence. Runners-up are recorded in `final_answer.alternatives`
         when the final confidence is below `alternatives_threshold`.

Optional extensions (off by default; set via GeoConfig flags):
  - Forward-geocode snap (D)      : requires Google Maps API; snaps coords to
    geocoded description when the two disagree by more than a precision-aware
    threshold. Catches the "right place, wrong numbers" failure mode.
  - Final-adjustment pass (E)     : a third LLM call that verifies and, if
    needed, corrects the candidate. Uses Google Maps evidence when Maps is
    enabled, plain clue-only verification otherwise.

Backends (selected via GeoConfig.provider):
  - Gemini (google-genai)         : Google Search grounding
  - OpenAI (gpt-5.4-mini etc.)    : web_search_preview tool via Responses API
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
import time
from collections.abc import Callable
from typing import Any

from config import GeoConfig, PROVIDER_GEMINI, PROVIDER_OPENAI
from models import GeoResult
from tools import (
    forward_geocode,
    load_image_bytes,
    maps_search_nearby,
    reverse_geocode,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts (shared across providers)
# ---------------------------------------------------------------------------

CLUE_EXTRACTION_PROMPT = """\
You are a world-class geographic analyst. Examine this image carefully and extract \
structured geolocation clues. Do NOT guess a location yet. Only extract observable evidence.

For each clue, provide:
- type: one of [language_script, signage, POI_landmark, vehicle_style, vehicle_registration, \
architecture, residential_architecture, road_infrastructure, materials_palette, \
micro_infrastructure, built_form_morphology, parking_regulations, waste_management, \
property_boundary_landscaping, flora, fauna, climate, terrain, sky_weather, solar_temporal, \
environment_type, urban_density, negative_region, cue_consistency_conflict, other]
- value: what you observe
- confidence: 0.0 to 1.0

Clue type guidance:
- flora: native/visible plants — tree species, shrub types, grass, crops, moss, lichens. \
Call out regionally diagnostic species (palm, olive, Japanese maple, Monterey cypress, redwood, \
eucalyptus, saguaro cactus) and planted-vs-wild distinction when possible.
- fauna: animals visible or typical to the area — birds, livestock, pets, wildlife, insects
- climate: climate zone indicators — humidity cues, snow/ice, arid/lush ground, seasonal state
- solar_temporal: sun position, shadow direction/length, golden-hour tint, season-of-day indicators
- materials_palette: dominant surface materials (e.g. red brick, limestone, corrugated metal, tile roof, thatched)
- architecture: non-residential or mixed styles — institutional, commercial, industrial, ecclesiastical
- residential_architecture: house style and era — Tudor, ranch, split-level, craftsman, bungalow, \
Spanish-revival, Cape Cod, Victorian, Queen Anne, modern infill, mid-century, McMansion. Also note \
roof elements (gable / hip / flat / dormer / solar panel layout), facade (brick color, stucco, \
board-and-batten, multi-material), entry structure (pergola, front porch, porch swing), \
balcony / window styles (window AC, bay window, dormer), accessory structures (detached garage, \
carport, garage door style)
- built_form_morphology: building footprint pattern, setback from road, roof pitch/shape, \
floor count, facade rhythm, lot subdivision pattern, yard / house ratio, garage orientation
- road_infrastructure: curb type/material (rounded concrete, granite, stamped), sidewalk width \
and grass strip, pedestrian sloping, road marking standards, intersection layout, bike lane \
markings, street slope, shoulder style
- micro_infrastructure: small street furniture — bollards, manhole cover design, fire hydrant \
color, utility pole (wooden / concrete / underground utilities), bin design, bench style, \
streetlight fixtures, parking meters, mailbox (cluster, standalone), bus shelter design
- signage: any visible text or pictorial signs — directional route signs, business names, \
institutional markers (school yard sign, university logo, church signboard), house number \
plaques, special zone signs ("Pedestrian Priority Zone", railroad crossing, construction signage)
- parking_regulations: permit zone numbers, residential parking signage with ward / precinct / \
district identifiers, time-restriction signs, street-sweeping schedules — these are often \
city-specific and strongly diagnostic at the neighborhood / ward level
- waste_management: trash / recycling / compost bin color schemes, city-branded bin logos \
(e.g. Recology), bin placement patterns, collection-day markings — often uniquely identify a \
specific municipality
- vehicle_registration: license plate format, color scheme, state slogan or motto, plate \
layout, temporary-tag style — typically resolves state / province / country
- vehicle_style: non-plate car features — vehicle makes/models popular in the region, \
car-share service branding (Zipcar, Car2Go), rideshare stickers, steering side
- property_boundary_landscaping: front-yard fence style (white picket, wrought iron, pink \
slump stone, brick wall), hedges (triangular hedge, spiral topiary), driveway layout \
(decorative concrete, gravel, paver), front-yard landscaping (desert landscaping, greenbelt, \
mulch, flowering trees), shrub / tree placement
- environment_type: coastal, riverine, mountainous, desert, plains, lakeside, island, etc.
- cue_consistency_conflict: flag any pair of clues that contradict each other (e.g. \
tropical vegetation but European signage) — report both the conflicting clues and your assessment

Also provide:
- likely_continents: ranked list of most likely continents
- likely_hemisphere: north/south/equatorial
- urban_or_rural: urban / suburban / rural
- driving_side: left / right / unknown
- overall_region_guess: your best broad region guess (e.g. "Western Europe", "East Asia")
- reasoning: brief chain of thought

Return ONLY valid JSON with this structure:
{
  "clues": [{"type": "...", "value": "...", "confidence": 0.9}, ...],
  "likely_continents": ["..."],
  "likely_hemisphere": "...",
  "urban_or_rural": "...",
  "driving_side": "...",
  "overall_region_guess": "...",
  "reasoning": "..."
}
"""

FINAL_ADJUSTMENT_PROMPT = """\
You are verifying and, if needed, correcting a proposed geolocation using the
image clues{maps_evidence_clause}.

## Candidate answer (from the previous step)
- latitude:        {latitude}
- longitude:       {longitude}
- description:     {description}
- precision_level: {precision_level}
- rationale:       {rationale}

## Evidence from the image (Stage-1 clues, sorted by confidence)
```json
{clue_raw_json}
```
{maps_evidence_block}
## Your task

Cross-check the candidate against the evidence above, then return a verdict:

1. **confirmed** - description and coords agree with the evidence; optionally
   refine the coordinate (e.g. snap to the storefront or intersection rather
   than the city centroid).

2. **corrected** - the description is likely correct but the coordinates
   point elsewhere; trust the description and supply coords that match it.

3. **refined** - both coords and description are roughly right but need a
   small tweak (tighten description, adjust precision_level).

4. **downgraded** - evidence contradicts the claimed precision; drop
   precision_level to the highest level you can honestly justify (e.g. "city"
   or "state/district") and adjust coordinates accordingly.

Never claim higher precision than the combined evidence supports.

Return ONLY valid JSON:
{{
  "verdict": "confirmed" | "refined" | "corrected" | "downgraded",
  "latitude": 0.0,
  "longitude": 0.0,
  "description": "...",
  "precision_level": "building/POI" | "street" | "neighborhood" | "city" | "state/district" | "country",
  "confidence": 0.0,
  "rationale": "Short. Cite which clues (and, if present, which Maps evidence) support or contradict the candidate."
}}
"""


COARSE_TO_FINE_PROMPT = """\
You are a world-class geolocation expert. Your task is to identify where this \
photo was taken by performing a systematic coarse-to-fine geographic search, \
committing to one best hypothesis at each level before zooming further in.

## Extracted Clues from the Image (human summary, sorted by confidence)
{clue_summary}

## Raw Stage 1 JSON (clues sorted by confidence; higher-confidence first)
```json
{clue_raw_json}
```

## Instructions

Work through the levels IN ORDER. At each level, use the image, ALL the clues \
above (including the raw JSON), and web-search results to narrow the location. \
Append reasoning and evidence at each level before moving down. Commit to a \
single best hypothesis at each level — spend your search budget going DEEPER on \
that hypothesis (e.g., reading visible storefront names, looking up specific \
landmarks) rather than listing parallel alternatives mid-chain.

Important rules:
- Weight each clue by its confidence; do not silently ignore low-confidence ones.
- Respect "negative_region" clues: ruled-out regions stay ruled out unless later \
evidence clearly overrides them.
- If any "cue_consistency_conflict" is present, explicitly name it and decide \
which side to trust before committing to a country.
- Commit to ONE hypothesis at every level. Use your web search budget to \
verify and deepen the chosen hypothesis (especially reading any visible text, \
storefront names, or distinctive features) rather than enumerating runners-up.
- Do not overclaim precision. Use the confidence rubric below.

### Confidence rubric (use these anchors, don't invent your own scale)
- 0.90-1.00 = you identified a specific POI with a visible sign, unambiguous \
landmark, or distinctive storefront; a person familiar with the place would \
recognize it from the image.
- 0.70-0.89 = correct city and likely correct neighborhood, but not the exact \
building or street.
- 0.45-0.69 = correct country and state/region, uncertain on the specific city.
- 0.20-0.44 = only country or macro-region level confidence; could be any of \
several neighboring countries.
- 0.00-0.19 = continent, climate zone, or very broad region only.

### Description anchor rule
The final `description` MUST include `{{city or town}}, {{state/region}}, \
{{country}}` at minimum, PLUS at least one of: a street name, a POI/landmark \
name, or a distinctive identifying feature. Avoid vague descriptions like \
"coastal town in Portugal" — say "Cascais (western Lisbon region), Portugal; \
seaside boardwalk near Praia da Rainha" instead.

### Level 1: Country (commit to one)
Identify the most likely country. State hypothesis, evidence, confidence. \
Use web search to verify language/script, architecture style, road \
conventions when needed.

### Level 2: State / District / Province (commit to one)
Within the chosen country, commit to the single most likely state/province/ \
region. Use regional visual cues (climate, vegetation, terrain, urban \
density, regulatory/signage conventions that vary by region). \
State hypothesis, evidence, confidence in [0, 1].

### Level 3: City / Town (commit to one)
Within the chosen state, commit to the single most likely city or town. \
Focus on municipal-scale signals — transit system branding, chain stores \
present/absent, street grid shape, characteristic vehicles, language \
register, municipal street furniture. State hypothesis, evidence, \
confidence.

### Level 4: Neighborhood / ZIP / Ward (commit to one)
Within the chosen city, commit to the single most likely sub-city region. \
This level is NOT about naming an exact building — it is about the local \
environment a resident would recognize as "my neighborhood". Diagnostic \
cues at this scale:
- waste-management branding and bin-color scheme (often municipality- or \
ward-specific),
- parking-permit zone numbers and street-sweeping schedules,
- residential architectural style and era (craftsman vs mid-century infill \
vs row-house vs postwar bungalow) and predominant siding/roof materials,
- school-district signage, ward/precinct markers, council-district flags,
- ZIP-code or postal-district hints on mail, flyers, permit stickers.
State hypothesis, evidence, confidence.

### Level 5: Street / Building / POI (commit to one)
Within the best L4 candidate, pinpoint the most specific location possible. \
Look for exact building names, hotel/restaurant/store signage, monuments, \
distinctive facades, intersection names, and house numbers. Use web search \
on any visible text or distinctive feature. If you cannot identify a \
specific building or street, it is correct to leave L5 at neighborhood \
precision rather than invent a POI.

### Final Judgment
Synthesize the entire chain. Assess coherence across levels — does every \
level support every other? Pay special attention to the L3-L4 handoff: \
if the chosen neighborhood doesn't fit the chosen city (e.g. a Brooklyn- \
style brownstone block under a "Los Angeles" city pick), downgrade the \
city and reconsider. If at any level you realize the evidence genuinely \
supports a different hypothesis than the one you picked, briefly note \
what the runner-up would have been — this is what populates the \
`alternatives` field below. Do NOT restart the chain; just record that \
plausible runner-up as an alternative.

### Coordinate sanity check (do this BEFORE emitting the final answer)
After you pick `(latitude, longitude)`, silently verify:
  1. Do the coords fall inside the country you committed to?
  2. Do they fall inside the state/region you committed to?
  3. Do they fall within ~10 km of the city/town centroid you named?
  4. Do they fall inside (or very near) the neighborhood/ward you named?
If any check fails, the description and coords disagree - this almost always \
means the coords are wrong. Correct the coords to match the named place \
(use the neighborhood centroid if unsure of an exact street, or the city \
centroid if unsure of a neighborhood) BEFORE emitting the final answer.

Then produce a single `final_answer`:
- `precision_level`: the highest level you can honestly justify.
- `confidence`: a calibrated value per the rubric above.
- `latitude` and `longitude`: the best point estimate (after the sanity check).
- `alternatives`: if `confidence < {alt_threshold:.2f}`, fill this with up to 3 \
next-best candidates (the runners-up you noted in Final Judgment), each with \
description, coords, and confidence. Otherwise you MAY leave `alternatives` \
as an empty list.

## Output Format

Return ONLY valid JSON:
{{
  "chain_of_thought": {{
    "country": {{
      "hypothesis": "...",
      "evidence": "...",
      "confidence": 0.0
    }},
    "state_district": {{
      "hypothesis": "...",
      "evidence": "...",
      "confidence": 0.0
    }},
    "city_town": {{
      "hypothesis": "...",
      "evidence": "...",
      "confidence": 0.0
    }},
    "neighborhood_ward": {{
      "hypothesis": "...",
      "evidence": "...",
      "confidence": 0.0
    }},
    "street_poi": {{
      "hypothesis": "...",
      "evidence": "...",
      "confidence": 0.0
    }}
  }},
  "final_answer": {{
    "latitude": 0.0,
    "longitude": 0.0,
    "description": "{{city}}, {{region}}, {{country}}; plus neighborhood / street / POI / distinctive feature",
    "precision_level": "building/POI" or "street" or "neighborhood" or "city" or "state/district" or "country",
    "confidence": 0.0,
    "rationale": "Brief synthesis citing the strongest clues.",
    "alternatives": [
      {{"description": "...", "latitude": 0.0, "longitude": 0.0, "confidence": 0.0}}
    ]
  }}
}}
"""


def _parse_json(text: str) -> dict:
    """Extract JSON from model output, tolerating markdown fences."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m2 = re.search(r"\{.*\}", text, re.DOTALL)
        if m2:
            try:
                return json.loads(m2.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _emit_progress(callback: Callable[[dict[str, Any]], None] | None, **event: Any) -> None:
    if callback is None:
        return
    try:
        callback({key: value for key, value in event.items() if value is not None})
    except Exception as exc:
        log.debug("Geo pipeline progress callback failed: %s", exc)


def _stream_chunk_callback(
    callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    model: str,
) -> Callable[[str], None] | None:
    if callback is None:
        return None
    buffer = ""

    def emit_chunk(text: str, *, final: bool = False) -> None:
        nonlocal buffer
        if text:
            buffer += text
        if not buffer:
            return
        if not final and len(buffer) < 180 and "\n" not in buffer:
            return
        chunk = buffer
        buffer = ""
        _emit_progress(
            callback,
            kind="geospatial_model_stream",
            event="model_output_chunk",
            stage=stage,
            model=model,
            text=chunk,
        )

    def on_chunk(text: str) -> None:
        emit_chunk(text)

    setattr(on_chunk, "flush", lambda: emit_chunk("", final=True))
    setattr(on_chunk, "progress_callback", callback)
    setattr(on_chunk, "stage", stage)
    setattr(on_chunk, "model", model)
    return on_chunk


def _object_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _coerce_token_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if not usage:
        return {}
    if isinstance(usage, dict):
        return usage
    for method_name in ("model_dump", "dict"):
        method = getattr(usage, method_name, None)
        if callable(method):
            try:
                dumped = method()
            except TypeError:
                continue
            if isinstance(dumped, dict):
                return dumped
    return {
        key: getattr(usage, key)
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "prompt_token_count",
            "candidates_token_count",
            "total_token_count",
        )
        if hasattr(usage, key)
    }


def _first_token_count(raw: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = _coerce_token_count(raw.get(name))
        if value is not None:
            return value
    return None


def _response_token_usage(response: Any) -> dict[str, Any]:
    usage = _object_get(response, "usage") or _object_get(response, "usage_metadata")
    raw = _usage_to_dict(usage)
    if not raw:
        return {"usage_available": False}
    input_tokens = _first_token_count(raw, "input_tokens", "prompt_tokens", "prompt_token_count")
    output_tokens = _first_token_count(
        raw,
        "output_tokens",
        "completion_tokens",
        "completion_token_count",
        "candidates_token_count",
    )
    total_tokens = _first_token_count(raw, "total_tokens", "total_token_count")
    input_details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    cached_input_tokens = (
        _coerce_token_count(input_details.get("cached_tokens"))
        if isinstance(input_details, dict)
        else None
    )
    return {
        "usage_available": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
    }


def _emit_model_token_usage(
    stream_callback: Callable[[str], None] | None,
    response: Any,
    *,
    model: str,
    provider: str,
    streamed: bool,
) -> None:
    progress_callback = getattr(stream_callback, "progress_callback", None)
    if progress_callback is None:
        return
    stage = getattr(stream_callback, "stage", "image_analysis")
    usage = _response_token_usage(response)
    _emit_progress(
        progress_callback,
        kind="geospatial_token_usage",
        event="image_analysis_token_usage",
        text=f"Image analysis token usage recorded for {stage}.",
        usage_scope="image_analysis_model_call",
        stage=stage,
        model=model,
        provider=provider,
        streamed=streamed,
        **usage,
    )


def _log(msg: str, verbose: bool = True) -> None:
    if verbose:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Precision-level helper (used to gate the final-adjustment pass)
# ---------------------------------------------------------------------------

_PRECISION_ORDER = {
    "country": 0,
    "state/district": 1,
    "state_district": 1,
    "region": 1,
    "city": 2,
    "city/town": 2,
    "city_town": 2,
    "town": 2,
    "neighborhood": 3,
    "street": 4,
    "building/POI": 5,
    "poi": 5,
    "building": 5,
}


def _precision_rank(level: str) -> int:
    if not level:
        return 0
    return _PRECISION_ORDER.get(level.strip().lower(), 0)


# ---------------------------------------------------------------------------
# Helpers for A (sort clues) / D (forward-geocode snap)
# Alternatives in final_answer are parsed via _format_alternatives.
# ---------------------------------------------------------------------------

# Precision-aware thresholds for the forward-geocode sanity check (in km).
# If the model's self-reported coords differ from the forward-geocoded coords
# of its description by MORE than this, we trust the description (snap).
_FG_THRESHOLDS_KM = {
    "country":         500.0,
    "state/district":  200.0,
    "state_district":  200.0,
    "region":          200.0,
    "city":             50.0,
    "city/town":        50.0,
    "city_town":        50.0,
    "town":             50.0,
    "neighborhood":     15.0,
    "street":            5.0,
    "building/POI":      2.0,
    "poi":               2.0,
    "building":          2.0,
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    r = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2.0) ** 2
    )
    return 2.0 * r * math.asin(math.sqrt(a))


def _sort_clues_by_confidence(clue_data: dict) -> dict:
    """Return a shallow copy of Stage-1 output with `clues` sorted by conf desc."""
    clues = clue_data.get("clues")
    if not isinstance(clues, list):
        return clue_data
    sorted_clues = sorted(
        clues,
        key=lambda c: (
            float(c.get("confidence", 0.0))
            if isinstance(c.get("confidence"), (int, float))
            else 0.0
        ),
        reverse=True,
    )
    out = dict(clue_data)
    out["clues"] = sorted_clues
    return out


def _flatten_cot_chain(cot: dict) -> list[str]:
    """Flatten chain_of_thought into log-friendly lines.
    Current schema is single-hypothesis per level across all 5 levels. Older
    traces on disk may still use the legacy multi-candidate schema
    (`candidates: [...]`) or the legacy 4-level key `neighborhood_poi`;
    both are handled transparently for backwards compatibility."""
    out: list[str] = []
    levels = (
        "country",
        "state_district",
        "city_town",
        "neighborhood_ward",
        "street_poi",
        # legacy fallback keys
        "neighborhood_poi",
    )
    seen = set()
    for level in levels:
        node = cot.get(level, {})
        if not node or level in seen:
            continue
        seen.add(level)
        if "candidates" in node and isinstance(node["candidates"], list):
            for i, cand in enumerate(node["candidates"], start=1):
                h = cand.get("hypothesis", "?")
                c = cand.get("confidence", "?")
                out.append(f"[{level} #{i}] {h} (conf={c})")
        else:
            h = node.get("hypothesis", "?")
            c = node.get("confidence", "?")
            out.append(f"[{level}] {h} (conf={c})")
    return out


def _format_alternatives(raw_alts: list[dict]) -> list[dict]:
    """Normalize LLM-returned alternatives into GeoResult.alternatives shape."""
    out: list[dict] = []
    for a in raw_alts[:3]:
        try:
            lat = float(a.get("latitude", 0.0))
            lon = float(a.get("longitude", 0.0))
        except (TypeError, ValueError):
            lat, lon = 0.0, 0.0
        out.append({
            "description": str(a.get("description", "Unknown")),
            "coordinates": (lat, lon),
            "confidence": float(a.get("confidence", 0.0) or 0.0),
            "precision_level": str(a.get("precision_level", "city")),
        })
    return out


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------

class _GeminiBackend:
    """Wraps google-genai SDK."""

    def __init__(self, model: str):
        from google import genai
        from google.genai import types
        from config import get_google_api_key

        self._genai = genai
        self._types = types
        self.model = model
        self.client = genai.Client(api_key=get_google_api_key())
        self._cached_file: types.File | None = None

    def upload_image(self, image_bytes: bytes) -> None:
        if self._cached_file is not None:
            return
        buf = io.BytesIO(image_bytes)
        self._cached_file = self.client.files.upload(
            file=buf, config={"mime_type": "image/jpeg"},
        )

    def cleanup(self) -> None:
        """Delete the uploaded file from Gemini servers (otherwise expires in 48h)."""
        if self._cached_file is not None:
            try:
                self.client.files.delete(name=self._cached_file.name)
            except Exception:
                pass
            self._cached_file = None

    def call(
        self,
        prompt: str,
        *,
        include_image: bool = False,
        use_web_search: bool = False,
        stream_callback: Callable[[str], None] | None = None,
    ) -> str:
        types = self._types
        contents: list = []
        if include_image and self._cached_file:
            contents.append(types.Part.from_uri(
                file_uri=self._cached_file.uri, mime_type="image/jpeg",
            ))
        contents.append(types.Part.from_text(text=prompt))

        kwargs: dict[str, Any] = {}
        if use_web_search:
            kwargs["config"] = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            )

        if stream_callback is not None and hasattr(self.client.models, "generate_content_stream"):
            try:
                chunks: list[str] = []
                usage_response = None
                for chunk in self.client.models.generate_content_stream(
                    model=self.model, contents=contents, **kwargs,
                ):
                    if getattr(chunk, "usage_metadata", None) is not None:
                        usage_response = chunk
                    text = getattr(chunk, "text", None) or ""
                    if not text:
                        continue
                    chunks.append(text)
                    stream_callback(text)
                flush = getattr(stream_callback, "flush", None)
                if callable(flush):
                    flush()
                _emit_model_token_usage(
                    stream_callback,
                    usage_response,
                    model=self.model,
                    provider=PROVIDER_GEMINI,
                    streamed=True,
                )
                return "".join(chunks)
            except Exception as exc:
                log.debug("Gemini streaming call failed; falling back to non-streaming: %s", exc)

        resp = self.client.models.generate_content(
            model=self.model, contents=contents, **kwargs,
        )
        _emit_model_token_usage(
            stream_callback,
            resp,
            model=self.model,
            provider=PROVIDER_GEMINI,
            streamed=False,
        )
        return resp.text or ""


# ---------------------------------------------------------------------------
# OpenAI backend
# ---------------------------------------------------------------------------

class _OpenAIBackend:
    """Wraps OpenAI Responses API with web_search_preview."""

    def __init__(self, model: str, image_detail: str = "auto"):
        from openai import OpenAI
        from config import get_openai_api_key

        self.model = model
        self.client = OpenAI(api_key=get_openai_api_key())
        self._image_b64: str | None = None
        self._image_detail = image_detail

    def upload_image(self, image_bytes: bytes) -> None:
        self._image_b64 = base64.b64encode(image_bytes).decode()

    def cleanup(self) -> None:
        """Drop the in-memory base64 copy."""
        self._image_b64 = None

    def call(
        self,
        prompt: str,
        *,
        include_image: bool = False,
        use_web_search: bool = False,
        stream_callback: Callable[[str], None] | None = None,
    ) -> str:
        content: list[dict[str, Any]] = []
        if include_image and self._image_b64:
            img_part: dict[str, Any] = {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{self._image_b64}",
            }
            if self._image_detail and self._image_detail != "auto":
                img_part["detail"] = self._image_detail
            content.append(img_part)
        content.append({"type": "input_text", "text": prompt})

        tools = []
        if use_web_search:
            tools.append({"type": "web_search_preview"})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": [{"role": "user", "content": content}],
        }
        if tools:
            kwargs["tools"] = tools

        resp = self.client.responses.create(**kwargs)
        _emit_model_token_usage(
            stream_callback,
            resp,
            model=self.model,
            provider=PROVIDER_OPENAI,
            streamed=False,
        )
        return resp.output_text or ""


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

class SimpleGeo:
    """Simplified 2-call geolocation pipeline (provider-agnostic)."""

    def __init__(self, config: GeoConfig | None = None):
        self.cfg = config or GeoConfig()
        if self.cfg.provider == PROVIDER_OPENAI:
            self._backend = _OpenAIBackend(
                self.cfg.model, image_detail=self.cfg.openai_image_detail,
            )
        else:
            self._backend = _GeminiBackend(self.cfg.model)

    def geolocate(self, image_path: str) -> GeoResult:
        """Run the 2-call pipeline (with optional D and E extensions)."""
        cfg = self.cfg
        _log(f"\nSimpleGeo starting on: {image_path}", cfg.verbose)
        _log(f"Provider: {cfg.provider}  Model: {cfg.model}\n", cfg.verbose)

        # Optional reasoning trace (populated only when cfg.save_trace).
        trace: dict[str, Any] = {}
        if cfg.save_trace:
            trace = {
                "image_path": image_path,
                "model": cfg.model,
                "provider": cfg.provider,
                "config": {
                    "image_in_step2": cfg.image_in_step2,
                    "send_raw_clue_json": cfg.send_raw_clue_json,
                    "max_image_dim": cfg.max_image_dim,
                    "alternatives_threshold": cfg.alternatives_threshold,
                    "image_condition": cfg.image_condition,
                    "enable_forward_geocode_check":
                        cfg.enable_forward_geocode_check,
                    "enable_final_adjustment": cfg.enable_final_adjustment,
                    "enable_maps_in_final_adjustment":
                        cfg.enable_maps_in_final_adjustment,
                },
            }

        t0 = time.time()
        image_bytes = load_image_bytes(image_path, max_dim=cfg.max_image_dim)
        _log(
            f"  Image bytes: {len(image_bytes) / 1024:.0f} KB"
            + (f" (resized to <= {cfg.max_image_dim}px)"
               if cfg.max_image_dim else ""),
            cfg.verbose,
        )
        self._backend.upload_image(image_bytes)

        # ---- Step 1: Clue Extraction ----
        _log("=" * 60, cfg.verbose)
        _log("STEP 1: Clue Extraction", cfg.verbose)
        _log("=" * 60, cfg.verbose)
        _emit_progress(
            cfg.progress_callback,
            kind="geospatial_inference",
            event="location_stage1_started",
            text="Gemini clue extraction started.",
            stage="clue_extraction",
            model=cfg.model,
        )

        clue_prompt = CLUE_EXTRACTION_PROMPT
        if cfg.image_condition:
            clue_prompt += (
                f"\n\nAdditional context provided about this image: "
                f"{cfg.image_condition}\n"
            )
            _log(f"  Image condition: {cfg.image_condition}", cfg.verbose)

        raw_clues = self._backend.call(
            clue_prompt,
            include_image=True,
            stream_callback=_stream_chunk_callback(
                cfg.progress_callback,
                stage="clue_extraction",
                model=cfg.model,
            ),
        )
        clue_data_raw = _parse_json(raw_clues)

        # (A) Sort clues by confidence before forwarding.
        clue_data = _sort_clues_by_confidence(clue_data_raw)

        if cfg.save_trace:
            trace["stage1_clues"] = clue_data
            if cfg.save_trace_raw:
                trace["stage1_raw"] = raw_clues

        if cfg.compact_clue_json:
            clue_raw_json_compact = json.dumps(
                clue_data, ensure_ascii=False, separators=(",", ":"),
            )
        else:
            clue_raw_json_compact = json.dumps(
                clue_data, indent=2, ensure_ascii=False,
            )

        clue_lines = []
        for c in clue_data.get("clues", []):
            clue_lines.append(
                f"- [{c.get('type', '?')}] {c.get('value', '?')} "
                f"(conf={c.get('confidence', '?')})"
            )
        clue_summary = "\n".join(clue_lines)
        clue_summary += (
            f"\nOverall region guess: "
            f"{clue_data.get('overall_region_guess', 'N/A')}"
        )
        clue_summary += (
            f"\nLikely continents: {clue_data.get('likely_continents', [])}"
        )
        clue_summary += (
            f"\nHemisphere: {clue_data.get('likely_hemisphere', 'unknown')}"
        )
        clue_summary += (
            f"\nUrban/Rural: {clue_data.get('urban_or_rural', 'unknown')}"
        )
        clue_summary += (
            f"\nDriving side: {clue_data.get('driving_side', 'unknown')}"
        )
        clue_summary += f"\nReasoning: {clue_data.get('reasoning', 'N/A')}"

        _log(f"\n{clue_summary}", cfg.verbose)
        _emit_progress(
            cfg.progress_callback,
            kind="geospatial_inference",
            event="location_stage1_completed",
            text=(
                "Gemini clue extraction completed. "
                f"Overall region guess: {clue_data.get('overall_region_guess', 'N/A')}."
            ),
            stage="clue_extraction",
            model=cfg.model,
            clue_count=len(clue_data.get("clues", []) or []),
            overall_region_guess=clue_data.get("overall_region_guess", "N/A"),
        )

        # ---- Step 2: Coarse-to-Fine (web-grounded) ----
        _log(f"\n{'=' * 60}", cfg.verbose)
        _log(
            "STEP 2: Coarse-to-Fine (web-grounded)",
            cfg.verbose,
        )
        _log("=" * 60, cfg.verbose)
        _emit_progress(
            cfg.progress_callback,
            kind="geospatial_inference",
            event="location_stage2_started",
            text="Gemini coarse-to-fine geolocation started.",
            stage="coarse_to_fine",
            model=cfg.model,
        )

        condition_block = ""
        if cfg.image_condition:
            condition_block = (
                f"\n## Additional Context\n{cfg.image_condition}\n"
            )

        if cfg.send_raw_clue_json:
            clue_raw_json = clue_raw_json_compact
        else:
            clue_raw_json = "(omitted to save tokens; use the summary above)"

        prompt = COARSE_TO_FINE_PROMPT.format(
            clue_summary=clue_summary,
            clue_raw_json=clue_raw_json,
            alt_threshold=cfg.alternatives_threshold,
        ) + condition_block
        raw_result = self._backend.call(
            prompt,
            include_image=cfg.image_in_step2,
            use_web_search=True,
            stream_callback=_stream_chunk_callback(
                cfg.progress_callback,
                stage="coarse_to_fine",
                model=cfg.model,
            ),
        )
        parsed = _parse_json(raw_result)

        if cfg.save_trace:
            trace["stage2"] = {
                "chain_of_thought": parsed.get("chain_of_thought", {}),
                "final_answer": parsed.get("final_answer", {}),
            }
            if cfg.save_trace_raw:
                trace["stage2_raw"] = raw_result

        cot = parsed.get("chain_of_thought", {})
        for line in _flatten_cot_chain(cot):
            _log(f"  {line}", cfg.verbose)
            _emit_progress(
                cfg.progress_callback,
                kind="geospatial_inference",
                event="location_hypothesis",
                text=line,
                stage="coarse_to_fine",
                model=cfg.model,
            )

        final = parsed.get("final_answer", {})
        result = GeoResult(
            latitude=float(final.get("latitude", 0.0) or 0.0),
            longitude=float(final.get("longitude", 0.0) or 0.0),
            description=final.get("description", "Unknown"),
            precision_level=final.get("precision_level", "city"),
            confidence=float(final.get("confidence", 0.0) or 0.0),
            rationale=final.get("rationale", ""),
            hypothesis_chain=_flatten_cot_chain(cot),
        )
        _emit_progress(
            cfg.progress_callback,
            kind="geospatial_inference",
            event="location_stage2_completed",
            text=(
                "Gemini coarse-to-fine geolocation completed: "
                f"{result.description} ({result.precision_level}, confidence {result.confidence:.2f})."
            ),
            stage="coarse_to_fine",
            model=cfg.model,
            description=result.description,
            precision_level=result.precision_level,
            confidence=result.confidence,
        )

        # (C) Persist top-3 alternatives when confidence is low.
        raw_alts = final.get("alternatives") or []
        if result.confidence < cfg.alternatives_threshold and raw_alts:
            result.alternatives = _format_alternatives(raw_alts)
            _log(
                f"\n  [alternatives] top-conf={result.confidence:.2f} "
                f"< {cfg.alternatives_threshold}; keeping "
                f"top-{len(result.alternatives)}:",
                cfg.verbose,
            )
            for alt in result.alternatives:
                _log(
                    f"    - {alt['description']} "
                    f"({alt['coordinates'][0]:.4f}, "
                    f"{alt['coordinates'][1]:.4f}) "
                    f"conf={alt['confidence']:.2f}",
                    cfg.verbose,
                )

        # (D) Optional forward-geocode sanity check (requires Maps).
        if cfg.enable_forward_geocode_check and result.description:
            _log(f"\n{'=' * 60}", cfg.verbose)
            _log("STEP 3a: Forward-Geocode Sanity Check", cfg.verbose)
            _log("=" * 60, cfg.verbose)
            try:
                fg = forward_geocode(result.description)
            except Exception as exc:
                fg = {"error": f"exception: {exc}"}
            stage3a_record: dict[str, Any] = {
                "description_input": result.description,
                "forward_geocode_result": fg,
                "snapped": False,
            }
            if fg.get("error"):
                _log(
                    f"  forward_geocode unavailable ({fg.get('error')}); "
                    f"skipping.",
                    cfg.verbose,
                )
            else:
                fg_lat = fg.get("latitude")
                fg_lon = fg.get("longitude")
                if fg_lat is not None and fg_lon is not None:
                    delta = _haversine_km(
                        result.latitude, result.longitude,
                        float(fg_lat), float(fg_lon),
                    )
                    threshold = _FG_THRESHOLDS_KM.get(
                        result.precision_level.strip().lower(), 50.0,
                    )
                    stage3a_record.update({
                        "delta_km": round(delta, 2),
                        "threshold_km": threshold,
                        "candidate_coords_before":
                            (result.latitude, result.longitude),
                    })
                    _log(
                        f"  description -> ({fg_lat:.4f}, {fg_lon:.4f})",
                        cfg.verbose,
                    )
                    _log(
                        f"  delta from candidate coords: {delta:.1f} km "
                        f"(snap threshold: {threshold:.0f} km)",
                        cfg.verbose,
                    )
                    if delta > threshold:
                        _log(
                            "  [SNAP] trusting description; updating coords.",
                            cfg.verbose,
                        )
                        old_lat, old_lon = result.latitude, result.longitude
                        result.latitude = float(fg_lat)
                        result.longitude = float(fg_lon)
                        result.rationale = (
                            f"[forward_geocode_snap delta={delta:.1f}km] "
                            + result.rationale
                        )
                        result.hypothesis_chain = result.hypothesis_chain + [
                            f"[forward_geocode_snap] "
                            f"({old_lat:.4f},{old_lon:.4f}) -> "
                            f"({fg_lat:.4f},{fg_lon:.4f})"
                        ]
                        stage3a_record["snapped"] = True
                        stage3a_record["candidate_coords_after"] = (
                            result.latitude, result.longitude,
                        )
                    else:
                        _log(
                            "  [OK] within threshold, keeping original coords.",
                            cfg.verbose,
                        )
            if cfg.save_trace:
                trace["stage3a_forward_geocode"] = stage3a_record

        # (E) Optional final adjustment pass (with or without Maps evidence).
        threshold_rank = _precision_rank(cfg.final_adjustment_min_level)
        if (cfg.enable_final_adjustment
                and _precision_rank(result.precision_level) >= threshold_rank):
            use_maps = bool(cfg.enable_maps_in_final_adjustment)
            _log(f"\n{'=' * 60}", cfg.verbose)
            _log(
                f"STEP 3b: Final Adjustment "
                f"(precision={result.precision_level} "
                f">= {cfg.final_adjustment_min_level}, "
                f"maps={'on' if use_maps else 'off'})",
                cfg.verbose,
            )
            _log("=" * 60, cfg.verbose)

            maps_evidence_clause = ""
            maps_evidence_block = ""
            if use_maps:
                maps_evidence_clause = (
                    " AND factual Google Maps ground-truth evidence"
                )
                rev_addr = "(reverse geocode unavailable)"
                nearby_str = "(nearby search unavailable)"
                fg_str = "(forward geocode unavailable)"
                try:
                    rev = reverse_geocode(result.latitude, result.longitude)
                    if not rev.get("error"):
                        rev_addr = (
                            rev.get("formatted_address", "")
                            or "(reverse geocode returned no address)"
                        )
                except Exception as exc:
                    rev_addr = f"(reverse geocode failed: {exc})"
                try:
                    nearby = maps_search_nearby(
                        result.latitude, result.longitude,
                        "point of interest", radius=500,
                    )
                    if nearby:
                        lines = []
                        for p in nearby[:8]:
                            name = p.get("name", "?")
                            types = ", ".join((p.get("types") or [])[:3])
                            lines.append(f"- {name} (types: {types})")
                        nearby_str = "\n".join(lines)
                    else:
                        nearby_str = "(no nearby POIs within 500 m)"
                except Exception as exc:
                    nearby_str = f"(nearby search failed: {exc})"
                try:
                    fg2 = forward_geocode(result.description)
                    if not fg2.get("error"):
                        fg_str = (
                            f"formatted_address: "
                            f"{fg2.get('formatted_address', '?')}\n"
                            f"coords: ({fg2.get('latitude', '?')}, "
                            f"{fg2.get('longitude', '?')})"
                        )
                except Exception as exc:
                    fg_str = f"(forward geocode failed: {exc})"
                maps_evidence_block = (
                    f"\n## Verification evidence (factual - from Google Maps)\n"
                    f"\nReverse-geocoded address of the candidate coords "
                    f"({result.latitude:.6f}, {result.longitude:.6f}):\n"
                    f"{rev_addr}\n"
                    f"\nNearby POIs within 500 m of the candidate coords:\n"
                    f"{nearby_str}\n"
                    f"\nForward-geocoded lookup of the description "
                    f"\"{result.description}\" returned:\n"
                    f"{fg_str}\n"
                )

            adj_prompt = FINAL_ADJUSTMENT_PROMPT.format(
                latitude=f"{result.latitude:.6f}",
                longitude=f"{result.longitude:.6f}",
                description=result.description,
                precision_level=result.precision_level,
                rationale=result.rationale,
                clue_raw_json=clue_raw_json_compact,
                maps_evidence_clause=maps_evidence_clause,
                maps_evidence_block=maps_evidence_block,
            )
            try:
                _emit_progress(
                    cfg.progress_callback,
                    kind="geospatial_inference",
                    event="location_final_adjustment_started",
                    text="Gemini final location adjustment started.",
                    stage="final_adjustment",
                    model=cfg.model,
                )
                adj_raw = self._backend.call(
                    adj_prompt,
                    include_image=False,
                    use_web_search=True,
                    stream_callback=_stream_chunk_callback(
                        cfg.progress_callback,
                        stage="final_adjustment",
                        model=cfg.model,
                    ),
                )
                adj = _parse_json(adj_raw)
            except Exception as e:
                _log(f"  [final-adjustment] call failed: {e}", cfg.verbose)
                adj = {}
                adj_raw = ""

            if cfg.save_trace:
                stage3b_record: dict[str, Any] = {
                    "use_maps": use_maps,
                    "before": {
                        "latitude": result.latitude,
                        "longitude": result.longitude,
                        "precision_level": result.precision_level,
                        "confidence": result.confidence,
                        "description": result.description,
                    },
                    "adjustment_response": adj,
                }
                if use_maps:
                    stage3b_record["maps_evidence"] = {
                        "reverse_geocode": rev_addr,
                        "nearby_pois": nearby_str,
                        "forward_geocode": fg_str,
                    }
                if cfg.save_trace_raw:
                    stage3b_record["adjustment_raw"] = adj_raw
                trace["stage3b_final_adjustment"] = stage3b_record

            if adj and "latitude" in adj and "longitude" in adj:
                verdict = str(adj.get("verdict", "refined")).lower()
                new_conf = float(adj.get("confidence", result.confidence) or 0.0)
                _log(f"  verdict: {verdict}", cfg.verbose)
                _log(
                    f"  before: ({result.latitude:.4f}, {result.longitude:.4f}) "
                    f"[{result.precision_level}] conf={result.confidence:.2f}",
                    cfg.verbose,
                )
                _log(
                    f"  after : ({float(adj['latitude']):.4f}, "
                    f"{float(adj['longitude']):.4f}) "
                    f"[{adj.get('precision_level', result.precision_level)}] "
                    f"conf={new_conf:.2f}",
                    cfg.verbose,
                )
                regress = (
                    cfg.final_adjustment_no_regression
                    and verdict == "downgraded"
                    and new_conf <= result.confidence
                )
                if regress:
                    _log(
                        "  [no-regression] keeping pre-adjustment answer.",
                        cfg.verbose,
                    )
                    result.hypothesis_chain = result.hypothesis_chain + [
                        f"[final_adjustment:downgraded_reverted] "
                        f"{str(adj.get('rationale', ''))[:200]}"
                    ]
                else:
                    result = GeoResult(
                        latitude=float(adj["latitude"]),
                        longitude=float(adj["longitude"]),
                        description=str(
                            adj.get("description", result.description)
                        ),
                        precision_level=str(
                            adj.get("precision_level", result.precision_level)
                        ),
                        confidence=new_conf,
                        rationale=(
                            f"[{verdict}] "
                            f"{str(adj.get('rationale', result.rationale))}"
                        ),
                        hypothesis_chain=result.hypothesis_chain + [
                            f"[final_adjustment:{verdict}] "
                            f"{str(adj.get('rationale', ''))[:200]}"
                        ],
                        alternatives=result.alternatives,
                    )

        self._backend.cleanup()

        elapsed = time.time() - t0
        _log(f"\n{'=' * 60}", cfg.verbose)
        _log(f"RESULT (elapsed: {elapsed:.1f}s)", cfg.verbose)
        _log("=" * 60, cfg.verbose)
        _log(str(result), cfg.verbose)

        if cfg.save_trace:
            trace["elapsed_s"] = round(elapsed, 2)
            trace["final"] = {
                "latitude": result.latitude,
                "longitude": result.longitude,
                "description": result.description,
                "precision_level": result.precision_level,
                "confidence": result.confidence,
                "rationale": result.rationale,
                "alternatives": result.alternatives,
                "hypothesis_chain": result.hypothesis_chain,
            }
            result.trace = trace

        return result
