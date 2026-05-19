#!/usr/bin/env python3
"""DeepSearch-Geo: Simplified 2-call image geolocation.

Usage:
    python main_simple.py --image path/to/photo.jpg
    python main_simple.py --image photo.jpg --model gemini-3-flash-preview
    python main_simple.py --image photo.jpg --model gpt-5.4-mini
    python main_simple.py --image photo.jpg --output-json result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(_SRC))
_PROJECT_ROOT = _SRC.parent

from config import GeoConfig, GEMINI_MODELS, OPENAI_MODELS
from pipeline_simple import SimpleGeo


ALL_MODELS = {**GEMINI_MODELS, **OPENAI_MODELS}


def parse_args() -> argparse.Namespace:
    model_list = ", ".join(ALL_MODELS.keys())
    parser = argparse.ArgumentParser(
        description="DeepSearch-Geo: Simplified 2-call Image Geolocation",
    )
    default_img = str(_PROJECT_ROOT / "img" / "demo_washington_dc.png")
    parser.add_argument("--image", type=str, default=default_img,
                        help="Path to the image to geolocate.")
    parser.add_argument("--model", type=str, default="gemini-3-flash-preview",
                        help=f"Model name. Available: {model_list}")
    parser.add_argument("--image-condition", type=str, default="",
                        help="Optional prior knowledge (e.g. 'in the US').")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress verbose output.")
    parser.add_argument("--output-json", type=str,
                        help="Write result to JSON file.")
    # Cost controls
    parser.add_argument("--max-image-dim", type=int, default=0,
                        help="Resize longest side to this value before upload "
                             "(0 = no resize; default). Downsizing saves a "
                             "negligible amount of money (image tokens are "
                             "1-3%% of total cost) but hurts readability of "
                             "storefront text and small signs. Pass e.g. "
                             "--max-image-dim 2048 only if you have a tight "
                             "cost ceiling on a large batch.")
    parser.add_argument("--no-step2-image", action="store_true",
                        help="Skip sending the image in the Step 2 call. Saves "
                             "~50%% of image-token cost; slight accuracy risk.")
    parser.add_argument("--no-send-raw-clue-json",
                        dest="send_raw_clue_json",
                        action="store_false", default=True,
                        help="Do NOT forward the raw Stage-1 clue JSON to "
                             "Step 2 (default: forward it).")
    parser.add_argument("--openai-image-detail",
                        choices=["auto", "low", "high"], default="auto",
                        help="OpenAI image 'detail' param. 'low'=85 tokens "
                             "regardless of size (cheapest, lossy).")
    # Pipeline routing
    parser.add_argument("--final-adjustment", dest="enable_final_adjustment",
                        action="store_true", default=False,
                        help="Enable the Step 3 final-adjustment pass "
                             "(opt-in; triggered when Step 2 reports "
                             "street/POI-level precision).")
    parser.add_argument("--final-adjustment-level",
                        choices=["neighborhood", "street", "building/POI"],
                        default="street",
                        help="Precision threshold that triggers final "
                             "adjustment (default: street).")
    # Reasoning-trace capture
    parser.add_argument("--save-trace", type=str, nargs="?",
                        const="<auto>", default=None,
                        help="Save the full reasoning trace as JSON. "
                             "Pass without a value to auto-name "
                             "<image_basename>.trace.json next to "
                             "--output-json (or in cwd if absent), "
                             "or pass an explicit path.")
    parser.add_argument("--save-trace-raw", action="store_true", default=False,
                        help="When saving the trace, also include raw LLM "
                             "output text for each call (verbose).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        print(f"Error: Image not found: {image_path}")
        sys.exit(1)

    save_trace = args.save_trace is not None
    save_trace_raw = args.save_trace_raw
    if save_trace_raw and not save_trace:
        save_trace = True
        args.save_trace = "<auto>"

    config = GeoConfig(
        model=args.model,
        image_condition=args.image_condition,
        verbose=not args.quiet,
        max_image_dim=args.max_image_dim,
        image_in_step2=not args.no_step2_image,
        send_raw_clue_json=args.send_raw_clue_json,
        openai_image_detail=args.openai_image_detail,
        enable_final_adjustment=args.enable_final_adjustment,
        final_adjustment_min_level=args.final_adjustment_level,
        save_trace=save_trace,
        save_trace_raw=save_trace_raw,
    )

    geo = SimpleGeo(config)
    result = geo.geolocate(str(image_path))

    if args.output_json:
        out = {
            "latitude": result.latitude,
            "longitude": result.longitude,
            "description": result.description,
            "precision_level": result.precision_level,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "hypothesis_chain": result.hypothesis_chain,
        }
        Path(args.output_json).write_text(json.dumps(out, indent=2))
        print(f"\nResult saved to: {args.output_json}")

    if save_trace and result.trace:
        if args.save_trace == "<auto>":
            base = image_path.stem
            if args.output_json:
                trace_path = Path(args.output_json).with_name(
                    f"{base}.trace.json"
                )
            else:
                trace_path = Path.cwd() / f"{base}.trace.json"
        else:
            trace_path = Path(args.save_trace)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps(result.trace, indent=2, default=str))
        print(f"Reasoning trace saved to: {trace_path}")


if __name__ == "__main__":
    main()
