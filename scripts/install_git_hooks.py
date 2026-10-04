from __future__ import annotations

import argparse
import os
import stat
import subprocess
from pathlib import Path

HOOKS_PATH = ".githooks"
REQUIRED_HOOKS = ("pre-commit", "pre-push")


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _ensure_required_hooks(repo_root: Path, *, make_executable: bool) -> list[Path]:
    hooks: list[Path] = []
    for name in REQUIRED_HOOKS:
        hook = repo_root / HOOKS_PATH / name
        if not hook.is_file():
            raise SystemExit(f"missing versioned {name} hook: {hook}")
        if make_executable:
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        # Windows does not expose POSIX executable mode bits reliably. Git for
        # Windows executes hooks via their shebang, so existence + hooksPath is
        # the enforceable local invariant there.
        if os.name != "nt" and not (hook.stat().st_mode & stat.S_IXUSR):
            raise SystemExit(f"versioned {name} hook is not executable: {hook}")
        hooks.append(hook)
    return hooks


def install(root: Path) -> None:
    repo_root = Path(_git("rev-parse", "--show-toplevel", cwd=root)).resolve()
    _ensure_required_hooks(repo_root, make_executable=True)
    _git("config", "core.hooksPath", HOOKS_PATH, cwd=repo_root)
    configured = _git("config", "--get", "core.hooksPath", cwd=repo_root)
    if configured != HOOKS_PATH:
        raise SystemExit(
            f"failed to configure core.hooksPath: expected {HOOKS_PATH!r}, got {configured!r}"
        )
    print(
        "Installed EQS git hooks: "
        f"core.hooksPath={configured}; hooks={','.join(REQUIRED_HOOKS)}"
    )


def check(root: Path) -> None:
    repo_root = Path(_git("rev-parse", "--show-toplevel", cwd=root)).resolve()
    configured = _git("config", "--get", "core.hooksPath", cwd=repo_root)
    try:
        _ensure_required_hooks(repo_root, make_executable=False)
    except SystemExit as exc:
        raise SystemExit(
            "EQS git safety hooks are not active; run: python scripts/install_git_hooks.py"
        ) from exc
    if configured != HOOKS_PATH:
        raise SystemExit(
            "EQS git safety hooks are not active; run: python scripts/install_git_hooks.py"
        )
    print("EQS pre-commit and pre-push TEST_DATA guards are active")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install or verify the EQS versioned pre-commit and pre-push hooks."
    )
    parser.add_argument("--check", action="store_true", help="verify hook activation only")
    args = parser.parse_args()
    root = Path.cwd()
    if args.check:
        check(root)
    else:
        install(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
