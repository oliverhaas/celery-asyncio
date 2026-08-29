"""Report the package versions each benchmark venv resolves to."""

# runner.py stamps the same dict into every result JSON. A JSON written before
# that landed falls back to probing here, which is equivalent: setup_venvs.sh
# is the only thing that ever rebuilds a venv, and it never runs mid-matrix.

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The fork ships under its own distribution names and installs editable, so
# both spellings are probed and the source tree's git revision is recorded
# alongside: 6.0.0a3 does not move between runs, but the commit does.
DISTRIBUTIONS = {
    "celery": ("celery-asyncio", "celery"),
    "kombu": ("kombu-asyncio", "kombu"),
    "redis": ("redis",),
    "uvloop": ("uvloop",),
}

_PROBE = """
import importlib, importlib.metadata as md, platform, sys, json
out = {"python": platform.python_version(), "implementation": platform.python_implementation()}
out["free_threading"] = not getattr(sys, "_is_gil_enabled", lambda: True)()
for key, names in %r.items():
    for name in names:
        try:
            out[key] = f"{name} {md.version(name)}"
        except md.PackageNotFoundError:
            continue
        try:
            out[key + "_path"] = importlib.import_module(key).__file__
        except ImportError:
            pass
        break
    else:
        out[key] = None
print(json.dumps(out))
"""


def venv_for(config: str) -> str:
    """Map a config label back to the venv `run_all.matrix()` ran it in."""
    suffix = "314t" if config.endswith("-314t") else "314"
    flavor = "classic" if config.startswith("classic-") else "async"
    return f".venv-{flavor}-{suffix}"


def _git_revision(path: str | None, venv_dir: Path) -> str | None:
    """Short HEAD of the worktree an editable install points into."""
    # A package vendored into the venv gets no revision: site-packages sits
    # inside this repo, so git would report the fork's own HEAD for upstream.
    if not path or Path(path).is_relative_to(venv_dir):
        return None
    proc = subprocess.run(
        ["git", "-C", str(Path(path).parent), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or None


def probe(venv: str) -> dict:
    python = ROOT / venv / "bin" / "python"
    if not python.exists():
        return {}
    proc = subprocess.run([str(python), "-c", _PROBE % (DISTRIBUTIONS,)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return {}
    out = json.loads(proc.stdout)
    for key in ("celery", "kombu"):
        rev = _git_revision(out.pop(key + "_path", None), python.parent.parent)
        if rev and out.get(key):
            out[key] = f"{out[key]} ({rev})"
    for key in list(out):
        if key.endswith("_path"):
            del out[key]
    return out


def broker_version() -> str | None:
    """The valkey/redis server the whole matrix published through."""
    proc = subprocess.run(
        ["redis-cli", "-h", "localhost", "-p", "6379", "INFO", "server"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    fields = dict(line.split(":", 1) for line in proc.stdout.splitlines() if ":" in line and not line.startswith("#"))
    for key in ("valkey_version", "redis_version"):
        if key in fields:
            label = "Valkey" if key.startswith("valkey") else "Redis"
            return f"{label} {fields[key].strip()}"
    return None


def versions_for(result: dict) -> dict:
    """Versions for one result JSON, stamped in by the runner or probed now."""
    return result.get("versions") or probe(venv_for(result["config"]))


if __name__ == "__main__":
    venvs = sys.argv[1:] or [f".venv-{f}-{s}" for f in ("async", "classic") for s in ("314", "314t")]
    print(json.dumps({v: probe(v) for v in venvs} | {"broker": broker_version()}, indent=2))
