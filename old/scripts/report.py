#!/usr/bin/env python3
"""Generate a self-test Markdown report from event logs.

Usage:
  python scripts/report.py --data-dir data --out reports/self-test.md
  python scripts/report.py --data-dir data --session s_... --out reports/one.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.reporting import load_session_events, render_markdown


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--session", action="append", default=[])
    parser.add_argument("--out", default="reports/self-test.md")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if args.session:
        session_ids = args.session
    else:
        sessions_dir = data_dir / "sessions"
        session_ids = sorted(p.name for p in sessions_dir.iterdir() if p.is_dir()) if sessions_dir.exists() else []

    sessions = [(sid, load_session_events(data_dir, sid)) for sid in session_ids]
    sessions = [(sid, events) for sid, events in sessions if events]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(data_dir, sessions), encoding="utf-8")
    print(f"wrote {out} ({len(sessions)} sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
