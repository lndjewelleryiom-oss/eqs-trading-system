import json
import os
import shutil
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
GUARD_COMMAND = "quant_system.ci.test_data_evidence_guard"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHON_BIN": shutil.which("python") or "python"},
    )


def _build_git_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "quant_system" / "ci").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / ".githooks").mkdir()

    shutil.copy2(
        ROOT / "src" / "quant_system" / "ci" / "test_data_evidence_guard.py",
        repo / "src" / "quant_system" / "ci" / "test_data_evidence_guard.py",
    )
    (repo / "src" / "quant_system" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "quant_system" / "ci" / "__init__.py").write_text("", encoding="utf-8")
    for hook_name in ("pre-commit", "pre-push"):
        source = ROOT / ".githooks" / hook_name
        target = repo / ".githooks" / hook_name
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | stat.S_IXUSR)

    (repo / "docs" / "F7_EXTERNAL_GATE_APPROVAL_EXAMPLE_TEST_DATA.md").write_text(
        "TEST_DATA: true\nAUTHORIZATION_EFFECT: NONE\n",
        encoding="utf-8",
    )
    (repo / "tracker.json").write_text(
        json.dumps({"objective_evidence_recorded": []}),
        encoding="utf-8",
    )
    (repo / "docs" / "TRACKER.md").write_text("# Tracker\n", encoding="utf-8")

    _git(repo, "init")
    _git(repo, "config", "user.email", "eqs-test@example.invalid")
    _git(repo, "config", "user.name", "EQS Test")
    _git(repo, "config", "core.hooksPath", ".githooks")
    _git(repo, "add", ".")
    return repo


def test_versioned_local_hooks_run_same_guard_as_ci():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert GUARD_COMMAND in workflow
    for hook_name in ("pre-commit", "pre-push"):
        hook = ROOT / ".githooks" / hook_name
        text = hook.read_text(encoding="utf-8")
        assert hook.exists()
        if os.name != "nt":
            assert hook.stat().st_mode & stat.S_IXUSR
        assert GUARD_COMMAND in text


def test_installer_targets_both_versioned_git_hooks():
    text = (ROOT / "scripts" / "install_git_hooks.py").read_text(encoding="utf-8")
    assert 'HOOKS_PATH = ".githooks"' in text
    assert 'REQUIRED_HOOKS = ("pre-commit", "pre-push")' in text
    assert '"config", "core.hooksPath", HOOKS_PATH' in text


def test_installer_activates_and_repairs_executable_bits(tmp_path: Path):
    repo = tmp_path / "install-repo"
    (repo / ".githooks").mkdir(parents=True)
    (repo / "scripts").mkdir()
    for hook_name in ("pre-commit", "pre-push"):
        target = repo / ".githooks" / hook_name
        shutil.copy2(ROOT / ".githooks" / hook_name, target)
        target.chmod(target.stat().st_mode & ~stat.S_IXUSR)
    shutil.copy2(
        ROOT / "scripts" / "install_git_hooks.py",
        repo / "scripts" / "install_git_hooks.py",
    )
    _git(repo, "init")
    install = subprocess.run(
        [shutil.which("python") or "python", "scripts/install_git_hooks.py"],
        cwd=repo,
        check=False,
        text=True,
        capture_output=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert _git(repo, "config", "--get", "core.hooksPath").stdout.strip() == ".githooks"
    for hook_name in ("pre-commit", "pre-push"):
        hook = repo / ".githooks" / hook_name
        assert hook.is_file()
        if os.name != "nt":
            assert hook.stat().st_mode & stat.S_IXUSR


def test_clean_repository_commit_is_allowed(tmp_path: Path):
    repo = _build_git_fixture(tmp_path)
    result = _git(repo, "commit", "-m", "clean fixture", check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TEST_DATA LIVE-EVIDENCE GUARD: PASS" in result.stdout + result.stderr


def test_contaminated_live_evidence_blocks_commit(tmp_path: Path):
    repo = _build_git_fixture(tmp_path)
    first = _git(repo, "commit", "-m", "clean fixture", check=False)
    assert first.returncode == 0, first.stdout + first.stderr

    example = "docs/F7_EXTERNAL_GATE_APPROVAL_EXAMPLE_TEST_DATA.md"
    (repo / "tracker.json").write_text(
        json.dumps({"objective_evidence_recorded": [example]}),
        encoding="utf-8",
    )
    _git(repo, "add", "tracker.json")
    blocked = _git(repo, "commit", "-m", "must be blocked", check=False)
    output = blocked.stdout + blocked.stderr
    assert blocked.returncode != 0
    assert "TEST_DATA LIVE-EVIDENCE GUARD: FAIL" in output
    assert "objective_evidence_recorded" in output

    count = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    assert count == "1"


def test_clean_repository_push_is_allowed(tmp_path: Path):
    repo = _build_git_fixture(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, text=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    commit = _git(repo, "commit", "-m", "clean fixture", check=False)
    assert commit.returncode == 0, commit.stdout + commit.stderr

    pushed = _git(repo, "push", "origin", "HEAD:refs/heads/main", check=False)
    output = pushed.stdout + pushed.stderr
    assert pushed.returncode == 0, output
    assert "TEST_DATA LIVE-EVIDENCE GUARD: PASS" in output

    local_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert remote_head == local_head


def test_contaminated_branch_blocks_push_before_remote_advances(tmp_path: Path):
    repo = _build_git_fixture(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, text=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))

    first = _git(repo, "commit", "-m", "clean fixture", check=False)
    assert first.returncode == 0, first.stdout + first.stderr
    first_push = _git(repo, "push", "origin", "HEAD:refs/heads/main", check=False)
    assert first_push.returncode == 0, first_push.stdout + first_push.stderr
    remote_before = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    example = "docs/F7_EXTERNAL_GATE_APPROVAL_EXAMPLE_TEST_DATA.md"
    (repo / "tracker.json").write_text(
        json.dumps({"objective_evidence_recorded": [example]}),
        encoding="utf-8",
    )
    _git(repo, "add", "tracker.json")
    bypassed = _git(repo, "commit", "--no-verify", "-m", "contamination bypass fixture", check=False)
    assert bypassed.returncode == 0, bypassed.stdout + bypassed.stderr

    blocked = _git(repo, "push", "origin", "HEAD:refs/heads/main", check=False)
    output = blocked.stdout + blocked.stderr
    assert blocked.returncode != 0
    assert "TEST_DATA LIVE-EVIDENCE GUARD: FAIL" in output
    assert "objective_evidence_recorded" in output

    remote_after = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert remote_after == remote_before
