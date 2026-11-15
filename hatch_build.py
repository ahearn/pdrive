"""Hatch build hook: install the Node bridge's dependencies and bundle it when pdrive is installed.

Runs for wheel and editable builds (uv tool install, pip install). It needs npm on PATH. Without npm
it only warns and the install still succeeds; pdrive then says what to run when it first starts.
Set PDRIVE_SKIP_NPM=1 to skip this step.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

BRIDGE = Path("src/pdrive/bridge_js")


def _stale(target: Path, *sources: Path) -> bool:
    if not target.exists():
        return True
    t = target.stat().st_mtime
    return any(s.exists() and s.stat().st_mtime > t for s in sources)


def _warn(msg: str) -> None:
    print(f"pdrive build: warning: {msg}", file=sys.stderr)


class BridgeBuildHook(BuildHookInterface):
    PLUGIN_NAME = "pdrive-bridge"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel" or os.environ.get("PDRIVE_SKIP_NPM"):
            return
        bridge = Path(self.root) / BRIDGE
        npm = shutil.which("npm")
        if npm is None:
            _warn(f"npm not found; skipping the bridge build. Later run: npm ci --prefix {bridge} "
                  f"&& npm run build --prefix {bridge}")
            return
        lock, pkg = bridge / "package-lock.json", bridge / "package.json"
        marker = bridge / "node_modules" / ".package-lock.json"  # written by npm on every install
        try:
            if _stale(marker, lock, pkg):
                self._run([npm, "ci", "--no-audit", "--no-fund"], bridge)
            if _stale(bridge / "dist" / "bridge.mjs", bridge / "bridge.ts", lock):
                self._run([npm, "run", "build"], bridge)
        except subprocess.CalledProcessError as e:
            _warn(f"'{' '.join(e.cmd)}' failed (exit {e.returncode}); pdrive will fall back to tsx if possible")

    def _run(self, cmd: list[str], cwd: Path) -> None:
        print(f"pdrive build: {' '.join(Path(c).name if i == 0 else c for i, c in enumerate(cmd))}",
              file=sys.stderr)
        # npm writes progress to stdout; keep it off the build frontend's stdout.
        subprocess.run(cmd, cwd=cwd, check=True, stdout=sys.stderr)
