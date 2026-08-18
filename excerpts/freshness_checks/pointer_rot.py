# NOTE: Excerpt from ops-system, the private production repo this showcase
# curates. Imports reference the full codebase and will not resolve here;
# the file is otherwise verbatim.
# SPDX-License-Identifier: Apache-2.0
"""Pointer-rot check — does every registry path resolve on disk?

Scope discipline (per the P1=A lean): registry-level pointer-rot only.
Doc-level cross-reference checking (markdown links inside each registered
doc) is deferred to its own check when the v1 surface earns it.

Expects: a list of registry-row dicts from services.freshness_engine
.parse_markdown_tables. Each dict carries at minimum a `path` key (the
column the engine identifies the row by) and ideally a `Name` key (for
human-readable failure messages).
Guarantees: a list of CheckResult objects, one per row with a non-TBD
path: status=pass when the path resolves to a file, status=fail when it
doesn't. TBD paths are skipped silently (per the per-doc-when-touched
discipline — the engine doesn't watch docs that haven't been authored
yet).
Missing: a row with no `path` key is skipped; a path that resolves to a
directory rather than a file is treated as fail (registry should never
point at a directory).
Next consumer: services.freshness_engine aggregates results into the
JSONL store and the pre-commit context fails the commit on any fail
(per blocks_commit=True in the check registration).
"""
from __future__ import annotations

from pathlib import Path

from services.freshness_checks import CheckResult, now_iso

# Repo root is three levels up from this file:
# services/freshness_checks/pointer_rot.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def check(rows: list[dict]) -> list[CheckResult]:
    results: list[CheckResult] = []
    run_at = now_iso()
    for row in rows:
        raw_path = row.get("path", "").strip().strip("`")
        if not raw_path or raw_path.upper() == "TBD":
            continue
        name = row.get("Name", row.get("name", "?"))
        resolved = _REPO_ROOT / raw_path
        if resolved.is_file():
            results.append(CheckResult(
                check_name="pointer_rot",
                doc_path=raw_path,
                status="pass",
                reason=f"path resolves: {resolved}",
                suggested_action="",
                run_at=run_at,
            ))
        else:
            results.append(CheckResult(
                check_name="pointer_rot",
                doc_path=raw_path,
                status="fail",
                reason=(
                    f"registry row '{name}' declares path {raw_path!r} but file does not exist "
                    f"(resolved: {resolved})"
                ),
                suggested_action=(
                    "Either the file moved or was deleted, or the registry row's path is stale. "
                    "Verify the file's actual location and either move it back, update the "
                    "registry path, or remove the row if the doc was retired."
                ),
                run_at=run_at,
            ))
    return results
