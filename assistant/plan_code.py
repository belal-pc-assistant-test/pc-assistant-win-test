"""Plan codes and fingerprints.

Every pending plan gets

* a short random **code** (6 characters, unambiguous alphabet) that the user can
  type as ``approve CODE`` / ``deny CODE``, and
* a **fingerprint**: the first 8 hex characters of a SHA-256 over the exact
  steps of the plan, so a code can never be silently reused for a different
  plan.

Both are derived at plan-creation time; re-planning (or editing) creates a new
pair, and a code is single use.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from .planner import Plan, Step

#: No 0/O, 1/I/L — codes get typed by humans on phones.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
FINGERPRINT_LENGTH = 8


def generate_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def canonical_steps(steps: "Iterable[Step]") -> str:
    """Stable JSON representation of the steps (order matters, params sorted)."""
    payload = [
        {"action": step.action, "params": dict(sorted(step.params.items()))}
        for step in steps
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(steps: "Iterable[Step]") -> str:
    """First 8 hex chars of SHA-256 over the exact steps."""
    digest = hashlib.sha256(canonical_steps(steps).encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


def fingerprint_of_plan(plan: "Plan") -> str:
    return fingerprint(plan.steps)


def unique_code(taken: Iterable[str], attempts: int = 50) -> str:
    """A code that is not in ``taken``."""
    taken_set = {code.upper() for code in taken}
    for _ in range(attempts):
        code = generate_code()
        if code not in taken_set:
            return code
    # Astronomically unlikely (32^6 ≈ 1.07e9); widen instead of looping forever.
    return generate_code(CODE_LENGTH + 2)


def normalize_code(raw: str) -> str:
    """User input → canonical code (upper case, no spaces)."""
    return (raw or "").strip().upper().replace("-", "").replace(" ", "")
