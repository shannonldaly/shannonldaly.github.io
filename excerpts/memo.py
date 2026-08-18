# NOTE: Excerpt from ops-system, the private production repo this showcase
# curates. Imports reference the full codebase and will not resolve here;
# the file is otherwise verbatim.
# SPDX-License-Identifier: Apache-2.0
"""BL-203 — verdict + extraction memoization: full coverage without re-spending.

Why (decisions/2026-07-07-kl-verdict-memoization.md): the daily sweep re-judged
every claim on every run (~$6.50/day) even when NOTHING the judge looks at had
changed — and on 2026-07-07 that spend pattern exhausted the monthly API cap a
week into the month. The grounded judge is a pure function of its prompt:
(system prompt, model, doc claims, deterministic ground bundle). If every one
of those bytes is identical to a prior run, the verdict is a REPLAY, not a
guess — so carrying it forward preserves the full-canon coverage law (partial
coverage is net-negative; this is total coverage where unchanged inputs reuse
the already-paid answer).

THE ELIGIBILITY RULE (load-bearing — trust over savings):
  - `grounded` and `temporal` verdicts memoize: their inputs are fully captured
    by the key (claim fields + bundle + prompt/model fingerprint).
  - `agentic` verdicts NEVER memoize: the agentic judge explores live repo
    state beyond the bundle, so its inputs are not fully captured — an
    escalated claim re-runs the cascade every sweep.
  - `failsafe` verdicts NEVER memoize: an API error is not an answer (the
    2026-07-07 usage-cap outage minted 862 of these; replaying them would have
    frozen an outage into the record).
  - Any prompt, model, or memo-schema change rotates the fingerprint and
    busts EVERYTHING — a tuned judge never serves a stale predecessor's calls.

Store: .knowledge_layer/kl_memo.json — a local derived CACHE, gitignored, never
the record (kl_traces in Postgres is the record; every carried verdict still
flows through persist_run like a fresh one). Corrupt/missing store → loud log,
fresh start, one full-price sweep rebuilds it. Delete the file to force a
cold sweep.

Expects: Claim objects + the sweep's doc text; build_bundle for keying.
Guarantees: get_* return exact prior objects or None (never a partial); record_*
  writes only eligible verdicts; counters expose hits/misses for the report
  (silent savings would hide staleness bugs — the report prints them).
Missing/corrupt store: loud log + empty store, never a crash, never a guess.
Next consumer: scripts/kl_sweep.py _process splits claims via this module and
  reports the counts; the cost METER only sees genuinely-judged claims.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from services.knowledge_layer import extract as _extract
from services.knowledge_layer import judge_grounded as _grounded
from services.knowledge_layer.extract import Claim
from services.knowledge_layer.ground import build_bundle
from services.knowledge_layer.judge import Atom, Verdict
from services.logger import get_logger

log = get_logger(__name__)

from services.knowledge_layer.statedir import state_dir

_SCHEMA_VERSION = "1"  # bump to force a global bust on memo-logic changes
_STORE_PATH = state_dir() / "kl_memo.json"

_MEMOIZABLE_JUDGES = frozenset({"grounded", "temporal"})


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _judge_fingerprint() -> str:
    """Everything that shapes a grounded verdict besides (claim, bundle).
    Deliberately imports the judge's private prompt/model — the coupling is the
    point: edit the prompt or swap the model and every memo key rotates."""
    return _sha(_SCHEMA_VERSION, _grounded._MODEL, _grounded._SYSTEM)


def _extract_fingerprint() -> str:
    return _sha(_SCHEMA_VERSION, _extract._MODEL, _extract._SYSTEM)


def claim_key(claim: Claim, bundle: str) -> str:
    """The grounded judge's exact inputs for one claim, hashed."""
    return _sha(_judge_fingerprint(), claim.doc_path, claim.claim_type,
                claim.text, claim.quote, str(claim.line), claim.ground_hint, bundle)


def extraction_key(doc_text: str) -> str:
    return _sha(_extract_fingerprint(), doc_text)


def chunk_key(chunk_text: str) -> str:
    """Chunk-tier key (2026-07-13, cost phase 1): content-addressed on the
    window text + the extract fingerprint — same bust discipline as the doc
    tier (prompt/model/schema rotation invalidates everything). Line-window
    boundaries mean an edit that shifts line counts invalidates the edited
    window AND every window after it in the doc; unchanged-boundary windows
    keep hitting. Accepted for phase 1; section-based chunking (stable keys)
    is gated behind a fixture-eval receipt."""
    return _sha(_extract_fingerprint(), chunk_text)


