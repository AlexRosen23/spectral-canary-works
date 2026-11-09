#!/usr/bin/env bash
# fmt-check + validate both OpenTofu configs WITHOUT touching any cloud account.
# `tofu init -backend=false` only downloads providers; `validate` is offline. No plan/apply is run.
set -euo pipefail
cd "$(dirname "$0")/.."
TOFU_IMAGE="${TOFU_IMAGE:-ghcr.io/opentofu/opentofu:1.8.5}"
run() {
  if command -v tofu >/dev/null 2>&1; then (cd "$1" && shift && tofu "$@")
  else docker run --rm -v "$PWD/$1:/w" -w /w -e TF_PLUGIN_CACHE_DIR=/w/.terraform/plugin-cache "$TOFU_IMAGE" "${@:2}"; fi
}
for d in infra/local infra/aws; do
  echo "== $d"
  mkdir -p "$d/.terraform/plugin-cache"
  run "$d" fmt -check -diff
  run "$d" init -backend=false -input=false -no-color >/dev/null
  run "$d" validate -no-color
done
echo "iac-validate: both configs are formatted and valid (nothing was planned or applied)"
