# NOTE: Excerpt from ops-system, the private production repo this showcase
# curates. Imports reference the full codebase and will not resolve here;
# the file is otherwise verbatim.
# SPDX-License-Identifier: Apache-2.0
"""Universal budget ceiling — fail-closed when a paid run crosses its cap.

The backstop for when a cost preview is still wrong (it sometimes will be):
a run takes a dollar ceiling and STOPS when the running METER total crosses
it, instead of running to completion on a bad estimate. Damage is capped,
not discovered on the invoice. And a budget-kill isn't wasted spend — the
seed's skip-fix and prove's memo-harvest mean completed work persists and
the resume is cheap.

Generalizes the KL_SEMANTIC_BUDGET_USD pattern (2026-07-17 S2 ruling) into
one helper every paid path can call between work units.

Expects: a ceiling in USD (None = no cap) and the METER (or any object with
a summary()['total_usd']).
Guarantees: check() raises BudgetExceededError the first time spent >=
ceiling; loud log on the trip. Pure; no spend, no DB.
Next consumer: kl_seed_decisions (--budget), kl_prove (--budget); the
sweep's semantic lane kept its own KL_SEMANTIC_BUDGET_USD guard until the
lane retired (SDA-51).
"""
from __future__ import annotations

from services.logger import get_logger

log = get_logger(__name__)


class BudgetExceededError(RuntimeError):
    """A paid run crossed its dollar ceiling and was stopped fail-closed."""


def check(ceiling_usd: float | None, spent_usd: float, *, label: str = "run") -> None:
    """Raise BudgetExceededError if a ceiling is set and spend has crossed it.

    Call between work units. None ceiling = no cap (caller opted out)."""
    if ceiling_usd is None:
        return
    if spent_usd >= ceiling_usd:
        log.error("kl_budget_exceeded", label=label, spent=round(spent_usd, 2),
                  ceiling=ceiling_usd)
        raise BudgetExceededError(
            f"{label}: spend ${spent_usd:.2f} reached the ${ceiling_usd:.2f} "
            f"budget ceiling — stopped fail-closed. Completed work persists; "
            f"resume is cheap (already-done units are skipped/replayed).")


def meter_spent(meter=None) -> float:
    """Current run spend from the process METER (0.0 if unavailable)."""
    try:
        from services.knowledge_layer.cost import METER
        return float((meter or METER).summary().get("total_usd", 0.0))
    except Exception:  # noqa: BLE001 — a guard read must never crash the run
        return 0.0


# ---- E15 (SDA-29 slice 2): the worker-side HALT ----
# The run arms once (env ceiling composed with the quote), then every work
# unit calls check_run() — one module-level slot per process, reset per run
# (the METER precedent: documented state, not hidden).

_CEILING_ENV = "KL_SPEND_CEILING_USD"
_MARGIN_ENV = "KL_SPEND_CEILING_MARGIN"
_DEFAULT_MARGIN = 2.0

_armed: dict = {"ceiling": None, "label": "run"}


def effective_ceiling(quote_high: float | None = None) -> float | None:
    """Compose the run ceiling: min(env absolute cap, quote.high x margin).

    Expects: the quote's high-band estimate (None when unquoted). Reads
      KL_SPEND_CEILING_USD (absolute, optional) and KL_SPEND_CEILING_MARGIN
      (default 2.0 — even a wrong week-one quote bounds damage to the
      margin, never 3x open-ended).
    Guarantees: the tightest applicable ceiling, or None when neither input
      exists (no cap — the pre-E15 posture, opted into by absence).
    Malformed env values: ignored loud (a broken cap must not become no-cap
      silently — it logs and the other candidate still applies).
    Next consumer: kl_sweep arms the run with this.
    """
    import os
    candidates: list[float] = []
    raw = os.environ.get(_CEILING_ENV, "").strip()
    if raw:
        try:
            candidates.append(float(raw))
        except ValueError:
            log.error("kl_spend_ceiling_env_malformed", raw=raw)
    if quote_high is not None and quote_high > 0:
        try:
            margin = float(os.environ.get(_MARGIN_ENV, "") or _DEFAULT_MARGIN)
        except ValueError:
            margin = _DEFAULT_MARGIN
        candidates.append(quote_high * margin)
    return min(candidates) if candidates else None


def arm_run(ceiling: float | None, *, label: str = "run") -> None:
    """Arm (or disarm with None) the per-run ceiling check_run() enforces."""
    _armed["ceiling"] = ceiling
    _armed["label"] = label
    if ceiling is not None:
        log.info("kl_budget_armed", label=label, ceiling=round(ceiling, 2))


def armed_ceiling() -> float | None:
    """The currently armed ceiling (None = no cap)."""
    return _armed["ceiling"]


def check_run(meter=None) -> None:
    """The between-work-units halt: check the armed ceiling vs METER spend.

    Expects: arm_run() called at run start (unarmed = no cap, no raise).
    Guarantees: raises BudgetExceededError the first time spend crosses the
      armed ceiling; otherwise returns None. Delegates to check() — one
      halt semantic, one error type.
    Next consumer: kl_sweep's per-doc entry points; the halt handler writes
      the ledger marker the spend-halt PAGE reads.
    """
    check(_armed["ceiling"], meter_spent(meter), label=_armed["label"])


__all__ = ["BudgetExceededError", "check", "meter_spent",
           "effective_ceiling", "arm_run", "armed_ceiling", "check_run"]
