"""Tests for the install-layout contract.

The repo's actual memory provider lives at ``plugins/memory/postgres/``,
discovered by the runtime via ``plugins/memory/__init__.py`` →
``_iter_provider_dirs()``. The top-level ``plugin.yaml`` exists so
``hermes plugins install <repo>`` — which only inspects the repo root —
finds a valid manifest and emits a clean install.

This test file pins the contract:

  - Root ``plugin.yaml`` parses and exposes ``name``, ``version``,
    ``requires_env``, ``pip_dependencies``.
  - The root manifest's ``name`` does NOT collide with the inner
    plugin's ``name`` (``postgres``), so they can coexist in a single
    install (the inner one is what the runtime loads; the root one is
    install-time only).
  - The repo root does NOT ship a top-level ``__init__.py`` — the
    install path doesn't need it and adding one would imply a Python
    module that doesn't exist at runtime.

If any of these regress, ``hermes plugins install skb50bd/hermes-postgres-memory``
will fail to recognize the repo, falling back to the manual
``./install.sh`` recipe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_PLUGIN_YAML = REPO_ROOT / "plugin.yaml"
INNER_PLUGIN_YAML = REPO_ROOT / "plugins" / "memory" / "postgres" / "plugin.yaml"
ROOT_INIT_PY = REPO_ROOT / "__init__.py"


def _load_yaml(path: Path) -> dict:
    assert path.exists(), f"Expected {path} to exist"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class TestRootManifest:
    """The top-level plugin.yaml exists and is valid."""

    def test_root_manifest_parses(self):
        manifest = _load_yaml(ROOT_PLUGIN_YAML)
        assert manifest, "Root plugin.yaml is empty"

    def test_root_manifest_has_required_fields(self):
        manifest = _load_yaml(ROOT_PLUGIN_YAML)
        for key in ("name", "version", "description", "author"):
            assert key in manifest, f"Root manifest missing required field: {key!r}"

    def test_root_name_does_not_collide_with_inner(self):
        """If both manifest name and inner plugin name were 'postgres',
        `hermes plugins install` and the runtime discovery would race for
        the same key. The root manifest uses a distinct name so the
        two layers never collide."""
        root = _load_yaml(ROOT_PLUGIN_YAML)
        inner = _load_yaml(INNER_PLUGIN_YAML)
        assert root["name"] != inner["name"], (
            f"Root manifest name {root['name']!r} collides with inner "
            f"plugin name {inner['name']!r}; pick a different root name "
            f"(e.g. 'hermes-postgres-memory')"
        )

    def test_root_name_matches_repo(self):
        """The root manifest name should reflect the repo identity so
        users see something recognizable in `hermes plugins list`."""
        root = _load_yaml(ROOT_PLUGIN_YAML)
        assert root["name"] == "hermes-postgres-memory", (
            f"Unexpected root name {root['name']!r}; expected "
            f"'hermes-postgres-memory' to match the repo"
        )

    def test_root_manifest_propagates_env_requirements(self):
        """The inner plugin requires PG_MEM_DB_CONN_STR. The root
        manifest must declare the same so the installer's
        `_missing_requires_env_names` check (which reads the root
        manifest, not the inner one) warns the user before they hit
        runtime errors."""
        root = _load_yaml(ROOT_PLUGIN_YAML)
        assert "PG_MEM_DB_CONN_STR" in (root.get("requires_env") or []), (
            "Root manifest must declare requires_env: [PG_MEM_DB_CONN_STR] "
            "to match the inner plugin's runtime requirement"
        )

    def test_root_manifest_propagates_pip_dependencies(self):
        root = _load_yaml(ROOT_PLUGIN_YAML)
        inner = _load_yaml(INNER_PLUGIN_YAML)
        assert root.get("pip_dependencies") == inner.get("pip_dependencies"), (
            f"Root pip_dependencies {root.get('pip_dependencies')!r} "
            f"drifts from inner {inner.get('pip_dependencies')!r}"
        )

    def test_version_is_semver(self):
        manifest = _load_yaml(ROOT_PLUGIN_YAML)
        assert re.match(r"^\d+\.\d+\.\d+$", manifest["version"]), (
            f"Version {manifest['version']!r} is not semver (X.Y.Z)"
        )


class TestNoRootInit:
    """The repo does NOT ship a top-level __init__.py.

    Adding one would imply a Python module that the runtime never
    imports. The install path doesn't need it either — `_read_manifest`
    only reads plugin.yaml, and the runtime discovery imports the
    inner `plugins/memory/postgres/__init__.py` directly. Keeping the
    root module-free is the cleanest contract.
    """

    def test_no_root_init_py(self):
        assert not ROOT_INIT_PY.exists(), (
            f"{ROOT_INIT_PY} exists but should not. The install path "
            f"doesn't need it and the runtime never imports it. If you "
            f"need re-exports for the source tree, add them to a "
            f"documented shim — but the current contract is no shim."
        )


class TestInstallShContract:
    """The install.sh script remains the source of truth for greenfield
    installs. This test pins the relationship between install.sh and
    the new root manifest."""

    def test_install_sh_still_present(self):
        assert (REPO_ROOT / "install.sh").exists()

    def test_install_sh_unchanged(self):
        """install.sh has its own copy logic and doesn't depend on the
        root manifest. Pin that we haven't accidentally coupled it."""
        script = (REPO_ROOT / "install.sh").read_text()
        assert "PLUGIN_SRC=" in script, "install.sh PLUGIN_SRC variable missing"
        assert "plugins/memory/postgres" in script, (
            "install.sh should still reference the inner plugin path"
        )
        # install.sh should NOT try to read the root manifest
        assert "plugin.yaml" not in script or "PLUGIN_SRC" in script, (
            "install.sh appears to depend on plugin.yaml at the wrong path"
        )
