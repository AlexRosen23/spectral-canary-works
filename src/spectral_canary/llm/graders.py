"""Code graders for triage outputs. No model grades another model here: every check is deterministic.

- ``schema``: the reply parses as one JSON object with exactly ``reason_code`` (one of the five codes),
  ``escalate`` (a JSON boolean) and ``rationale`` (a non-empty string of at most 300 characters).
- ``reason``: reason_code equals the rule-derived label.
- ``escalate``: escalate equals the rule-derived label.
- ``correct``: all three. pass^k over k tries requires ``correct`` on every try.
- ``one_sentence``: the rationale has at most one sentence-ending mark followed by more text (reported, not gated).
"""

from __future__ import annotations

import json
import re
from typing import Any

from .alerts import REASON_CODES

RATIONALE_MAX_CHARS = 300
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason_code": {"type": "string", "enum": list(REASON_CODES)},
        "escalate": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["reason_code", "escalate", "rationale"],
}
_SENTENCE_BREAK = re.compile(r"[.!?](\s+)(?=[A-Z0-9$])")


def parse_output(text: str | None) -> tuple[dict[str, Any] | None, str | None]:
    if not text or not text.strip():
        return None, "empty"
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"not_json: {exc.msg}"
    if not isinstance(obj, dict):
        return None, "not_an_object"
    return obj, None


def schema_problems(obj: dict[str, Any] | None) -> list[str]:
    if obj is None:
        return ["unparseable"]
    p = []
    extra = set(obj) - {"reason_code", "escalate", "rationale"}
    if extra:
        p.append(f"extra_keys:{sorted(extra)}")
    if obj.get("reason_code") not in REASON_CODES:
        p.append("reason_code_not_in_enum")
    if not isinstance(obj.get("escalate"), bool):
        p.append("escalate_not_boolean")
    r = obj.get("rationale")
    if not isinstance(r, str) or not r.strip():
        p.append("rationale_missing")
    elif len(r) > RATIONALE_MAX_CHARS:
        p.append("rationale_too_long")
    return p


def one_sentence(text: str | None) -> bool:
    if not text:
        return False
    return len(_SENTENCE_BREAK.findall(text.strip())) == 0


def grade(text: str | None, expected: dict[str, Any] | None) -> dict[str, Any]:
    """Grade one raw reply. ``expected`` may be None (label not yet known): only the schema is graded."""
    obj, perr = parse_output(text)
    problems = schema_problems(obj) if perr is None else [perr]
    valid = not problems
    out: dict[str, Any] = {
        "valid": valid,
        "problems": problems,
        "reason_code": obj.get("reason_code") if obj else None,
        "escalate": obj.get("escalate") if obj and isinstance(obj.get("escalate"), bool) else None,
        "one_sentence": one_sentence(obj.get("rationale") if obj and isinstance(obj.get("rationale"), str) else None),
    }
    if expected is not None:
        out["reason_ok"] = valid and out["reason_code"] == expected["reason_code"]
        out["escalate_ok"] = valid and out["escalate"] == expected["escalate"]
        out["correct"] = bool(out["reason_ok"] and out["escalate_ok"])
    return out
