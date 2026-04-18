#!/usr/bin/env python3
"""Session recap doodle — snapshot of today's git/gh activity as an ASCII box.

Usage:
    scripts/session-recap.py                 # today midnight → now
    scripts/session-recap.py --since 2d      # 2 days ago → now
    scripts/session-recap.py --since 2026-04-10  # explicit date

Zero dependencies beyond stdlib + git + gh. Run from anywhere — auto-detects repo root.
"""

from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import sys
from datetime import datetime, date, timedelta


MOODS = {
    0: ["quiet day. maybe something tomorrow.", "no PRs, no problems.", "rest mode engaged."],
    1: ["steady progress.", "one for the board.", "forward is forward."],
    2: ["two-PR tuesday energy.", "warming up.", "nice pace."],
    3: ["good iteration.", "the machine hums.", "shipping weather."],
    4: ["real momentum.", "flow state spotted.", "productive afternoon."],
    5: ["hub in the zone.", "five merges, zero regrets.", "reviewers earning their keep."],
    6: ["hub on fire.", "stand back.", "iterations go brrr."],
    7: ["disciplined chaos.", "scorecard: legendary.", "who needs sleep."],
}


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _parse_since(since: str | None) -> tuple[str, str]:
    """Return (iso_date, human_label) for the --since argument."""
    if since is None or since == "today":
        today = date.today()
        return today.isoformat(), today.isoformat()
    if since.endswith("d") and since[:-1].isdigit():
        days = int(since[:-1])
        d = date.today() - timedelta(days=days)
        return d.isoformat(), f"{since} ago"
    try:
        d = date.fromisoformat(since)
        return d.isoformat(), since
    except ValueError:
        print(f"session-recap: can't parse --since '{since}'", file=sys.stderr)
        sys.exit(2)


def _find_repo_root() -> str | None:
    root = _run(["git", "rev-parse", "--show-toplevel"])
    return root or None


def _count_commits(repo: str, since_iso: str) -> int:
    out = _run(["git", "-C", repo, "log", "--oneline", f"--since={since_iso} 00:00"])
    return len([l for l in out.splitlines() if l.strip()]) if out else 0


def _latest_commit(repo: str, since_iso: str) -> str:
    out = _run(["git", "-C", repo, "log", "-1", "--format=%h %s", f"--since={since_iso} 00:00"])
    return out or "(none)"


def _gh_count(cmd: list[str]) -> int:
    out = _run(cmd)
    if not out:
        return 0
    return len([l for l in out.splitlines() if l.strip()])


def _render_box(lines: list[tuple[str, str]], header: str, footer: str) -> str:
    # Widest content = max of header/footer and each "label: value" line
    inner_w = max(
        len(header),
        len(footer),
        max(len(f"{k}  {v}") for k, v in lines),
    )
    inner_w += 4  # padding

    top = f"┌─ {header} " + "─" * (inner_w - len(header) - 3) + "┐"
    bot = f"└─ {footer} " + "─" * (inner_w - len(footer) - 3) + "┘"

    body = []
    for label, value in lines:
        label_part = f"  {label}"
        value_part = f"{value}  "
        pad = inner_w - len(label_part) - len(value_part)
        body.append(f"│{label_part}{' ' * pad}{value_part}│")

    return "\n".join([top, *body, bot])


def main() -> int:
    ap = argparse.ArgumentParser(description="Session recap doodle")
    ap.add_argument("--since", help="today | Nd | YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    repo = _find_repo_root()
    if not repo:
        print("session-recap: not inside a git repository", file=sys.stderr)
        return 2

    since_iso, since_label = _parse_since(args.since)

    commits = _count_commits(repo, since_iso)
    latest = _latest_commit(repo, since_iso)

    gh_available = shutil.which("gh") is not None
    if gh_available:
        prs = _gh_count(
            ["gh", "pr", "list", "--state", "merged",
             "--search", f"merged:>={since_iso}",
             "--limit", "50", "--json", "number", "--jq", ".[].number"]
        )
        issues = _gh_count(
            ["gh", "issue", "list",
             "--search", f"created:>={since_iso}",
             "--limit", "50", "--state", "all", "--json", "number", "--jq", ".[].number"]
        )
    else:
        prs = -1
        issues = -1

    mood_key = min(commits, max(MOODS.keys()))
    vibe = random.choice(MOODS[mood_key])

    lines: list[tuple[str, str]] = [
        ("commits:", str(commits)),
    ]
    if prs >= 0:
        lines.append(("PRs merged:", str(prs)))
    if issues >= 0:
        lines.append(("issues filed:", str(issues)))
    lines.append(("latest:", latest[:40]))
    lines.append(("vibe:", vibe))

    box = _render_box(
        lines,
        header=f"session {since_label}",
        footer="manager out",
    )
    print(box)
    return 0


if __name__ == "__main__":
    sys.exit(main())
