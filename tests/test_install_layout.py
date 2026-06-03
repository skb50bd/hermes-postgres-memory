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
        """The root manifest's name should match the repo's directory
        name (or the GitHub repo basename) — not a hardcoded string.
        If the repo is ever renamed, the manifest should be renamed
        too; this test catches drift by reading the actual repo name
        from disk rather than asserting a literal."""
        root = _load_yaml(ROOT_PLUGIN_YAML)
        # The repo identity is the directory name. This is what users
        # see in `hermes plugins list` and what shows up in GitHub URLs.
        repo_name = REPO_ROOT.name
        # The root manifest name should be either the repo name itself
        # or the inner-plugin's name with a prefix (e.g. 'pg-mem'
        # for a repo called 'pg-mem'). The simplest invariant: it
        # must be a non-empty string with no spaces or slashes, and
        # it should not be the inner plugin's name (which would
        # collide at runtime).
        inner = _load_yaml(INNER_PLUGIN_YAML)
        name = root["name"]
        assert name, "Root manifest name must be non-empty"
        assert " " not in name, f"Root name {name!r} must not contain spaces"
        assert "/" not in name, f"Root name {name!r} must not contain slashes"
        assert name != inner["name"], (
            f"Root name {name!r} collides with inner plugin name "
            f"{inner['name']!r}"
        )
        # Soft check: the repo name should appear somewhere in the
        # manifest name. This catches silent renames where the repo
        # moved but the manifest stayed the same.
        assert repo_name.replace("-", "") in name.replace("-", ""), (
            f"Root name {name!r} doesn't reflect the repo name "
            f"{repo_name!r}; if you renamed the repo, update the "
            f"manifest (or vice versa)"
        )

    def test_root_manifest_propagates_env_requirements(self):
        """The root manifest must declare the SAME set of requires_env as
        the inner plugin. The installer's `_missing_requires_env_names`
        check reads the root manifest (not the inner one) and warns the
        user before they hit runtime errors. A subset is acceptable
        ONLY if the inner is empty; otherwise the root must match
        exactly — anything less means the user is missing a
        pre-flight warning."""
        root = _load_yaml(ROOT_PLUGIN_YAML)
        inner = _load_yaml(INNER_PLUGIN_YAML)
        root_env = set(root.get("requires_env") or [])
        inner_env = set(inner.get("requires_env") or [])
        assert root_env == inner_env, (
            f"Root requires_env {sorted(root_env)!r} drifts from inner "
            f"{sorted(inner_env)!r}; the installer's pre-flight check "
            f"reads the root manifest, so a missing var there means a "
            f"missing warning to the user"
        )

    def test_root_manifest_propagates_pip_dependencies(self):
        """Same contract for pip_dependencies as for requires_env: the
        root must match the inner exactly, and the inner must declare
        at least one dep so a missing dep is caught."""
        root = _load_yaml(ROOT_PLUGIN_YAML)
        inner = _load_yaml(INNER_PLUGIN_YAML)
        root_deps = list(root.get("pip_dependencies") or [])
        inner_deps = list(inner.get("pip_dependencies") or [])
        assert root_deps == inner_deps, (
            f"Root pip_dependencies {root_deps!r} "
            f"drifts from inner {inner_deps!r}"
        )
        assert inner_deps, (
            "Inner manifest must declare at least one pip_dependencies "
            "entry; a plugin with no declared deps would silently import "
            "missing packages at runtime"
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

    def test_install_sh_targets_inner_plugin_path(self):
        """install.sh must continue to copy the inner
        plugins/memory/postgres/ directory — not the new root manifest.
        The root plugin.yaml is install-time metadata only; the runtime
        loads from the inner __init__.py. If install.sh ever starts
        reading the root manifest, it would diverge from the runtime
        discovery path."""
        script = (REPO_ROOT / "install.sh").read_text()
        assert "PLUGIN_SRC=" in script, "install.sh PLUGIN_SRC variable missing"
        assert "plugins/memory/postgres" in script, (
            "install.sh should reference the inner plugin path"
        )
        # install.sh must not depend on the root manifest — the install
        # path uses the inner plugin's own plugin.yaml, not the new
        # top-level one.
        assert "plugin.yaml" not in script, (
            "install.sh appears to read plugin.yaml; the inner plugin "
            "has its own plugin.yaml at plugins/memory/postgres/, and "
            "the new root manifest is for `hermes plugins install` only. "
            "install.sh should not depend on either."
        )
