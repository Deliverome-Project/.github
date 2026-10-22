#!/bin/bash
# SessionStart hook, Claude Code cloud sessions only: make sure uv is new enough for this repo.
# uv before 0.13.4 carries root certificates that trust the eMudhra intermediates compromised on
# 2026-10-19, and the repo's [tool.uv] required-version refuses to run on it. The cloud image
# ships an older uv, so without this every uv command in a cloud session fails.
# Locally (CLAUDE_CODE_REMOTE unset) it does nothing.
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0

MIN=@UV_MIN@
PIN=@UV_PIN@  # an exact release, so cloud sessions don't pick up a uv under 7 days old

have=$(uv --version 2>/dev/null | awk '{print $2}')
if [ -n "$have" ] && [ "$(printf '%s\n%s\n' "$MIN" "$have" | sort -V | head -1)" = "$MIN" ]; then
  exit 0  # already new enough
fi

if ! python3 -m pip install --quiet --disable-pip-version-check "uv==$PIN"; then
  echo "ensure-uv: could not install uv $PIN; uv commands will fail until it is upgraded" >&2
  exit 0
fi
bin=$(python3 -c 'import os, uv; print(os.path.dirname(uv.find_uv_bin()))') || exit 0
# Put the new uv ahead of the image's copy for the rest of the session.
[ -n "$CLAUDE_ENV_FILE" ] && echo "export PATH=\"$bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
echo "ensure-uv: using uv $("$bin/uv" --version | awk '{print $2}') (was ${have:-none})"
exit 0
