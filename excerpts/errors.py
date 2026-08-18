# NOTE: Excerpt from ops-system, the private production repo this showcase
# curates. Imports reference the full codebase and will not resolve here;
# the file is otherwise verbatim.
# SPDX-License-Identifier: Apache-2.0
"""Canonical structured-error builder for agent_messages.last_error
and agent_message_attempts.error JSONB columns.

Bucket 3 item #2 (2026-04-27). Replaces free-text error strings with
an 8-key shape that's queryable via SQL: every error has a type
(exception class name) and category ("transient" | "permanent" |
"config") so we can aggregate "all permanent errors of class X in
last 24h" without regex.

Single helper: build_error(exc=None, *, error_type, message, ...).
Accepts either an Exception or explicit type+message (for synthetic
errors like reaper revivals where there's no exception, just a state
observation).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from services.trace_context import get_trace_id

ErrorCategory = Literal["transient", "permanent", "config"]
ErrorPhase = Literal["claim", "dispatch", "finalize", "reap"]

# Trim message to a sane bound. Postgres JSONB has no hard size limit,
# but 500 chars is plenty for a debug field; full tracebacks stay in
# the structured logs (services/logger.py).
_MESSAGE_MAX_LEN = 500


def build_error(
    exc: Optional[Exception] = None,
    *,
    error_type: Optional[str] = None,
    message: Optional[str] = None,
    category: ErrorCategory = "transient",
    attempt: Optional[int] = None,
    agent: Optional[str] = None,
    phase: Optional[ErrorPhase] = None,
) -> dict[str, Any]:
    """Build the canonical structured-error dict.

    Expects: either an Exception (auto-fills type + message) OR explicit
      error_type + message (for synthetic errors -- e.g., reaper revivals,
      where there's no exception, just an observed state). Both forms
      can be combined: explicit message overrides exc's str(); explicit
      error_type overrides exc's class name.
    Guarantees: returns an 8-key dict ready to spread into a JSONB column.
      type defaults to "UnknownError" if neither exc nor error_type given.
      message is truncated to 500 chars. trace_id auto-pulled from
      contextvars (None when called outside a request context).
    Missing inputs: any kwarg is None-able. category defaults to
      "transient" (most errors are; caller overrides for known-permanent
      cases like exhausted-retry permanent failure).
    Next agent: poller spreads this into agent_messages.last_error;
      record_message_attempt accepts dict | Exception | None and spreads
      to agent_message_attempts.error.
    """
    if exc is not None:
        error_type = error_type or type(exc).__name__
        message = message or str(exc)
    # F3/SDA-76: scrub BEFORE truncation and before this dict is spread into
    # agent_messages.last_error (JSONB) and logged. `str(exc)` on a DB or HTTP
    # failure routinely carries a connection URL or an Authorization header, and
    # nothing on this path scrubbed it. Lazy import mirrors logger.py — redaction
    # imports get_logger, so a module-level import risks an import cycle.
    if message:
        try:
            from services.redaction import redact_secrets
            message, _hits = redact_secrets(message)
        except Exception:  # noqa: BLE001 -- error construction must never raise
            pass
    return {
        "type": error_type or "UnknownError",
        "message": (message or "")[:_MESSAGE_MAX_LEN],
        "category": category,
        "attempt": attempt,
        "agent": agent,
        "phase": phase,
        "trace_id": get_trace_id(),
        "at": datetime.now(timezone.utc).isoformat(),
    }
