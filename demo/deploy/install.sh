#!/bin/bash
# Install or update the public demo (generated data only) from this app folder:
#   sudo demo/deploy/install.sh [--origin https://demo.example.org] [--no-generate]
# Code and venv go to /opt/demos/speedtest (root-owned: the service can't change what it runs), data to
# /srv/demos/speedtest (the unprivileged `demo` user, created if missing). Units: demo-speedtest.service (web app on
# 127.0.0.1:8921), demo-speedtest-tick.timer (a minute's worth of generated results each minute),
# demo-speedtest-prebuild.timer (03:40) and the 04:00 reset (through demo-reset-setup when the host has it).
# It never touches the real instance, its data or its agent. Re-run after updating the app (the data is kept).
set -euo pipefail
ORIGIN=""
GENERATE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --origin) ORIGIN="$2"; shift 2 ;;
    --no-generate) GENERATE=0; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/../.." && pwd)
OPT=/opt/demos/speedtest
DATA=/srv/demos/speedtest
UNITS="demo-speedtest.service demo-speedtest-tick.service demo-speedtest-tick.timer demo-speedtest-prebuild.service
       demo-speedtest-prebuild.timer"

id demo >/dev/null 2>&1 || useradd --system --home-dir /srv/demos --shell /usr/sbin/nologin --user-group demo
[ -d /srv/demos ] || install -d -o root -g root -m 0755 /srv/demos
install -d -o demo -g demo -m 0750 "$DATA"
install -d -o root -g root -m 0755 /opt/demos "$OPT" "$OPT/app"

# code: only what the demo runs (no tests, tools, docs, data or keys)
rsync -rt --delete --chmod=D755,F644 --exclude __pycache__ --exclude tests --exclude deploy --exclude '*.md' \
  --exclude .git --exclude .gitignore "$SRC/speedmon" "$SRC/speedagent" "$SRC/demo" "$SRC/requirements.txt" "$OPT/app/"
chown -R root:root "$OPT/app"
[ -x "$OPT/venv/bin/python" ] || python3 -m venv "$OPT/venv"
"$OPT/venv/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check -r "$OPT/app/requirements.txt"
chown -R root:root "$OPT/venv"
if [ -n "$ORIGIN" ] || [ ! -f "$OPT/demo.env" ]; then
  printf 'SPEEDMON_PUBLIC_ORIGINS=%s\n' "${ORIGIN:-http://127.0.0.1:8921}" > "$OPT/demo.env"
  chmod 0644 "$OPT/demo.env"
fi
install -d -o root -g root -m 0755 /usr/local/lib/demos /usr/local/lib/demos/speedtest
install -o root -g root -m 0755 "$SRC/demo/deploy/reset" /usr/local/lib/demos/speedtest/reset
for u in $UNITS; do
  install -o root -g root -m 0644 "$SRC/demo/deploy/$u" /etc/systemd/system/$u
done
systemctl daemon-reload

if [ "$GENERATE" = 1 ] && [ ! -f "$DATA/speedmon.sqlite3" ]; then
  echo "building three years of generated data (about seven minutes)..."
  ( cd "$OPT/app" && sudo -u demo SPEEDMON_DATA_DIR="$DATA" "$OPT/venv/bin/python" -m demo.generate --verify )
fi

# nightly reset at 04:00: stop the app and the updater, swap in the pre-built data, start them again
if command -v demo-reset-setup >/dev/null; then
  demo-reset-setup speedtest demo-speedtest.service demo-speedtest-tick.service >/dev/null
else
  cat > /etc/systemd/system/demo-speedtest-reset.service <<EOF
[Unit]
Description=Reset the Line Monitor demo

[Service]
Type=oneshot
User=demo
Group=demo
Environment=DEMO_NAME=speedtest DEMO_DIR=$DATA
WorkingDirectory=$DATA
ReadWritePaths=$DATA
ExecStartPre=+/bin/systemctl stop demo-speedtest.service demo-speedtest-tick.service
ExecStart=/usr/local/lib/demos/speedtest/reset
ExecStopPost=+/bin/systemctl start demo-speedtest.service demo-speedtest-tick.service
TimeoutStartSec=20min
Nice=10
EOF
  printf '[Unit]\nDescription=Reset the Line Monitor demo nightly\n\n[Timer]\nOnCalendar=*-*-* 04:00:00 Europe/London\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n' \
    > /etc/systemd/system/demo-speedtest-reset.timer
  systemctl daemon-reload
  systemctl enable --now demo-speedtest-reset.timer
fi
systemctl enable --now demo-speedtest.service demo-speedtest-tick.timer demo-speedtest-prebuild.timer
systemctl restart demo-speedtest.service
sleep 3
systemctl is-active demo-speedtest.service demo-speedtest-tick.timer demo-speedtest-prebuild.timer demo-speedtest-reset.timer
