// Chat messages for the triage bundle under test. Source of truth is the bundle TOML + prompt file in
// configs/llm/ (tests/test_llm_lane.py::test_promptfoo_config_matches_bundle keeps this file in sync).
const fs = require("fs");
const path = require("path");

const BUNDLE_PROMPT = process.env.SCW_TRIAGE_PROMPT || "triage-p2";
const system = fs.readFileSync(path.join(__dirname, "..", "..", "configs", "llm", "prompts", `${BUNDLE_PROMPT}.md`), "utf8");

module.exports = async function ({ vars }) {
  return [
    { role: "system", content: system },
    { role: "user", content: `Fraud alert:\n${vars.summary}` },
  ];
};
