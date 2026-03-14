#!/usr/bin/env python3
"""
Vision Model Benchmark: Claude API vs UI-TARS (local DGX)

Compares response time and output quality for screenshot description tasks.

Usage:
    cd multi-agent-system-shell/scripts
    python3 benchmark-vision.py /path/to/screenshot.png
    python3 benchmark-vision.py /path/to/screenshot.png --rounds 5
    python3 benchmark-vision.py /path/to/screenshot.png --uitars-only
    python3 benchmark-vision.py /path/to/screenshot.png --claude-only

Requires:
    - ANTHROPIC_API_KEY env var (or set via scripts/set-anthropic-key.sh)
    - UI-TARS running on DGX at UITARS_URL (default: http://192.168.1.51:8000)
"""

import argparse
import base64
import json
import os
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

UITARS_URL = os.environ.get("UITARS_URL", "http://192.168.1.51:8000")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

PROMPT = "Describe what application is open and what the user is doing."


def load_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def query_uitars(b64_image: str) -> dict:
    payload = json.dumps({
        "model": "ByteDance-Seed/UI-TARS-1.5-7B",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}},
                {"type": "text", "text": PROMPT},
            ],
        }],
        "max_tokens": 512,
        "temperature": 0.0,
    }).encode()

    req = Request(
        f"{UITARS_URL}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    start = time.perf_counter()
    with urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    elapsed = time.perf_counter() - start

    content = body["choices"][0]["message"]["content"]
    tokens = body.get("usage", {})
    return {
        "model": "UI-TARS-1.5-7B (local DGX)",
        "elapsed_s": round(elapsed, 3),
        "content": content,
        "prompt_tokens": tokens.get("prompt_tokens"),
        "completion_tokens": tokens.get("completion_tokens"),
    }


def query_claude(b64_image: str) -> dict:
    if not ANTHROPIC_API_KEY:
        return {"model": CLAUDE_MODEL, "error": "ANTHROPIC_API_KEY not set"}

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 512,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64_image,
                    },
                },
                {"type": "text", "text": PROMPT},
            ],
        }],
    }).encode()

    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )

    start = time.perf_counter()
    with urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    elapsed = time.perf_counter() - start

    content = body["content"][0]["text"]
    usage = body.get("usage", {})
    return {
        "model": f"{CLAUDE_MODEL} (API)",
        "elapsed_s": round(elapsed, 3),
        "content": content,
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
    }


def print_result(result: dict, round_num: int = None):
    prefix = f"  Round {round_num}" if round_num else " "
    if "error" in result:
        print(f"{prefix} {result['model']}: SKIPPED ({result['error']})")
        return
    print(f"{prefix} {result['model']}")
    print(f"    Time:    {result['elapsed_s']}s")
    print(f"    Tokens:  {result['prompt_tokens']} in / {result['completion_tokens']} out")
    print(f"    Output:  {result['content'][:200]}{'...' if len(result['content']) > 200 else ''}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Benchmark vision models")
    parser.add_argument("image", help="Path to screenshot PNG")
    parser.add_argument("--rounds", type=int, default=3, help="Number of rounds (default: 3)")
    parser.add_argument("--uitars-only", action="store_true", help="Only benchmark UI-TARS")
    parser.add_argument("--claude-only", action="store_true", help="Only benchmark Claude")
    args = parser.parse_args()

    if not os.path.exists(args.image):
        print(f"Error: {args.image} not found")
        sys.exit(1)

    print(f"Loading image: {args.image}")
    b64 = load_image_b64(args.image)
    print(f"Image size: {len(b64) * 3 // 4 // 1024} KB (base64: {len(b64) // 1024} KB)")
    print(f"Rounds: {args.rounds}")
    print(f"Prompt: \"{PROMPT}\"")
    print("=" * 60)

    run_uitars = not args.claude_only
    run_claude = not args.uitars_only

    uitars_times = []
    claude_times = []

    for i in range(1, args.rounds + 1):
        print(f"\n--- Round {i}/{args.rounds} ---")

        if run_uitars:
            try:
                r = query_uitars(b64)
                print_result(r, i)
                if "error" not in r:
                    uitars_times.append(r["elapsed_s"])
            except (URLError, Exception) as e:
                print(f"  UI-TARS error: {e}\n")

        if run_claude:
            try:
                r = query_claude(b64)
                print_result(r, i)
                if "error" not in r:
                    claude_times.append(r["elapsed_s"])
            except (URLError, Exception) as e:
                print(f"  Claude error: {e}\n")

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    if uitars_times:
        avg = sum(uitars_times) / len(uitars_times)
        mn, mx = min(uitars_times), max(uitars_times)
        print(f"  UI-TARS-1.5-7B (local):  avg {avg:.3f}s  min {mn:.3f}s  max {mx:.3f}s  ({len(uitars_times)} rounds)")

    if claude_times:
        avg = sum(claude_times) / len(claude_times)
        mn, mx = min(claude_times), max(claude_times)
        print(f"  {CLAUDE_MODEL} (API):  avg {avg:.3f}s  min {mn:.3f}s  max {mx:.3f}s  ({len(claude_times)} rounds)")

    if uitars_times and claude_times:
        uitars_avg = sum(uitars_times) / len(uitars_times)
        claude_avg = sum(claude_times) / len(claude_times)
        faster = "UI-TARS" if uitars_avg < claude_avg else "Claude"
        ratio = max(uitars_avg, claude_avg) / min(uitars_avg, claude_avg)
        print(f"\n  {faster} is {ratio:.1f}x faster on average")

    print()


if __name__ == "__main__":
    main()
