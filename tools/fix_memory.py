"""
fix_memory.py — AI Ops Debugger - Fix Memory

Logs successful bug fixes / feature implementations to JSONL and retrieves
recent history so future agent runs can learn from past fixes.

Storage: agent_logs/fix_history.jsonl (relative to this script's directory)

Usage (CLI):
    python fix_memory.py show [--limit N]   # Show last N fixes (default 15)
    python fix_memory.py summary            # Show pattern summary
    python fix_memory.py test               # Append a fake fix entry for testing

Programmatic:
    from fix_memory import get_recent_fixes, append_fix, get_patterns_summary
"""

import argparse
import fcntl
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_LOG_DIR = _SCRIPT_DIR / "agent_logs"
_JSONL_PATH = _LOG_DIR / "fix_history.jsonl"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_log_dir() -> None:
    """Create agent_logs/ if it does not exist."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _classify_pattern(root_cause: str) -> str:
    """Classify the fix pattern from the root cause text."""
    rc = root_cause.lower()
    if "organization_id" in rc:
        return "missing_org_id_filter"
    if "import" in rc:
        return "missing_import"
    if "template" in rc or "render" in rc:
        return "missing_template_variable"
    if "keyerror" in rc or "key" in rc:
        return "missing_key"
    if "none" in rc or "null" in rc:
        return "null_reference"
    if "query" in rc or "select" in rc or "sql" in rc:
        return "bad_query"
    return "other"


def _parse_field(text: str, markers: list[str]) -> str:
    """Return the text after the first matching marker (to end-of-line)."""
    for marker in markers:
        pattern = re.compile(re.escape(marker) + r"\s*(.*)", re.IGNORECASE)
        m = pattern.search(text)
        if m:
            return m.group(1).strip()
    return ""


def _extract_file_paths(text: str) -> list[str]:
    """Extract file paths that look like Python source files."""
    # Match paths like app/foo/bar.py or app/baz.py — at least one slash
    return re.findall(r"(?:[\w./-]+/)+[\w.-]+\.py", text)


def _relative_time(ts_str: str) -> str:
    """Return a human-readable relative time string from an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return "unknown time ago"

    # Ensure both are offset-aware (UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    delta = now - ts
    total_seconds = int(delta.total_seconds())

    if total_seconds < 0:
        return "just now"
    if total_seconds < 60:
        return "just now"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = total_seconds // 3600
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = total_seconds // 86400
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    months = days // 30
    return f"{months} month{'s' if months != 1 else ''} ago"


