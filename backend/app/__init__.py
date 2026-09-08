"""EGA Update Console — backend package (V1).

FastAPI API + independent systemd worker share SQLite state and redacted
JSONL logs. Python 3.10 compatible (no 3.11+ syntax).
"""
from __future__ import annotations

__version__ = "1.0.0"

TOOL_IDS = ("hermes", "opencode", "codex", "t3")

JOB_NONTERMINAL = ("accepted", "preflight", "backup", "updating", "verifying")
JOB_TERMINAL = ("succeeded", "blocked", "failed", "health_failed", "interrupted")
