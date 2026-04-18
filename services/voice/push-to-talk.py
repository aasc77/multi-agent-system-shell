#!/usr/bin/env python3
"""Push-to-Talk Voice Command Daemon.

Hold Cmd+Shift+V to record, release to send. Records 16kHz mono WAV,
hands it to the configured voice-understanding provider (Voxtral /
Whisper+LLM / Phi-4 Multimodal / null stub), and forwards the resulting
text to hassio via the MAS NATS bus.

The backend is selected via `providers.voice.backend` in the MAS shell
repo's config.yaml (issue #16). Swapping models is a one-line config
edit — no changes here.

Usage: python push-to-talk.py
Stop:  Ctrl+C

Environment overrides:
    MAS_SHELL_REPO   Path to the shell repo (defaults to ~/Repositories/
                     multi-agent-system-shell). Needed so the provider
                     adapter module can be imported from this script,
                     which lives outside the repo. Tracking pip-package
                     vs. sys.path cleanup under issue #49.
"""

import io
import os
import subprocess
import sys
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import yaml
from pynput import keyboard

# ---------------------------------------------------------------------------
# Cross-repo import of the voice-provider adapter (issue #16 / #49)
# ---------------------------------------------------------------------------

MAS_SHELL_REPO = Path(os.environ.get(
    "MAS_SHELL_REPO",
    str(Path.home() / "Repositories" / "multi-agent-system-shell"),
))

if str(MAS_SHELL_REPO) not in sys.path:
    sys.path.insert(0, str(MAS_SHELL_REPO))

from orchestrator.providers.voice import (  # noqa: E402
    VoiceProviderError,
    get_voice_provider,
)

# ---------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"

TARGET_AGENT = "hassio"

# State
recording = False
audio_frames = []
stream = None
keys_held = set()
lock = threading.Lock()

# Provider instance (built once at startup, reused for every utterance).
voice_provider = None


def play_sound(name):
    """Play a macOS system sound."""
    path = f"/System/Library/Sounds/{name}.aiff"
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def send_to_hassio(text):
    """Send voice command to hassio via NATS CLI."""
    message = f"VOICE_COMMAND: {text}"
    try:
        result = subprocess.run(
            ["nats", "pub", f"agents.{TARGET_AGENT}.inbox",
             f'{{"from":"hub","message":"{message}"}}'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(f"  -> Sent to {TARGET_AGENT}")
        else:
            print(f"  -> NATS send failed: {result.stderr}")
    except FileNotFoundError:
        print("  -> nats CLI not found, printing command only")
        print(f"  -> Would send: {message}")
    except Exception as e:
        print(f"  -> Send error: {e}")


def audio_callback(indata, frames, time_info, status):
    """Callback for audio stream - accumulate frames."""
    if status:
        print(f"  Audio status: {status}", file=sys.stderr)
    audio_frames.append(indata.copy())


def start_recording():
    """Start recording audio from microphone."""
    global recording, audio_frames, stream
    with lock:
        if recording:
            return
        recording = True
        audio_frames = []

    play_sound("Tink")
    print("\n[REC] Recording... (release to send)")

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        callback=audio_callback,
    )
    stream.start()


def stop_recording():
    """Stop recording and process the audio."""
    global recording, stream
    with lock:
        if not recording:
            return
        recording = False

    if stream:
        stream.stop()
        stream.close()
        stream = None

    if not audio_frames:
        print("[---] No audio captured")
        return

    print("[...] Processing...")

    # Build WAV in memory
    audio_data = np.concatenate(audio_frames, axis=0)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_data.tobytes())
    wav_bytes = wav_buffer.getvalue()

    duration = len(audio_data) / SAMPLE_RATE
    print(f"  Recorded {duration:.1f}s of audio")

    # Hand off to the configured voice-understanding provider.
    try:
        result = voice_provider.understand(wav_bytes)
    except VoiceProviderError as e:
        print(f"  -> Voice provider error: {e}")
        return
    except Exception as e:
        print(f"  -> Unexpected error: {e}")
        return

    text = result.text
    if not text:
        print("  [empty transcription]")
        return

    print(f"  Transcription: \"{text}\"  ({result.latency_ms}ms)")
    if result.tool_call:
        print(f"  Tool call:     {result.tool_call}")

    send_to_hassio(text)
    play_sound("Glass")


# Track Cmd+Shift+V combo
COMBO = {keyboard.Key.cmd, keyboard.Key.shift}
V_KEY = keyboard.KeyCode.from_char("v")


def on_press(key):
    """Track key presses for the hotkey combo."""
    # Normalize
    if key in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r):
        keys_held.add(keyboard.Key.cmd)
    elif key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
        keys_held.add(keyboard.Key.shift)
    elif key == V_KEY or (hasattr(key, "char") and key.char == "v"):
        keys_held.add(V_KEY)

    # Check if all three keys are held
    if COMBO.issubset(keys_held) and V_KEY in keys_held:
        start_recording()


def on_release(key):
    """Track key releases - stop recording when combo broken."""
    if key in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r):
        keys_held.discard(keyboard.Key.cmd)
    elif key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
        keys_held.discard(keyboard.Key.shift)
    elif key == V_KEY or (hasattr(key, "char") and key.char == "v"):
        keys_held.discard(V_KEY)

    # If we were recording and combo is broken, stop
    if recording and not (COMBO.issubset(keys_held) and V_KEY in keys_held):
        threading.Thread(target=stop_recording, daemon=True).start()


def _load_voice_cfg() -> dict:
    """Read providers.voice out of the shell repo's config.yaml."""
    config_path = MAS_SHELL_REPO / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Shell repo config not found at {config_path}. "
            f"Set MAS_SHELL_REPO to override."
        )
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    voice_cfg = (data.get("providers") or {}).get("voice")
    if not voice_cfg:
        raise KeyError(
            f"providers.voice is missing from {config_path}. "
            f"See README 'Voice provider' section."
        )
    return voice_cfg


def main():
    global voice_provider

    print("=" * 50)
    print("  Push-to-Talk Voice Command Daemon")
    print("  Hold Cmd+Shift+V to record")
    print("  Release to transcribe & send")
    print("  Ctrl+C to quit")
    print("=" * 50)

    voice_cfg = _load_voice_cfg()
    backend = voice_cfg.get("backend", "?")
    voice_provider = get_voice_provider(voice_cfg)

    print(f"\n  Voice provider: {backend}")
    print(f"  Shell repo:     {MAS_SHELL_REPO}")
    print(f"  Target:         {TARGET_AGENT}")
    print("\nListening for hotkey...\n")

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
