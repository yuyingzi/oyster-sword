#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
bash -n "$root/codex-deepseek"
output="$("$root/codex-deepseek" status)"
grep -q '^Codex: codex-cli ' <<<"$output"
grep -q '^Profile: ' <<<"$output"
grep -q '^API key: ' <<<"$output"

tmp_home="$(mktemp -d)"
trap 'rm -rf "$tmp_home"' EXIT
CODEX_HOME="$tmp_home" "$root/codex-deepseek" install >/dev/null
grep -q '^# managed-by: codex-deepseek-profile$' "$tmp_home/deepseek.config.toml"
grep -q '^model = "deepseek-v4-flash"$' "$tmp_home/deepseek.config.toml"
grep -q '^wire_api = "responses"$' "$tmp_home/deepseek.config.toml"
python3 - "$tmp_home/model-catalogs/deepseek.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    models = json.load(source)["models"]
assert [model["slug"] for model in models] == ["deepseek-v4-flash"]
serialized = json.dumps(models)
assert "You are Codex, an agent based on GPT-5." not in serialized
assert "You are Codex, running on DeepSeek-V4-Flash." in serialized
PY
CODEX_HOME="$tmp_home" "$root/codex-deepseek" install >/dev/null
grep -q '^model = "deepseek-v4-flash"$' "$tmp_home/deepseek.config.toml"
! grep -q 'You are Codex, an agent based on GPT-5.' "$tmp_home/model-catalogs/deepseek.json"
CODEX_HOME="$tmp_home" "$root/codex-deepseek" uninstall >/dev/null
[ ! -e "$tmp_home/deepseek.config.toml" ]
printf 'ok\n'