class MemoStore:
    """One sweep's view of the memo file. Load once, save once at sweep end."""

    def __init__(self, path: Path = _STORE_PATH, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.hits_claims = 0        # docs whose extraction was replayed
        self.hits_verdicts = 0      # claims whose verdict was replayed
        self.judged_fresh = 0       # claims that actually hit the API
        self.hits_chunks = 0        # windows served from the chunk tier
        self.missed_chunks = 0      # windows extracted fresh
        self._claims: dict[str, list[dict]] = {}
        self._verdicts: dict[str, dict] = {}
        self._chunks: dict[str, list[dict]] = {}
        self._dirty = False
        if enabled:
            self._load()

    # ----- persistence -----

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("schema") != _SCHEMA_VERSION:
                log.info("kl_memo_schema_rotated", found=raw.get("schema"),
                         expected=_SCHEMA_VERSION)
                return  # stale schema → cold start
            self._claims = raw.get("claims", {})
            self._verdicts = raw.get("verdicts", {})
            self._chunks = raw.get("chunks", {})
        except (json.JSONDecodeError, OSError) as exc:
            # Corrupt cache is an inconvenience, not a crisis — loud + cold.
            log.warning("kl_memo_store_unreadable_cold_start",
                        path=str(self.path), error=str(exc))
            self._claims, self._verdicts, self._chunks = {}, {}, {}

    def save(self) -> None:
        if not (self.enabled and self._dirty):
            return
        try:
            tmp = self.path.with_suffix(".json.tmp")
            payload = {"schema": _SCHEMA_VERSION, "claims": self._claims,
                       "verdicts": self._verdicts, "chunks": self._chunks}
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            # The mirror of _load's posture: an unwritable CACHE is an
            # inconvenience, never a crash — the record is kl_traces in
            # Postgres, and losing the memo only means the next run re-pays.
            # 2026-07-09: this exact write killed the first armed sweep AFTER
            # judging, losing the whole paid run before persistence.
            log.error("kl_memo_store_unwritable_run_continues",
                      path=str(self.path), error=str(exc))

    # ----- extraction memo -----

    def get_claims(self, doc_text: str) -> list[Claim] | None:
        if not self.enabled:
            return None
        rows = self._claims.get(extraction_key(doc_text))
        if rows is None:
            return None
        try:
            claims = [Claim(**r) for r in rows]
        except TypeError as exc:  # Claim schema drifted since the row was stored
            log.warning("kl_memo_claim_shape_stale", error=str(exc))
            return None
        self.hits_claims += 1
        return claims

    def put_claims(self, doc_text: str, claims: list[Claim]) -> None:
        if not self.enabled:
            return
        self._claims[extraction_key(doc_text)] = [dataclasses.asdict(c) for c in claims]
        self._dirty = True

    # ----- chunk-tier extraction memo (phase 1, 2026-07-13) -----

    def get_chunk(self, chunk_text: str) -> list[dict] | None:
        """Raw cached rows for one window, or None. Rows are the extractor's
        _ExtractedClaim dumps — the CALLER validates them back into models
        (a validation failure is treated as a miss, never a crash)."""
        if not self.enabled:
            return None
        rows = self._chunks.get(chunk_key(chunk_text))
        if rows is None:
            self.missed_chunks += 1
            return None
        self.hits_chunks += 1
        return rows

    def put_chunk(self, chunk_text: str, rows: list[dict]) -> None:
        if not self.enabled:
            return
        self._chunks[chunk_key(chunk_text)] = rows
        self._dirty = True

    # ----- verdict memo -----

    def split(self, claims: list[Claim]) -> tuple[dict[int, Verdict], list[int]]:
        """Partition judgeable claims into {index: replayed Verdict} and the
        indices that must be judged fresh. Bundles are rebuilt here ($0,
        deterministic) — a changed bundle changes the key, so a claim whose
        ground truth moved ALWAYS re-judges."""
        carried: dict[int, Verdict] = {}
        to_judge: list[int] = []
        if not self.enabled:
            return carried, list(range(len(claims)))
        for i, claim in enumerate(claims):
            row = self._verdicts.get(claim_key(claim, build_bundle(claim)))
            if row is None:
                to_judge.append(i)
                continue
            try:
                atoms = [Atom(**a) for a in row.get("atoms", [])]
                carried[i] = Verdict(
                    claim=claim, decision=row["decision"],
                    confidence=row["confidence"], reason=row["reason"],
                    evidence=row.get("evidence", ""),
                    proposed_old=row.get("proposed_old", ""),
                    proposed_new=row.get("proposed_new", ""),
                    atoms=atoms, judge=row.get("judge", ""))
            except (KeyError, TypeError) as exc:  # stored shape stale → re-judge
                log.warning("kl_memo_verdict_shape_stale", error=str(exc))
                to_judge.append(i)
                continue
        self.hits_verdicts += len(carried)
        return carried, to_judge

    def record(self, verdicts: list[Verdict], run_id: str) -> None:
        """Store eligible fresh verdicts under their input keys."""
        self.judged_fresh += len(verdicts)
        if not self.enabled:
            return
        for v in verdicts:
            if v.judge not in _MEMOIZABLE_JUDGES:
                continue  # agentic/failsafe/'' — see THE ELIGIBILITY RULE
            row = {"decision": v.decision, "confidence": v.confidence,
                   "reason": v.reason, "evidence": v.evidence,
                   "proposed_old": v.proposed_old, "proposed_new": v.proposed_new,
                   "atoms": [dataclasses.asdict(a) for a in v.atoms],
                   "judge": v.judge, "source_run": run_id}
            self._verdicts[claim_key(v.claim, build_bundle(v.claim))] = row
            self._dirty = True

    def summary(self) -> str:
        return (f"memo: {self.hits_verdicts} verdicts replayed · "
                f"{self.judged_fresh} judged fresh · "
                f"{self.hits_claims} docs' extraction replayed · "
                f"chunks {self.hits_chunks} replayed / {self.missed_chunks} fresh")
