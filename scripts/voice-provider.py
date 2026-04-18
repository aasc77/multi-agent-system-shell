#!/usr/bin/env python3
"""CLI helper for the voice-understanding provider (issue #16).

Commands:
    show            Print the resolved providers.voice subtree and which
                    concrete adapter class would be instantiated.
    switch BACKEND  Rewrite config.yaml in place to set
                    providers.voice.backend = BACKEND. Other sub-sections
                    (voxtral/whisper_llm/phi4_multimodal) are left intact
                    so the switch is truly one line.
    test WAV_PATH   Feed a WAV file to the currently-configured provider
                    and print its VoiceResponse. Useful for smoke-testing
                    a backend swap end-to-end without the full push-to-talk
                    hotkey loop.

Examples:
    python3 scripts/voice-provider.py show
    python3 scripts/voice-provider.py switch voxtral
    python3 scripts/voice-provider.py test /tmp/sample.wav
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the orchestrator package importable when running from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from orchestrator.providers.voice import (  # noqa: E402
    SUPPORTED_BACKENDS,
    VoiceProviderError,
    get_voice_provider,
)

CONFIG_PATH = REPO_ROOT / "config.yaml"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"error: {CONFIG_PATH} not found")
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _voice_subtree(data: dict) -> dict:
    voice = (data.get("providers") or {}).get("voice")
    if not voice:
        sys.exit("error: providers.voice section missing from config.yaml")
    return voice


def cmd_show() -> None:
    data = _load_config()
    voice = _voice_subtree(data)
    print(yaml.safe_dump({"providers": {"voice": voice}}, sort_keys=False).rstrip())
    try:
        provider = get_voice_provider(voice)
    except VoiceProviderError as e:
        print(f"\nstatus: MISCONFIGURED -- {e}", file=sys.stderr)
        sys.exit(1)
    print(f"\nresolved adapter: {type(provider).__name__}")


def cmd_switch(backend: str) -> None:
    if backend not in SUPPORTED_BACKENDS:
        sys.exit(
            f"error: unknown backend {backend!r}. "
            f"Supported: {', '.join(SUPPORTED_BACKENDS)}"
        )
    data = _load_config()
    voice = dict(_voice_subtree(data))
    old = voice.get("backend")
    voice["backend"] = backend

    # Validate the result before touching config.yaml, so we fail loud on e.g.
    # missing whisper_llm.model instead of shipping a broken config.
    try:
        get_voice_provider(voice)
    except VoiceProviderError as e:
        sys.exit(
            f"error: {backend} is not fully configured; refusing to switch.\n"
            f"  reason: {e}"
        )

    # Line-level rewrite so comments, blank lines, and key ordering from the
    # hand-authored config.yaml survive the edit. A yaml round-trip would
    # strip all of that, which is how we lost the comments on a prior run.
    raw = CONFIG_PATH.read_text()
    new_raw, replaced = _rewrite_voice_backend(raw, backend)
    if not replaced:
        sys.exit(
            "error: could not locate providers.voice.backend line in config.yaml; "
            "refusing to rewrite. Edit it by hand."
        )
    CONFIG_PATH.write_text(new_raw)
    print(f"providers.voice.backend: {old} -> {backend}")


def _rewrite_voice_backend(raw: str, backend: str) -> tuple[str, bool]:
    """Rewrite the first `backend:` line that sits directly under `voice:`.

    Returns the (new_text, replaced?) tuple so callers can refuse to write
    if the anchor wasn't found — safer than silently no-oping.
    """
    lines = raw.splitlines(keepends=True)
    in_voice = False
    voice_indent = -1
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not in_voice:
            if stripped.startswith("voice:"):
                in_voice = True
                voice_indent = indent
            continue
        # Left the voice block when we see an equal- or less-indented key.
        if stripped and not stripped.startswith("#") and indent <= voice_indent:
            break
        if stripped.startswith("backend:") and indent == voice_indent + 2:
            prefix = line[:indent]
            # Preserve any inline comment after the value so the list of
            # supported backends stays readable.
            comment = ""
            if "#" in line:
                comment = "  " + line[line.index("#"):].rstrip("\n")
            lines[i] = f"{prefix}backend: {backend}{comment}\n"
            return "".join(lines), True
    return raw, False


def cmd_test(wav_path: str) -> None:
    wav_file = Path(wav_path)
    if not wav_file.exists():
        sys.exit(f"error: {wav_file} not found")

    voice = _voice_subtree(_load_config())
    provider = get_voice_provider(voice)
    result = provider.understand(wav_file.read_bytes())

    print(json.dumps({
        "backend": voice.get("backend"),
        "adapter": type(provider).__name__,
        "text": result.text,
        "tool_call": result.tool_call,
        "latency_ms": result.latency_ms,
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="voice-provider")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="Print resolved providers.voice config")

    switch = sub.add_parser("switch", help="Set providers.voice.backend in config.yaml")
    switch.add_argument("backend", choices=SUPPORTED_BACKENDS)

    test = sub.add_parser("test", help="Feed a WAV file to the configured provider")
    test.add_argument("wav_path")

    args = parser.parse_args()
    if args.cmd == "show":
        cmd_show()
    elif args.cmd == "switch":
        cmd_switch(args.backend)
    elif args.cmd == "test":
        cmd_test(args.wav_path)


if __name__ == "__main__":
    main()
