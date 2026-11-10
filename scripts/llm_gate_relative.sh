#!/usr/bin/env bash
# Relative LLM triage gate (`make llm-gate-relative`): run the promptfoo suite in evals/triage/ for a BASE checkout and
# for this checkout (HEAD) against the same Ollama server, back to back, then compare the pass rates with
# scripts/llm_gate_compare.py (fails when HEAD drops more than the release policy's margin below BASE).
#
# Environment:
#   LLM_GATE_BASE_DIR     an existing checkout of the base (CI checks out github.base_ref there). If unset, a
#                         temporary detached git worktree of LLM_GATE_BASE is created and removed afterwards.
#   LLM_GATE_BASE         git ref for that temporary worktree (default origin/main).
#   LLM_GATE_BASE_PROMPT  optional SCW_TRIAGE_PROMPT for the base run   (e.g. triage-p2)
#   LLM_GATE_HEAD_PROMPT  optional SCW_TRIAGE_PROMPT for the head run   (e.g. triage-p3 to see the gate fail)
#   PYTHON                interpreter command for the comparator (default python3; stdlib only, Python >= 3.9)
# Results land in evals/triage/results/relative/ (gitignored): base.json, head.json, verdict.json, junit files.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$root/evals/triage/results/relative"
read -r -a python <<<"${PYTHON:-python3}"  # may be a command with arguments, e.g. "uv run --frozen python"
mkdir -p "$out"
rm -f "$out"/*.json "$out"/*.xml

for p in "${LLM_GATE_BASE_PROMPT:-}" "${LLM_GATE_HEAD_PROMPT:-}"; do
  if [[ -n "$p" && ! "$p" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]]; then
    echo "llm-gate: prompt names are lowercase letters, digits and dashes (got '$p')" >&2
    exit 2
  fi
done

base_dir="${LLM_GATE_BASE_DIR:-}"
if [[ -z "$base_dir" ]]; then
  ref="${LLM_GATE_BASE:-origin/main}"
  tmp="$(mktemp -d)"
  base_dir="$tmp/base"
  git -C "$root" worktree add --quiet --detach "$base_dir" "$ref"
  # shellcheck disable=SC2064  # expand now: these paths are fixed for this run
  trap "git -C '$root' worktree remove --force '$base_dir' >/dev/null 2>&1 || true; rm -rf '$tmp'" EXIT
fi
base_dir="$(cd "$base_dir" && pwd)"
if [[ ! -f "$base_dir/evals/triage/promptfooconfig.yaml" ]]; then
  echo "llm-gate: base checkout $base_dir has no evals/triage/ suite to compare against" >&2
  exit 2
fi

describe() { git -C "$1" log -1 --format='%h %s' 2>/dev/null || echo "(not a git checkout)"; }
short() { git -C "$1" rev-parse --short HEAD 2>/dev/null || echo "?"; }
echo "llm-gate: base $(describe "$base_dir")  [$base_dir]"
echo "llm-gate: head $(describe "$root")  [$root]"
base_name="base $(short "$base_dir") ${LLM_GATE_BASE_PROMPT:-bundle default}"
head_name="head $(short "$root") ${LLM_GATE_HEAD_PROMPT:-bundle default}"

run_suite() { # <label> <checkout> <prompt-or-empty>
  local label="$1" dir="$2" prompt="$3" status=0
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then echo "::group::promptfoo eval: $label${prompt:+ (SCW_TRIAGE_PROMPT=$prompt)}"; fi
  (
    cd "$dir/evals/triage"
    npm ci --no-audit --no-fund --silent
    if [[ -n "$prompt" ]]; then export SCW_TRIAGE_PROMPT="$prompt"; fi
    # Threshold 0: promptfoo itself never fails on the pass rate here; the comparison decides. It still exits non-zero
    # on provider errors, which the comparator also rejects.
    PROMPTFOO_PASS_RATE_THRESHOLD=0 PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 \
      npx promptfoo eval -c promptfooconfig.yaml --no-cache --no-progress-bar \
      -o "$out/$label.json" -o "$out/$label.junit.xml"
  ) || status=$?
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then echo "::endgroup::"; fi
  echo "llm-gate: $label run exit code $status"
  if [[ ! -s "$out/$label.json" ]]; then
    echo "llm-gate: $label run produced no results" >&2
    exit 2
  fi
}

run_suite base "$base_dir" "${LLM_GATE_BASE_PROMPT:-}"
run_suite head "$root" "${LLM_GATE_HEAD_PROMPT:-}"

policy="$base_dir/configs/llm/release_policy.toml"
[[ -f "$policy" ]] || policy="$root/configs/llm/release_policy.toml"
"${python[@]}" "$root/scripts/llm_gate_compare.py" --base "$out/base.json" --head "$out/head.json" \
  --policy "$policy" --base-name "$base_name" --head-name "$head_name" --json-out "$out/verdict.json"