def _read_all_entries() -> list[dict]:
    """Read all valid entries from the JSONL file, skipping corrupted lines."""
    if not _JSONL_PATH.exists():
        return []
    entries = []
    with open(_JSONL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # Graceful degradation: skip corrupted lines
                continue
    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_fix(task_id: str, agent_output: str, elapsed_seconds: float = 0) -> dict:
    """Parse agent Gate 5 report output, append structured entry to JSONL.

    Parameters
    ----------
    task_id : str
        Unique task identifier (e.g. "20260226_153000").
    agent_output : str
        The agent's raw Gate 5 report text.
    elapsed_seconds : float, optional
        Wall-clock seconds the fix took.

    Returns
    -------
    dict
        The entry that was appended.
    """
    _ensure_log_dir()

    # --- Parse fields from agent output --------------------------------
    root_cause = _parse_field(agent_output, ["ROOT CAUSE:", "Root cause:"])
    description = _parse_field(agent_output, ["ISSUE:", "Issue:", "FEATURE:", "Feature:"])
    commit_line = _parse_field(agent_output, ["COMMIT:", "Commit:"])
    commit_sha = commit_line.split()[0] if commit_line.split() else ""

    # Files changed: try the FIX: / FILES CHANGED: lines, then fall back to
    # scanning the entire output for Python file paths.
    fix_line = _parse_field(agent_output, ["FIX:", "Fix:", "FILES CHANGED:", "Files changed:"])
    files_changed = _extract_file_paths(fix_line)
    if not files_changed:
        files_changed = _extract_file_paths(agent_output)
    # Deduplicate while preserving order
    seen = set()
    unique_files = []
    for fp in files_changed:
        if fp not in seen:
            seen.add(fp)
            unique_files.append(fp)
    files_changed = unique_files

    # Determine task type heuristic
    output_lower = agent_output.lower()
    if "feature:" in output_lower or "feature " in output_lower:
        task_type = "feature"
    else:
        task_type = "bug"

    fix_pattern = _classify_pattern(root_cause)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "task_type": task_type,
        "description": description,
        "root_cause": root_cause,
        "files_changed": files_changed,
        "fix_pattern": fix_pattern,
        "commit_sha": commit_sha,
        "elapsed_seconds": int(elapsed_seconds),
    }

    # Append with file locking for safe concurrent access
    with open(_JSONL_PATH, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    return entry


def get_recent_fixes(limit: int = 15) -> str:
    """Return a human-readable context block of the last *limit* fixes.

    Returns an empty string if there are no entries.
    """
    entries = _read_all_entries()
    if not entries:
        return ""

    recent = entries[-limit:]

    lines = ["RECENT FIX HISTORY (for context \u2014 do not repeat past mistakes):", ""]
    for idx, entry in enumerate(recent, start=1):
        ts = entry.get("timestamp", "")
        desc = entry.get("description", "(no description)")
        root_cause = entry.get("root_cause", "")
        files = entry.get("files_changed", [])
        pattern = entry.get("fix_pattern", "other")

        rel = _relative_time(ts)
        files_str = ", ".join(files) if files else "(none)"

        lines.append(f"Fix {idx} ({rel}): \"{desc}\"")
        lines.append(f"  Root cause: {root_cause}")
        lines.append(f"  Files: {files_str}")
        lines.append(f"  Pattern: {pattern}")
        lines.append("")

    return "\n".join(lines).rstrip()


def get_patterns_summary() -> dict:
    """Aggregate fix patterns and most-changed files across all entries.

    Returns
    -------
    dict
        Keys: total_fixes, patterns, most_common, most_changed_files
    """
    entries = _read_all_entries()
    if not entries:
        return {
            "total_fixes": 0,
            "patterns": {},
            "most_common": "",
            "most_changed_files": [],
        }

    pattern_counter: Counter = Counter()
    file_counter: Counter = Counter()

    for entry in entries:
        pattern_counter[entry.get("fix_pattern", "other")] += 1
        for fp in entry.get("files_changed", []):
            file_counter[fp] += 1

    most_common = pattern_counter.most_common(1)[0][0] if pattern_counter else ""
    most_changed = [fp for fp, _ in file_counter.most_common(5)]

    return {
        "total_fixes": len(entries),
        "patterns": dict(pattern_counter.most_common()),
        "most_common": most_common,
        "most_changed_files": most_changed,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_show(limit: int) -> None:
    output = get_recent_fixes(limit=limit)
    if not output:
        print("No fix history found.")
    else:
        print(output)


def _cli_summary() -> None:
    summary = get_patterns_summary()
    if summary["total_fixes"] == 0:
        print("No fix history found.")
        return
    print(f"Total fixes: {summary['total_fixes']}")
    print(f"Most common pattern: {summary['most_common']}")
    print()
    print("Patterns:")
    for pattern, count in sorted(summary["patterns"].items(), key=lambda x: -x[1]):
        print(f"  {pattern}: {count}")
    print()
    print("Most changed files:")
    for fp in summary["most_changed_files"]:
        print(f"  {fp}")


def _cli_test() -> None:
    fake_output = (
        "ISSUE: Dashboard shows 0 properties instead of 60\n"
        "ROOT CAUSE: dashboard_service.get_metrics() missing organization_id filter\n"
        "FIX: app/services/dashboard_service.py:42 \u2014 added .eq(\"organization_id\", org_id)\n"
        "COMMIT: abc1234 fix: add organization_id filter to dashboard metrics\n"
        "VALIDATION: test_dashboard passed\n"
        "SMOKE TEST: ALL SMOKE TESTS PASS\n"
    )
    entry = append_fix(
        task_id=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
        agent_output=fake_output,
        elapsed_seconds=480,
    )
    print("Appended test entry:")
    print(json.dumps(entry, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix Memory — view and manage agent fix history"
    )
    subparsers = parser.add_subparsers(dest="command")

    show_parser = subparsers.add_parser("show", help="Show recent fixes")
    show_parser.add_argument(
        "--limit", type=int, default=15, help="Number of entries to show (default 15)"
    )

    subparsers.add_parser("summary", help="Show patterns summary")
    subparsers.add_parser("test", help="Append a fake fix entry for testing")

    args = parser.parse_args()

    if args.command == "show":
        _cli_show(args.limit)
    elif args.command == "summary":
        _cli_summary()
    elif args.command == "test":
        _cli_test()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
