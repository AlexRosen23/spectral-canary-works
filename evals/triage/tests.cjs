// Gold test split (150 cases) from the committed, generator-checked file. Labels are rule-derived, not human.
const fs = require("fs");
const path = require("path");

const SPLIT = process.env.SCW_TRIAGE_SPLIT || "test";
const rows = fs
  .readFileSync(path.join(__dirname, "triage_gold_v1.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((l) => JSON.parse(l))
  .filter((c) => c.split === SPLIT);

module.exports = rows.map((c) => ({
  description: `${c.case_id} ${c.expected.reason_code}${c.expected.escalate ? " escalate" : ""}${c.segments.length ? ` [${c.segments.join(",")}]` : ""}`,
  vars: {
    summary: c.summary,
    expected_reason: c.expected.reason_code,
    expected_escalate: c.expected.escalate,
    case_id: c.case_id,
  },
  metadata: { case_id: c.case_id, segments: c.segments.join(",") || "none", smoke: c.smoke },
}));
