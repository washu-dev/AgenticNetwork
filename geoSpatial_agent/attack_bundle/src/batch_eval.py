#!/usr/bin/env python3
"""Batch evaluation runner for Im2GPS / Im2GPS3k (or any image set with ground truth).

Usage:
    python src/batch_eval.py --dataset im2gps
    python src/batch_eval.py --dataset im2gps3k
    python src/batch_eval.py --dataset im2gps3k --model gpt-5.4-mini --batch-size 10
    python src/batch_eval.py --data-dir path/to/imgs --meta path/to/meta.csv
    python src/batch_eval.py --resume results/im2gps/eval_gemini-3-flash-preview_20260415_1200.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(_SRC))
_PROJECT_ROOT = _SRC.parent

from config import GeoConfig, GEMINI_MODELS, OPENAI_MODELS
from pipeline_simple import SimpleGeo
from geopy.distance import geodesic


ALL_MODELS = {**GEMINI_MODELS, **OPENAI_MODELS}

DISTANCE_THRESHOLDS_KM = [1, 25, 200, 750, 2500]

GEOSCORE_SCALE = 5000
GEOSCORE_DECAY = 1492.7


def geoscore(distance_km: float) -> float:
    """GeoScore: 5000 * exp(-d / 1492.7). Perfect=5000, antipodal≈0."""
    if distance_km == float("inf"):
        return 0.0
    return GEOSCORE_SCALE * math.exp(-distance_km / GEOSCORE_DECAY)

DATASET_PRESETS: dict[str, dict[str, str]] = {
    "im2gps": {
        "data_dir": str(_PROJECT_ROOT / "data" / "im2gps"),
        "meta": str(_PROJECT_ROOT / "data" / "im2gps_meta.csv"),
        "results_subdir": "img2gps",
    },
    "im2gps3k": {
        "data_dir": str(_PROJECT_ROOT / "data" / "im2gps3k" / "im2gps3ktest"),
        "meta": str(_PROJECT_ROOT / "data" / "im2gps3k_meta.csv"),
        "results_subdir": "im2gps3k",
    },
    "doxbench": {
        "data_dir": str(_PROJECT_ROOT / "data" / "doxbench"),
        "meta": str(_PROJECT_ROOT / "data" / "doxbench_meta.csv"),
        "results_subdir": "doxbench",
    },
}


@dataclass
class EvalRow:
    filename: str
    gt_lat: float
    gt_lon: float
    pred_lat: float
    pred_lon: float
    distance_km: float
    confidence: float
    description: str
    precision_level: str
    elapsed_s: float
    error: str = ""


def load_meta(csv_path: str) -> dict[str, tuple[float, float]]:
    """Load ground-truth GPS from metadata CSV."""
    meta: dict[str, tuple[float, float]] = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            meta[row["filename"]] = (
                float(row["latitude"]),
                float(row["longitude"]),
            )
    return meta


def process_one(
    image_path: str,
    filename: str,
    gt: tuple[float, float],
    config: GeoConfig,
    trace_dir: str | None = None,
) -> EvalRow:
    """Run geolocation on a single image and compute distance error.

    If ``trace_dir`` is provided AND the config has ``save_trace=True``,
    writes the per-image reasoning trace to ``<trace_dir>/<filename>.json``.
    """
    t0 = time.time()
    try:
        geo = SimpleGeo(config)
        result = geo.geolocate(image_path)
        elapsed = time.time() - t0

        pred = (result.latitude, result.longitude)
        if pred == (0.0, 0.0):
            dist = float("inf")
        else:
            dist = geodesic(gt, pred).km

        if trace_dir and config.save_trace and result.trace:
            try:
                os.makedirs(trace_dir, exist_ok=True)
                trace_payload = dict(result.trace)
                trace_payload.update({
                    "ground_truth": {"latitude": gt[0], "longitude": gt[1]},
                    "distance_km":
                        dist if dist != float("inf") else None,
                })
                trace_path = os.path.join(trace_dir, f"{filename}.json")
                with open(trace_path, "w") as tf:
                    json.dump(trace_payload, tf, indent=2, default=str)
            except Exception as trace_exc:
                # Trace write failure should never break the run.
                print(
                    f"  [trace] failed to write {filename}.json: {trace_exc}",
                    flush=True,
                )

        return EvalRow(
            filename=filename,
            gt_lat=gt[0],
            gt_lon=gt[1],
            pred_lat=result.latitude,
            pred_lon=result.longitude,
            distance_km=dist,
            confidence=result.confidence,
            description=result.description,
            precision_level=result.precision_level,
            elapsed_s=elapsed,
        )
    except Exception as exc:
        elapsed = time.time() - t0
        return EvalRow(
            filename=filename,
            gt_lat=gt[0],
            gt_lon=gt[1],
            pred_lat=0.0,
            pred_lon=0.0,
            distance_km=float("inf"),
            confidence=0.0,
            description="",
            precision_level="",
            elapsed_s=elapsed,
            error=str(exc),
        )


def compute_metrics(rows: list[EvalRow]) -> dict:
    """Compute standard geolocation accuracy metrics + GeoScore."""
    valid = [r for r in rows if r.error == ""]
    total = len(rows)
    n_valid = len(valid)
    n_error = total - n_valid
    distances = [r.distance_km for r in valid if r.distance_km != float("inf")]
    scores = [geoscore(r.distance_km) for r in valid]

    metrics: dict = {
        "total": total,
        "completed": n_valid,
        "errors": n_error,
    }

    if distances:
        import statistics
        metrics["median_km"] = round(statistics.median(distances), 1)
        metrics["mean_km"] = round(statistics.mean(distances), 1)

    if scores:
        import statistics
        metrics["mean_geoscore"] = round(statistics.mean(scores), 1)
        metrics["median_geoscore"] = round(statistics.median(scores), 1)

    for thresh in DISTANCE_THRESHOLDS_KM:
        count = sum(1 for d in distances if d <= thresh)
        pct = count / n_valid * 100 if n_valid else 0
        metrics[f"acc@{thresh}km"] = round(pct, 2)

    return metrics


def print_metrics(metrics: dict) -> None:
    print("\n" + "=" * 60)
    print("EVALUATION METRICS")
    print("=" * 60)
    print(f"  Total images:   {metrics['total']}")
    print(f"  Completed:      {metrics['completed']}")
    print(f"  Errors:         {metrics['errors']}")
    if "median_km" in metrics:
        print(f"  Median error:   {metrics['median_km']} km")
        print(f"  Mean error:     {metrics['mean_km']} km")
    if "mean_geoscore" in metrics:
        print(f"  Mean GeoScore:  {metrics['mean_geoscore']}  (max 5000)")
        print(f"  Median GeoScore:{metrics['median_geoscore']}")
    print()
    for thresh in DISTANCE_THRESHOLDS_KM:
        key = f"acc@{thresh}km"
        if key in metrics:
            print(f"  {key:>12s} = {metrics[key]:.2f}%")
    print("=" * 60)


def run_batch(
    data_dir: str,
    meta: dict[str, tuple[float, float]],
    config: GeoConfig,
    batch_size: int,
    output_path: str,
    limit: int | None = None,
    already_done: set[str] | None = None,
    trace_dir: str | None = None,
) -> list[EvalRow]:
    """Run geolocation on all images with concurrent batching."""
    already_done = already_done or set()

    items = []
    for fn, gt in sorted(meta.items()):
        if fn in already_done:
            continue
        img_path = os.path.join(data_dir, fn)
        if not os.path.exists(img_path):
            continue
        items.append((img_path, fn, gt))

    if limit is not None:
        items = items[:limit]

    total = len(items)
    print(f"\nBatch evaluation: {total} images, batch_size={batch_size}")
    print(f"Model: {config.model}  Provider: {config.provider}")
    print(f"Output: {output_path}\n")

    results: list[EvalRow] = []
    completed = len(already_done)
    total_all = completed + total

    out_file = open(output_path, "a")

    try:
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = {}
            for img_path, fn, gt in items:
                fut = executor.submit(
                    process_one, img_path, fn, gt, config, trace_dir,
                )
                futures[fut] = fn

            for fut in as_completed(futures):
                fn = futures[fut]
                row = fut.result()
                results.append(row)
                completed += 1

                gs = geoscore(row.distance_km)
                line = json.dumps({
                    "filename": row.filename,
                    "gt_lat": row.gt_lat,
                    "gt_lon": row.gt_lon,
                    "pred_lat": row.pred_lat,
                    "pred_lon": row.pred_lon,
                    "distance_km": row.distance_km if row.distance_km != float("inf") else -1,
                    "geoscore": round(gs, 1),
                    "confidence": row.confidence,
                    "description": row.description,
                    "precision_level": row.precision_level,
                    "elapsed_s": round(row.elapsed_s, 1),
                    "error": row.error,
                })
                out_file.write(line + "\n")
                out_file.flush()

                status = "OK" if not row.error else f"ERR: {row.error[:60]}"
                dist_str = f"{row.distance_km:.1f}km" if row.distance_km != float("inf") else "INF"
                print(
                    f"  [{completed}/{total_all}] {fn}: "
                    f"dist={dist_str} conf={row.confidence:.2f} "
                    f"({row.elapsed_s:.1f}s) {status}",
                    flush=True,
                )
    finally:
        out_file.close()

    return results


def load_existing_results(path: str) -> tuple[list[EvalRow], set[str]]:
    """Load previously completed results from JSONL for resume."""
    rows = []
    done = set()
    if not os.path.exists(path):
        return rows, done
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            dist = d["distance_km"]
            if dist == -1:
                dist = float("inf")
            row = EvalRow(
                filename=d["filename"],
                gt_lat=d["gt_lat"],
                gt_lon=d["gt_lon"],
                pred_lat=d["pred_lat"],
                pred_lon=d["pred_lon"],
                distance_km=dist,
                confidence=d["confidence"],
                description=d["description"],
                precision_level=d["precision_level"],
                elapsed_s=d["elapsed_s"],
                error=d.get("error", ""),
            )
            rows.append(row)
            done.add(d["filename"])
    return rows, done


def parse_args() -> argparse.Namespace:
    model_list = ", ".join(ALL_MODELS.keys())
    dataset_list = ", ".join(DATASET_PRESETS.keys())
    parser = argparse.ArgumentParser(
        description="Batch geolocation evaluation on Im2GPS, Im2GPS3k, or a custom dataset.",
    )
    parser.add_argument("--dataset", type=str, default=None,
                        choices=list(DATASET_PRESETS.keys()),
                        help=f"Dataset preset (auto-sets --data-dir and --meta). Available: {dataset_list}")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="Directory containing images (overrides --dataset).")
    parser.add_argument("--meta", type=str, default=None,
                        help="CSV with columns: filename, latitude, longitude (overrides --dataset).")
    parser.add_argument("--model", type=str, default="gemini-3-flash-preview",
                        help=f"Model. Available: {model_list}")
    parser.add_argument("--batch-size", type=int, default=300,
                        help="Concurrent workers (default: 300).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process first N images (for testing).")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSONL path (auto-generated if not set).")
    parser.add_argument("--resume", type=str, default=None,
                        help="Resume from existing JSONL output file.")
    parser.add_argument("--image-condition", type=str, default="",
                        help="Optional prior knowledge hint.")
    # Cost controls (forwarded into GeoConfig)
    parser.add_argument("--max-image-dim", type=int, default=0,
                        help="Resize longest side before upload "
                             "(0 = off; default). Image tokens are only "
                             "1-3%% of total cost, so resizing saves little "
                             "but hurts sign legibility. Pass e.g. 2048 "
                             "only if a very large batch needs cost control.")
    parser.add_argument("--no-step2-image", action="store_true",
                        help="Skip sending the image in Step 2. Big cost save, "
                             "small accuracy risk.")
    parser.add_argument("--no-send-raw-clue-json",
                        dest="send_raw_clue_json",
                        action="store_false", default=True,
                        help="Do NOT forward raw Stage-1 JSON to Step 2 "
                             "(default: forward it).")
    parser.add_argument("--final-adjustment", dest="enable_final_adjustment",
                        action="store_true", default=False,
                        help="Enable the Step 3 final-adjustment pass (opt-in).")
    parser.add_argument("--final-adjustment-level",
                        choices=["neighborhood", "street", "building/POI"],
                        default="street",
                        help="Precision threshold that triggers final "
                             "adjustment (default: street).")
    parser.add_argument("--openai-image-detail",
                        choices=["auto", "low", "high"], default="auto",
                        help="OpenAI image 'detail'. 'low' = 85 tokens flat.")
    # Optional forward-geocode sanity check (requires Google Maps API).
    parser.add_argument("--forward-geocode-check",
                        dest="enable_forward_geocode_check",
                        action="store_true", default=False,
                        help="Snap coords to the geocoded description when they "
                             "disagree (requires Google Maps API; off by default).")
    parser.add_argument("--alternatives-threshold", type=float, default=0.75,
                        help="Return up to 3 alternatives when the best "
                             "confidence is below this value (default: 0.75).")
    parser.add_argument("--maps-in-final-adjustment",
                        dest="enable_maps_in_final_adjustment",
                        action="store_true", default=False,
                        help="Inject Google Maps evidence (reverse-geocode + "
                             "nearby POIs) into the final adjustment prompt. "
                             "Requires Maps API. Off by default.")
    # Reasoning-trace capture
    parser.add_argument("--save-traces", dest="save_traces",
                        action="store_true", default=False,
                        help="Save per-image reasoning trace as JSON in "
                             "<results_dir>/traces/<filename>.json. "
                             "Off by default; zero cost when off.")
    parser.add_argument("--save-traces-raw", dest="save_traces_raw",
                        action="store_true", default=False,
                        help="When saving traces, also include each LLM "
                             "call's raw text output (verbose; useful for "
                             "debugging parse failures). Implies "
                             "--save-traces.")
    parser.add_argument("--trace-dir", type=str, default=None,
                        help="Override default trace directory "
                             "(<results_dir>/traces).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Resolve dataset preset; explicit --data-dir / --meta override preset values
    preset = DATASET_PRESETS.get(args.dataset) if args.dataset else None
    if preset:
        data_dir = args.data_dir or preset["data_dir"]
        meta_path = args.meta or preset["meta"]
        results_subdir = preset["results_subdir"]
    else:
        data_dir = args.data_dir or str(_PROJECT_ROOT / "data" / "im2gps3k" / "im2gps3ktest")
        meta_path = args.meta or str(_PROJECT_ROOT / "data" / "im2gps3k_meta.csv")
        results_subdir = "custom"

    if not os.path.isdir(data_dir):
        print(f"Error: data directory not found: {data_dir}")
        sys.exit(1)
    if not os.path.isfile(meta_path):
        print(f"Error: metadata CSV not found: {meta_path}")
        sys.exit(1)

    # Determine output path
    if args.resume:
        output_path = args.resume
    elif args.output:
        output_path = args.output
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        model_short = args.model.replace("/", "_")
        output_path = str(
            _PROJECT_ROOT / "results" / results_subdir
            / f"eval_{model_short}_{ts}.jsonl"
        )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Load existing results if resuming
    prev_rows, already_done = [], set()
    if args.resume or (args.output and os.path.exists(args.output)):
        prev_rows, already_done = load_existing_results(output_path)
        print(f"Resuming: {len(already_done)} images already done.")

    meta = load_meta(meta_path)
    print(f"Loaded metadata: {len(meta)} images with ground truth.")

    save_trace = args.save_traces or args.save_traces_raw
    save_trace_raw = args.save_traces_raw

    config = GeoConfig(
        model=args.model,
        image_condition=args.image_condition,
        verbose=False,
        max_image_dim=args.max_image_dim,
        image_in_step2=not args.no_step2_image,
        send_raw_clue_json=args.send_raw_clue_json,
        openai_image_detail=args.openai_image_detail,
        enable_final_adjustment=args.enable_final_adjustment,
        final_adjustment_min_level=args.final_adjustment_level,
        enable_forward_geocode_check=args.enable_forward_geocode_check,
        alternatives_threshold=args.alternatives_threshold,
        enable_maps_in_final_adjustment=args.enable_maps_in_final_adjustment,
        save_trace=save_trace,
        save_trace_raw=save_trace_raw,
    )

    trace_dir: str | None = None
    if save_trace:
        trace_dir = args.trace_dir or str(
            Path(output_path).parent / "traces"
        )
        os.makedirs(trace_dir, exist_ok=True)
        print(f"Reasoning traces will be saved to: {trace_dir}")

    new_rows = run_batch(
        data_dir=data_dir,
        meta=meta,
        config=config,
        batch_size=args.batch_size,
        output_path=output_path,
        limit=args.limit,
        already_done=already_done,
        trace_dir=trace_dir,
    )

    all_rows = prev_rows + new_rows
    metrics = compute_metrics(all_rows)
    print_metrics(metrics)

    # Save metrics summary
    metrics_path = output_path.replace(".jsonl", "_metrics.json")
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"\nResults: {output_path}")
    print(f"Metrics: {metrics_path}")


if __name__ == "__main__":
    main()
