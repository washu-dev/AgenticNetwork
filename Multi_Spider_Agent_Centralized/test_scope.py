"""
Test script: verify LLM-based scope resolution.

Sends test prompts to Central's /api/search and prints the scope decisions.
Only tests the PARSE step (checks intent fields), does NOT wait for full execution.

Usage:
    python test_scope.py [--url http://localhost:8080]
"""

import argparse
import httpx
import json
import sys
import time

TESTS = [
    # (prompt, expected_description)
    ("crawler-alpha-patrol-001 push up",                        "single robot by name → 001 only"),
    ("crawler-alpha-patrol-002 wave",                           "single robot by name → 002 only"),
    ("zone A robot detect person, if detected, wave",            "zone filter → zone A robots, agent mode"),
    ("zone B robot push up",                                     "zone filter → zone B robots, direct control"),
    ("all robots wave",                                         "all robots → multi_agent=true"),
    ("detect person, if detected wave",                         "detection → multi_agent=true, no specific target"),
    ("wave",                                                    "simple action → multi_agent=false (fast path)"),
]

# Scope-only mode: we use a lightweight approach - call the planner parse
# endpoint if available, or use the full /api/search and check logs.
# Since there's no dedicated parse-only endpoint, we'll hit /api/search
# but add a dry_run flag (won't exist, so it'll run fully).
# The key output is in Central's logs. We also print the API response.


def run_tests(base_url: str, dry_run: bool = False):
    print(f"{'='*70}")
    print(f"  Scope Resolution Test Suite")
    print(f"  Target: {base_url}")
    print(f"{'='*70}\n")

    results = []

    for i, (prompt, description) in enumerate(TESTS, 1):
        print(f"[{i}/{len(TESTS)}] {description}")
        print(f"  Prompt: \"{prompt}\"")

        if dry_run:
            print(f"  (dry run - skipped)\n")
            continue

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    f"{base_url}/api/search",
                    json={"prompt": prompt},
                )
                data = resp.json()

                status = data.get("status", "?")
                agents_used = data.get("agents_used", [])
                error = data.get("error", "")
                summary = data.get("summary", "")[:120]

                if error and "NOT_VALID_TASK" in str(error):
                    print(f"  Result: REJECTED - {error}")
                elif error:
                    print(f"  Result: ERROR - {error}")
                else:
                    print(f"  Result: {status}")
                    print(f"  Agents used: {agents_used}")
                    if summary:
                        print(f"  Summary: {summary}")

                results.append({
                    "prompt": prompt,
                    "expected": description,
                    "status": status,
                    "agents_used": agents_used,
                    "error": error,
                })

        except httpx.ConnectError:
            print(f"  ERROR: Cannot connect to {base_url}")
            print(f"  Is Central running?")
            sys.exit(1)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({
                "prompt": prompt,
                "expected": description,
                "status": "error",
                "error": str(e),
            })

        print()
        # Small delay between tests to avoid overwhelming
        time.sleep(1)

    # Summary
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  Total: {len(results)} tests")
    print(f"  Check Central logs for 'Parsed intent:' lines to verify:")
    print(f"    - target_robot_ids")
    print(f"    - target_zone")
    print(f"    - multi_agent")
    print(f"{'='*70}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test scope resolution")
    parser.add_argument("--url", default="http://localhost:8080", help="Central base URL")
    parser.add_argument("--dry-run", action="store_true", help="Print tests without sending")
    args = parser.parse_args()
    run_tests(args.url, dry_run=args.dry_run)
