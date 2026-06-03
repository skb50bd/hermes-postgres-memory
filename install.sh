#!/usr/bin/env bash
# install.sh — install the postgres memory plugin into a Hermes Agent checkout.
#
# This is a thin wrapper around the per-file copy. The real work — database
# creation, extension install, .env patching, config.yaml editing, plugin
# activation — is done by `bootstrap.sh` (interactive) or by hand. Use
# `install.sh` when you already have a working database and just want the
# files dropped in.
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
       POSTGRES_HOST=<host>
       POSTGRES_PORT=5432
       POSTGRES_USER=hermes
       POSTGRES_PASSWORD=***
       POSTGRES_DATABASE=hermes
       KIMI_API_KEY=***   # https://platform.moonshot.cn

  2. Make sure the database is set up. If you haven't done that yet,
     run the one-shot installer from the repo root:
       ./plugins/memory/postgres/scripts/bootstrap.sh
     (it creates the database, the role, the pgvector extension, and
     installs the agent_memory schema.)

  3. Edit ~/.hermes/config.yaml:
       memory:
         memory_enabled: true
         provider: postgres

  4. Restart the gateway:
       hermes gateway restart

  5. Verify:
       hermes postgres-memory preflight
       hermes postgres-memory status

For first-time installs, the bootstrap script is the recommended path.
This install.sh is for when you already have a working database and
just want the files dropped in.

EOF
