# NOTE: Excerpt from ops-system, the private production repo this showcase
# curates. Imports reference the full codebase and will not resolve here;
# the file is otherwise verbatim.
# SPDX-License-Identifier: Apache-2.0
"""Drift sweep — the autonomous detect-and-fix engine (drift-automation D).

This is the heart of "docs fresh on their own" (project memory: drift-
autonomy north star). It does two things the write-time checks (stage A)
and the brief surface (stage C) don't:

  1. DETECT drift outside a commit (so drift that slipped in via
     `--no-verify`, a direct push, or a check that wasn't yet registered
     still gets caught on a schedule).
  2. FIX the mechanical drift automatically — a count tag whose value
     drifted from the source-of-record has a *computed* correct value, so
     the edit is deterministic (no judgment). These are the only fixes the
     auto-merge lane is ever allowed to touch (Shannon's ruling 2026-06-22:
     "auto-merge mechanical only; PR the rest"; brief escalation trigger:
     "if the auto-merge lane would touch anything beyond a tagged count
     edit, stop").

Everything else — dangling BL refs, cross-doc disagreements with no truth
source, and (future) semantic prose drift — is classified HUMAN and routed
to a PR for approval, never auto-fixed.

The monthly `/schedule` cloud routine runs this, opens a PR with the
mechanical fixes applied + the human items listed, and pings ntfy. Until
the go-live test round-trips, even mechanical fixes ship as a PR (no
auto-merge). This module is also runnable on demand ("run the drift
sweep") — it does not depend on the scheduler.

Expects: nothing (walks the registry + filesystem like the stage-A checks).
  Optional apply=True to write mechanical fixes to disk.
Guarantees: a SweepResult with `mechanical` (auto-fixable count drift) and
  `human` (everything needing judgment) finding lists, plus `fixed` (the
  edits applied when apply=True). Pure detection when apply=False.
Missing: a truth source that can't compute (e.g. solto-standards absent)
  routes its tag to `human` as "could not verify", never a silent skip or
  a bogus auto-fix.
Next consumer: scripts/drift_sweep.py (CLI) and the /schedule routine.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from services.freshness_checks.count_tags import (
    TRUTH_SOURCES,
    TruthSourceError,
    scan_tags,
)
from services.freshness_checks.commitment_integrity import (
    _defined_ids,
    _iter_scan_files,
    _BL_RE,
)
from services.logger import get_logger

log = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclasses.dataclass
class DriftItem:
    kind: str          # 'count' | 'cross_doc' | 'commitment' | 'unverifiable'
    doc_path: str      # repo-relative
    line: int
    metric: str        # the count metric, or '' for commitment
    claimed: int | None
    truth: int | None
    detail: str        # human-readable description


@dataclasses.dataclass
class FixApplied:
    doc_path: str
    line: int
    metric: str
    old_value: int
    new_value: int


@dataclasses.dataclass
class SweepResult:
    mechanical: list[DriftItem]   # auto-fixable (deterministic count edits)
    human: list[DriftItem]        # needs judgment → PR for approval
    fixed: list[FixApplied]       # edits actually written (apply=True only)

    @property
    def clean(self) -> bool:
        return not self.mechanical and not self.human


def _resolve(rel: str) -> Path | None:
    """Repo-relative markdown path under the repo (symlinks not followed —
    same rule as the stage-A checks; keeps solto-standards repo-relative)."""
    rel = (rel or "").strip().strip("`")
    if not rel or not rel.endswith(".md") or rel.startswith("/") or ".." in rel:
        return None
    p = _REPO_ROOT / rel
    return p if p.is_file() else None


def _canon_docs() -> list[Path]:
    from services.freshness_engine import load_registry
    rows = load_registry() or []
    docs = {p for r in rows if (p := _resolve(r.get("path", ""))) is not None}
    return sorted(docs, key=lambda p: str(p))


def detect() -> SweepResult:
    """Find all drift. Mechanical = count tags whose value != computed
    truth (deterministic fix). Human = dangling BL refs + count tags whose
    truth can't be computed (unverifiable). Cross-doc disagreement collapses
    into the per-tag count check: every tag is compared to truth, so a
    disagreeing tag shows up as its own count drift."""
    mechanical: list[DriftItem] = []
    human: list[DriftItem] = []

    truth_cache: dict[str, int] = {}
    truth_err: dict[str, TruthSourceError] = {}

    def _truth(metric: str):
        if metric in truth_cache:
            return truth_cache[metric], None
        if metric in truth_err:
            return None, truth_err[metric]
        try:
            v = TRUTH_SOURCES[metric]()
            truth_cache[metric] = v
            return v, None
        except TruthSourceError as exc:
            truth_err[metric] = exc
            return None, exc

    # Count drift (mechanical when truth is computable).
    for doc in _canon_docs():
        rel = doc.relative_to(_REPO_ROOT).as_posix()
        for hit in scan_tags(doc.read_text(encoding="utf-8")):
            if hit.metric not in TRUTH_SOURCES:
                human.append(DriftItem(
                    kind="unverifiable", doc_path=rel, line=hit.line,
                    metric=hit.metric, claimed=hit.value, truth=None,
                    detail=(f"tag `count:{hit.metric}` has no registered truth "
                            f"source (typo or deferred metric) — needs a human"),
                ))
                continue
            truth, err = _truth(hit.metric)
            if err is not None:
                human.append(DriftItem(
                    kind="unverifiable", doc_path=rel, line=hit.line,
                    metric=hit.metric, claimed=hit.value, truth=None,
                    detail=(f"tag `count:{hit.metric}`={hit.value} could not be "
                            f"verified: {err}"),
                ))
                continue
            if hit.value != truth:
                mechanical.append(DriftItem(
                    kind="count", doc_path=rel, line=hit.line,
                    metric=hit.metric, claimed=hit.value, truth=truth,
                    detail=(f"`count:{hit.metric}` claims {hit.value}, "
                            f"source-of-record has {truth}"),
                ))

    # Dangling BL refs — always human (can't invent a BACKLOG entry).
    defined = _defined_ids()
    if defined is None:
        human.append(DriftItem(
            kind="commitment", doc_path="BACKLOG.md", line=0, metric="",
            claimed=None, truth=None,
            detail="BACKLOG.md missing — cannot verify BL references",
        ))
    else:
        for path in _iter_scan_files():
            rel = path.relative_to(_REPO_ROOT).as_posix()
            seen: set[int] = set()
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for m in _BL_RE.finditer(line):
                    bl = int(m.group(1))
                    if bl not in defined and bl not in seen:
                        seen.add(bl)
                        human.append(DriftItem(
                            kind="commitment", doc_path=rel, line=i, metric="",
                            claimed=None, truth=None,
                            detail=(f"references BL-{bl} with no BACKLOG entry "
                                    f"(file the commitment or fix the ref)"),
                        ))

    return SweepResult(mechanical=mechanical, human=human, fixed=[])


def _rewrite_tag(line: str, metric: str, old: int, new: int) -> str:
    """Replace `<!-- count:metric -->OLD` with `...NEW` on one line,
    preserving the comment's exact spacing. Only the matched tag's number
    changes; surrounding prose is untouched."""
    pattern = re.compile(
        r"(<!--\s*count:" + re.escape(metric) + r"\s*-->\s*)" + str(old) + r"\b"
    )
    return pattern.sub(lambda m: m.group(1) + str(new), line, count=1)


def fix_mechanical(result: SweepResult, *, apply: bool = False) -> SweepResult:
    """Apply the deterministic count-tag fixes in `result.mechanical`. With
    apply=False this is a no-op preview (callers print result.mechanical).
    With apply=True it rewrites each drifted tag to its computed truth and
    records the edit in result.fixed. Idempotent: re-running on fixed docs
    finds nothing."""
    if not apply:
        return result
    # Group fixes by doc so each file is read/written once.
    by_doc: dict[str, list[DriftItem]] = {}
    for item in result.mechanical:
        by_doc.setdefault(item.doc_path, []).append(item)

    for rel, items in by_doc.items():
        path = _REPO_ROOT / rel
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for item in items:
            idx = item.line - 1
            if not (0 <= idx < len(lines)):
                log.warning("drift_sweep_fix_line_out_of_range",
                            doc=rel, line=item.line)
                continue
            newline = _rewrite_tag(lines[idx], item.metric, item.claimed, item.truth)
            if newline == lines[idx]:
                log.warning("drift_sweep_fix_no_match", doc=rel, line=item.line,
                            metric=item.metric)
                continue
            lines[idx] = newline
            result.fixed.append(FixApplied(
                doc_path=rel, line=item.line, metric=item.metric,
                old_value=item.claimed, new_value=item.truth,
            ))
        path.write_text("".join(lines), encoding="utf-8")
    return result


def sweep(*, apply: bool = False) -> SweepResult:
    """Detect drift, optionally auto-fix the mechanical part. The one entry
    point the CLI + the /schedule routine call."""
    result = detect()
    if apply and result.mechanical:
        fix_mechanical(result, apply=True)
    return result
