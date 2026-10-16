#!/bin/sh
# Copy the code to the Pi, run the tests there, restart both services, then run the Pi's post-deploy hooks.
#   scripts/deploy.sh ["what changed" ...]      (PI and APP can be set in the environment)
# Never copies var/ (data, keys, backups), var-dev/ or the venv. Hooks: every executable in
# /usr/local/etc/speedmon/post-deploy.d/ on the Pi, run in name order with the app's folder as the argument and
# the "what changed" lines on stdin; a failing hook is reported but doesn't undo the deploy.
set -eu
PI=${PI:-pi}
APP=${APP:-/home/pi/line-monitor}
cd "$(dirname "$0")/.."

rsync -rtp --exclude .venv --exclude var --exclude var-dev --exclude __pycache__ --exclude .pytest_cache \
  --exclude .DS_Store --exclude .git ./ "$PI:$APP/"
ssh "$PI" "cd '$APP' && .venv/bin/python -m pytest -q -p no:warnings && sudo systemctl restart speedmon speedagent \
  && sleep 3 && systemctl is-active speedmon speedagent"
printf '%s\n' "$@" | ssh "$PI" "notes=\$(cat); for h in /usr/local/etc/speedmon/post-deploy.d/*; do [ -x \"\$h\" ] || continue; \
  echo \"hook: \$h\"; printf '%s\n' \"\$notes\" | \"\$h\" '$APP' || echo \"hook failed: \$h\"; done"
