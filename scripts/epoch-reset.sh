#!/bin/bash
# The planned "fresh start". Run as root every 5 minutes by speedmon-epoch-reset.timer; does nothing unless the
# "Fresh start at" setting is due (set in Settings, or "Start fresh now"). The fresh database doesn't inherit that
# setting, so each fresh start happens once.
# Order matters: the agent stops first (it finishes a running test and flushes its queue), anything still queued is
# drained, then the backend stops, the database is archived (verified copy, nothing deleted) and a fresh one created.
# On any failure before the archive step, services are simply restarted and nothing changes.
set -uo pipefail
# Defaults are production; the overrides exist so the whole sequence can be rehearsed on throwaway instances.
APP=${APP:-/home/pi/line-monitor}
DATA_DIR=${DATA_DIR:-$APP/var}
AGENT_UNIT=${AGENT_UNIT:-speedagent}
BACKEND_UNIT=${BACKEND_UNIT:-speedmon}
AGENT_STATE=${AGENT_STATE:-/var/lib/speedagent}
AGENT_KEY=${AGENT_KEY:-/etc/speedmon/agent.key}
AGENT_ENV=${AGENT_ENV:-production}
BACKEND_URL=${BACKEND_URL:-http://127.0.0.1:8772/speedtest/agent/v1}
MON_ENV=${MON_ENV:-production}
FAILED=${FAILED:-/var/lib/speedmon-reset/last-failure}
log() { echo "epoch-reset: $*"; }
restart() { systemctl start "$BACKEND_UNIT"; systemctl start "$AGENT_UNIT"; }
fail() { mkdir -p "$(dirname $FAILED)"; date +%s > "$FAILED"; log "$1; will retry in an hour"; restart; exit 1; }
# after a failure, wait an hour before trying again (never bounce the services every 5 minutes)
if [ -f "$FAILED" ] && [ $(( $(date +%s) - $(cat "$FAILED") )) -lt 3600 ]; then exit 0; fi
cd "$APP" || { log "app folder $APP missing"; exit 1; }
DUE=$(runuser -u speedmon -- env SPEEDMON_ENV=$MON_ENV SPEEDMON_DATA_DIR="$DATA_DIR" "$APP/.venv/bin/python" -m speedmon.cli fresh-start-due 2>&1)
RC=$?
[ $RC = 3 ] && exit 0                                       # not due: the normal case, every 5 minutes
if [ $RC != 0 ]; then log "could not check whether a fresh start is due (exit $RC): $DUE"; exit 1; fi
log "$DUE"

log "stopping the agent (waits for any running test)"
systemctl stop "$AGENT_UNIT"
log "draining the agent's queue"
DRAIN_OK=0
for i in 1 2 3 4 5; do
  if (cd "$APP" && SPEEDAGENT_ENV=$AGENT_ENV SPEEDAGENT_ID=${AGENT_ID:-pi} SPEEDAGENT_STATE_DIR="$AGENT_STATE" SPEEDAGENT_KEY_FILE="$AGENT_KEY" \
     SPEEDAGENT_BACKEND="$BACKEND_URL" "$APP/.venv/bin/python" -m speedagent.cli drain); then DRAIN_OK=1; break; fi
  sleep 10
done
chown -R speedagent:speedagent "$AGENT_STATE"
if [ $DRAIN_OK != 1 ]; then fail "could not drain the agent's queue; nothing changed"; fi

log "stopping the backend"
systemctl stop "$BACKEND_UNIT"
log "archiving and starting a fresh database"
if ! (cd "$APP" && runuser -u speedmon -- env SPEEDMON_ENV=$MON_ENV SPEEDMON_DATA_DIR="$DATA_DIR" "$APP/.venv/bin/python" -m speedmon.cli reset --yes); then
  fail "the reset failed; the database was not changed (see above)"
fi
log "clearing the agent's scheduling memory"
(cd "$APP" && runuser -u speedagent -- env SPEEDAGENT_ENV=$AGENT_ENV SPEEDAGENT_STATE_DIR="$AGENT_STATE" "$APP/.venv/bin/python" -m speedagent.cli reset-policy)
rm -f "$FAILED"
restart
log "done: fresh database live, agent running"
