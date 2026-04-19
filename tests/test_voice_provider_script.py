"""Tests for scripts/voice-provider.py — voice-provider CLI helper.

Focus on the line-level `_rewrite_voice_backend` logic because that is the
only non-trivial piece: it must preserve comments, blank lines, and inline
documentation when flipping `providers.voice.backend` (a prior yaml-dump
implementation silently stripped all of that).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "voice-provider.py"


def _load_script():
    """Import scripts/voice-provider.py as a module despite the hyphen in its name."""
    spec = importlib.util.spec_from_file_location("voice_provider_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # The script inserts REPO_ROOT into sys.path at import time; make sure
    # that plays nicely across tests.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return _load_script()


CANONICAL = """\
providers:
  stt:
    backend: whisper
    url: http://192.168.1.51:5112

  # Voice understanding block -- comments must survive the rewrite.
  voice:
    backend: whisper_llm          # voxtral | whisper_llm | phi4_multimodal | null
    voxtral:
      url: http://192.168.1.41:5100
    whisper_llm:
      stt_url: http://192.168.1.51:5112
      llm_url: http://192.168.1.51:11434
      model: qwen3:8b
"""


class TestRewriteVoiceBackend:
    def test_switches_backend_and_preserves_inline_comment(self, script):
        new_raw, ok = script._rewrite_voice_backend(CANONICAL, "voxtral")
        assert ok is True
        assert "backend: voxtral" in new_raw
        assert "whisper_llm | phi4_multimodal" in new_raw  # inline comment kept
        assert "whisper_llm:" in new_raw                   # sub-section kept

    def test_preserves_block_comment_above_voice(self, script):
        new_raw, _ = script._rewrite_voice_backend(CANONICAL, "phi4_multimodal")
        assert "# Voice understanding block" in new_raw

    def test_preserves_blank_lines(self, script):
        # Blank line between stt: and the voice block must survive.
        new_raw, _ = script._rewrite_voice_backend(CANONICAL, "null")
        lines = new_raw.splitlines()
        # There's exactly one blank line between 'url: http://...' (stt) and
        # the '# Voice understanding block' comment.
        stt_url_idx = next(i for i, l in enumerate(lines) if "192.168.1.51:5112" in l and "stt" not in l.lower()[:10] and not l.strip().startswith("stt_url"))
        assert lines[stt_url_idx + 1] == ""

    def test_only_rewrites_the_voice_backend_not_stt_backend(self, script):
        new_raw, _ = script._rewrite_voice_backend(CANONICAL, "voxtral")
        assert "backend: whisper\n" in new_raw  # stt backend untouched
        assert "backend: voxtral" in new_raw    # voice backend switched

    def test_reports_not_replaced_when_voice_section_missing(self, script):
        raw = "nats:\n  url: nats://x\n"
        new_raw, ok = script._rewrite_voice_backend(raw, "voxtral")
        assert ok is False
        assert new_raw == raw

    def test_reports_not_replaced_when_voice_has_no_backend_line(self, script):
        raw = "providers:\n  voice:\n    voxtral:\n      url: http://x\n"
        new_raw, ok = script._rewrite_voice_backend(raw, "voxtral")
        assert ok is False
        assert new_raw == raw

    def test_ignores_backend_keys_outside_voice_block(self, script):
        # A later top-level section with its own backend: must not be touched.
        raw = (
            "providers:\n"
            "  voice:\n"
            "    backend: whisper_llm\n"
            "    voxtral:\n"
            "      url: http://x\n"
            "other:\n"
            "  backend: do-not-touch\n"
        )
        new_raw, ok = script._rewrite_voice_backend(raw, "voxtral")
        assert ok is True
        assert "backend: voxtral" in new_raw
        assert "backend: do-not-touch" in new_raw
