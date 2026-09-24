#!/bin/sh
# Patchline container entrypoint (§8 build artifacts):
#   1. acquire the migration lock (stale after 30 min, refreshed every 60s by
#      a background refresher while migrations run in the foreground — FR-057)
#   2. run `python -m patchline.cli migrate` (NOT raw alembic, so
#      ALLOW_DESTRUCTIVE_MIGRATIONS is respected — FR-029/FR-057)
#   3. release the lock
#   4. PRAGMA integrity_check (FR-058; refuses to start on failure)
#   5. GITHUB_APP_PRIVATE_KEY env → temp file with smart newline handling (FR-056)
#   6. start worker + scheduler in background (MANDATORY), web in foreground
set -eu

DATA_DIR="${PATCHLINE_DATA_DIR:-/data}"
LOCK_FILE="$DATA_DIR/.migration.lock"
STALE_SECONDS=1800

mkdir -p "$DATA_DIR"

# --- 5. private key env → temp file (smart newline handling) ----------------
if [ -n "${GITHUB_APP_PRIVATE_KEY:-}" ] && [ -z "${GITHUB_APP_PRIVATE_KEY_PATH:-}" ]; then
  KEY_FILE="$DATA_DIR/.github-app-key.pem"
  # Content with real newlines is written as-is; literal \n sequences are
  # converted only when needed (FR-056).
  if printf '%s' "$GITHUB_APP_PRIVATE_KEY" | grep -q '\\n'; then
    printf '%b' "$GITHUB_APP_PRIVATE_KEY" > "$KEY_FILE"
  else
    printf '%s\n' "$GITHUB_APP_PRIVATE_KEY" > "$KEY_FILE"
  fi
  chmod 600 "$KEY_FILE"
  export GITHUB_APP_PRIVATE_KEY_PATH="$KEY_FILE"
fi

# --- 1. migration lock with stale-holder recovery ----------------------------
acquire_lock() {
  while :; do
    if [ -f "$LOCK_FILE" ]; then
      LOCK_AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || stat -f %m "$LOCK_FILE") ))
      if [ "$LOCK_AGE" -gt "$STALE_SECONDS" ]; then
        echo "entrypoint: removing stale migration lock (age ${LOCK_AGE}s)"
        rm -f "$LOCK_FILE"
      else
        echo "entrypoint: waiting for migration lock holder"
        sleep 2
        continue
      fi
    fi
    if ( set -o noclobber; echo "$$" > "$LOCK_FILE" ) 2>/dev/null; then
      break
    fi
    sleep 1
  done
}

release_lock() {
  rm -f "$LOCK_FILE"
}

# Refresher: keep the lock timestamp fresh every 60s while migrations run.
refresh_lock() {
  while [ -f "$LOCK_FILE" ]; do
    touch "$LOCK_FILE" 2>/dev/null || true
    sleep 60
  done
}

acquire_lock
refresh_lock &
REFRESHER_PID=$$

# --- 2. migrations via the CLI (destructive gate respected) ------------------
if ! python -m patchline.cli migrate; then
  echo "entrypoint: migrations failed" >&2
  kill "$REFRESHER_PID" 2>/dev/null || true
  release_lock
  exit 1
fi

# --- 3. release --------------------------------------------------------------
kill "$REFRESHER_PID" 2>/dev/null || true
release_lock

# --- 4. post-restore integrity check (FR-058) --------------------------------
DB_PATH=$(python - <<'EOF'
import os
url = os.environ.get("DATABASE_URL", "sqlite:///./patchline.db")
print(url.replace("sqlite:///", ""))
EOF
)
if [ -f "$DB_PATH" ]; then
  RESULT=$(python - "$DB_PATH" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("PRAGMA integrity_check").fetchone()[0])
conn.close()
EOF
)
  if [ "$RESULT" != "ok" ]; then
    echo "entrypoint: PRAGMA integrity_check failed ($RESULT); refusing to start" >&2
    exit 1
  fi
fi

# --- 6. worker (background, supervised) + web (foreground) -------------------
(
  while :; do
    python -m patchline.worker || true
    echo "entrypoint: worker exited; restarting in 5s" >&2
    sleep 5
  done
) &
WORKER_PID=$$

term() {
  kill "$WORKER_PID" 2>/dev/null || true
  exit 0
}
trap term TERM INT

exec python -m uvicorn patchline.app:app --host 0.0.0.0 --port "${PORT:-8000}"
