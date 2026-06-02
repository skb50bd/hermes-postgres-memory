#!/usr/bin/env bash
# install.sh — install the postgres memory plugin into a Hermes Agent checkout.
#
# Usage:
#   ./install.sh                       # install into ~/.hermes/hermes-agent (default)
#   HERMES_HOME=/path/to/hermes ./install.sh
#
# Re-running is safe: each step is idempotent and only overwrites if the
# destination file already exists from a previous install.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"

PLUGIN_SRC="$SCRIPT_DIR/plugins/memory/postgres"
PLUGIN_DST="$HERMES_HOME/plugins/memory/postgres"

SKILL_SRC="$SCRIPT_DIR/skills/devops/hermes-postgres-memory"
SKILL_DST="$HERMES_HOME/skills/devops/hermes-postgres-memory"

if [ ! -d "$HERMES_HOME" ]; then
    echo "ERROR: HERMES_HOME does not exist: $HERMES_HOME" >&2
    echo "Set HERMES_HOME to your hermes-agent checkout." >&2
    exit 1
fi

if [ ! -d "$HERMES_HOME/plugins/memory" ]; then
    echo "ERROR: $HERMES_HOME/plugins/memory does not exist." >&2
    echo "Is $HERMES_HOME a Hermes Agent checkout?" >&2
    exit 1
fi

echo "→ Installing plugin to $PLUGIN_DST"
mkdir -p "$PLUGIN_DST"
cp -R "$PLUGIN_SRC/." "$PLUGIN_DST/"

echo "→ Installing skill to $SKILL_DST"
mkdir -p "$SKILL_DST"
cp -R "$SKILL_SRC/." "$SKILL_DST/"

echo
echo "✓ Installed. Next steps:"
echo "  1. Edit ~/.hermes/.env — add POSTGRES_* and HERMES_EMBED_* vars (see README.md)."
echo "  2. Edit ~/.hermes/config.yaml — set memory.provider: postgres."
echo "  3. Run: hermes postgres-memory preflight"
echo "  4. If preflight says 'must transfer ownership', run migration 000 as a superuser."
echo "  5. Run migrations 001..003, then 'hermes postgres-memory backfill'."
echo "  6. Verify: python $SKILL_DST/scripts/verify_embeddings.py"
