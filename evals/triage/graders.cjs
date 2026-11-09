// Code graders, the same rules as src/spectral_canary/llm/graders.py (no model grades a model).
const CODES = ["CARD_TESTING", "ACCOUNT_TAKEOVER", "STOLEN_CARD_CNP", "FRIENDLY_FRAUD", "BENIGN_ANOMALY"];

function parse(output) {
  try {
    const o = typeof output === "string" ? JSON.parse(output) : output;
    return o && typeof o === "object" && !Array.isArray(o) ? o : null;
  } catch {
    return null;
  }
}

function problems(o) {
  if (!o) return ["unparseable"];
  const p = [];
  const extra = Object.keys(o).filter((k) => !["reason_code", "escalate", "rationale"].includes(k));
  if (extra.length) p.push(`extra_keys:${extra.join(",")}`);
  if (!CODES.includes(o.reason_code)) p.push("reason_code_not_in_enum");
  if (typeof o.escalate !== "boolean") p.push("escalate_not_boolean");
  if (typeof o.rationale !== "string" || !o.rationale.trim()) p.push("rationale_missing");
  else if (o.rationale.length > 300) p.push("rationale_too_long");
  return p;
}

module.exports.schemaValid = (output) => {
  const p = problems(parse(output));
  return { pass: p.length === 0, score: p.length === 0 ? 1 : 0, reason: p.length ? p.join("; ") : "valid output contract" };
};

module.exports.escalateCorrect = (output, context) => {
  const o = parse(output);
  const want = context.vars.expected_escalate === true || context.vars.expected_escalate === "true";
  const ok = !!o && o.escalate === want;
  return { pass: ok, score: ok ? 1 : 0, reason: `escalate expected ${want}, got ${o ? o.escalate : "unparseable"}` };
};

// Reported, never gating: always passes, scores 1/0 so promptfoo's named metric `reason_correct` is the accuracy.
module.exports.reasonCorrect = (output, context) => {
  const o = parse(output);
  const ok = !!o && o.reason_code === context.vars.expected_reason;
  return { pass: true, score: ok ? 1 : 0, reason: `reason expected ${context.vars.expected_reason}, got ${o ? o.reason_code : "unparseable"} (not gating)` };
};
