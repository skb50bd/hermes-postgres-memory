#!/usr/bin/env bash
# install.sh — install the postgres memory plugin into a Hermes Agent checkout.
#
# This is a thin wrapper around the per-file copy. Database role/database
# creation, pgvector install, and privileged grants are DBA prerequisites.
# bootstrap.sh verifies those prerequisites, writes local config, installs
# schema through PG_MEM_DB_CONN_STR, and then preflights the plugin. Use
# install.sh when prerequisites are already met and you just want files copied.
#
# Usage:
#   ./install.sh                                # install into ~/.hermes/hermes-agent
#   HERMES_HOME=/path/to/hermes ./install.sh    # custom location
#   ./install.sh --diagnose                     # run preflight only, no install
#   ./install.sh --yes                          # skip the "diagnose first" prompt
#
# Re-running is safe: each step is idempotent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"

# Auto-resolve HERMES_HOME: see plugins/memory/postgres/scripts/diagnose.sh
# for the full rationale. The agent runtime exports HERMES_HOME as the
# parent (/home/u/.hermes), but install.sh expects the checkout
# (/home/u/.hermes/hermes-agent). Resolve the mismatch once, here.
# In profile mode, HERMES_HOME is ~/.hermes/profiles/<name> and each
# profile has its own .env.
if [ -d "$HERMES_HOME" ]; then
    if [ ! -f "$HERMES_HOME/run_agent.py" ] && [ ! -f "$HERMES_HOME/AGENTS.md" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
        HERMES_HOME="$HERMES_HOME/hermes-agent"
    fi
fi
if [ ! -d "$HERMES_HOME" ] && [ -d "$HERMES_HOME/hermes-agent" ]; then
    HERMES_HOME="$HERMES_HOME/hermes-agent"
fi

PLUGIN_SRC="$SCRIPT_DIR/plugins/memory/postgres"
PLUGIN_DST="$HERMES_HOME/plugins/memory/postgres"

SKILL_SRC="$SCRIPT_DIR/skills/devops/hermes-postgres-memory"
SKILL_DST="$HERMES_HOME/skills/devops/hermes-postgres-memory"

DIAGNOSE_ONLY=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --diagnose)  DIAGNOSE_ONLY=1; shift ;;
        --yes|-y)    ASSUME_YES=1; shift ;;
        -h|--help)
            sed -n '2,/^set -euo pipefail/p' "$0" | head -n -1 | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# ─── preflight ──────────────────────────────────────────────────────────

if [ $DIAGNOSE_ONLY -eq 1 ]; then
    if [ -x "$PLUGIN_SRC/scripts/diagnose.sh" ]; then
        HERMES_HOME="$HERMES_HOME" "$PLUGIN_SRC/scripts/diagnose.sh"
        exit $?
    else
        echo "diagnose.sh not found at $PLUGIN_SRC/scripts/diagnose.sh" >&2
        exit 1
    fi
fi

# Detect a hermes-agent checkout
if [ ! -d "$HERMES_HOME" ]; then
    echo "ERROR: HERMES_HOME does not exist: $HERMES_HOME" >&2
    echo "Set HERMES_HOME to your hermes-agent checkout." >&2
    echo "Or re-run with --diagnose to see what's missing." >&2
    exit 1
fi

if [ ! -d "$HERMES_HOME/plugins/memory" ]; then
    echo "ERROR: $HERMES_HOME/plugins/memory does not exist." >&2
    echo "Is $HERMES_HOME a Hermes Agent checkout?" >&2
    exit 1
fi

# Run the diagnose script as a preflight (cheap, idempotent).
# It only reports; it doesn't modify anything. The user can skip with --yes.
if [ -x "$PLUGIN_SRC/scripts/diagnose.sh" ] && [ $ASSUME_YES -eq 0 ]; then
    echo "→ Running preflight diagnose (use --yes to skip)..."
    echo
    if ! HERMES_HOME="$HERMES_HOME" "$PLUGIN_SRC/scripts/diagnose.sh" --json > /tmp/hpm-diagnose.json 2>&1; then
        # The diagnose script may print a human report (it goes to stdout).
        # We re-run for the human output if the JSON ran.
        HERMES_HOME="$HERMES_HOME" "$PLUGIN_SRC/scripts/diagnose.sh" || true
        echo
        echo "  ↑ the preflight reported failures. the plugin will still be"
        echo "  installed, but it will not work until the failing checks pass."
        echo
        read -r -p "  continue with the install anyway? [y/N] " CONT
        if [[ ! "$CONT" =~ ^[Yy]$ ]]; then
            echo "  aborted. fix the issues above and re-run install.sh."
            exit 1
        fi
    else
        # Diagnose passed — show a brief summary
        HERMES_HOME="$HERMES_HOME" "$PLUGIN_SRC/scripts/diagnose.sh" 2>&1 | tail -n 5 || true
    fi
fi

# ─── copy plugin + skill ───────────────────────────────────────────────

echo
echo "→ Installing plugin to $PLUGIN_DST"
mkdir -p "$PLUGIN_DST"
cp -R "$PLUGIN_SRC/." "$PLUGIN_DST/"

echo "→ Installing skill to $SKILL_DST"
mkdir -p "$SKILL_DST"
cp -R "$SKILL_SRC/." "$SKILL_DST/"

# ─── done ───────────────────────────────────────────────────────────────

cat <<EOF

✓ Installed.

  Plugin: $PLUGIN_DST
  Skill:  $SKILL_DST

Next steps:

  1. Make sure ~/.hermes/.env has these (add if missing):
       PG_MEM_DB_CONN_STR='postgresql://hermes:***@10.0.0.1:5432/hermes'
       KIMI_API_KEY=***   # https://platform.moonshot.cn

  2. Make sure DBA prerequisites are complete before runtime use:
       - dedicated non-superuser role/database
       - pgvector installed in the target database
       - runtime role owns public schema and can create objects
     Then run the agent-side bootstrap/preflight:
       ./plugins/memory/postgres/scripts/bootstrap.sh
     (it verifies prerequisites and installs agent_memory schema; it does
     not use PostgreSQL superuser credentials.)

  3. Edit ~/.hermes/config.yaml:
       memory:
         memory_enabled: true
         provider: postgres

  4. Restart the gateway:
       hermes gateway restart

  5. Verify:
       hermes postgres-memory preflight
       hermes postgres-memory status

For first-time agent-side installs, bootstrap.sh is the recommended path after
DBA prerequisites are complete. This install.sh is for when you already have a
working database and just want the files dropped in.

EOF
