import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "freeze_code_tree_v1.py"
spec = importlib.util.spec_from_file_location("freeze_code_tree_v1", SCRIPT)
freeze = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(freeze)

def make_tree(tmp_path):
    for name in freeze.INCLUDE_ROOTS:
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("a=1\n")
    (tmp_path / "tests" / "b.py").write_text("b=1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    return tmp_path

def digest(root):
    freeze.ROOT = root
    rows = freeze.build_rows()
    return freeze.tree_sha256(rows), rows

def test_deterministic_and_order_independent(tmp_path):
    root = make_tree(tmp_path)
    h1, rows = digest(root)
    h2, rows2 = digest(root)
    assert h1 == h2
    assert freeze.tree_sha256(list(reversed(rows))) == h1
    assert rows == rows2

def test_content_add_remove_and_path_change_affect_hash(tmp_path):
    root = make_tree(tmp_path)
    base, _ = digest(root)
    p = root / "src" / "a.py"
    p.write_text("a=2\n"); changed, _ = digest(root); assert changed != base
    p.write_text("a=1\n")
    q = root / "src" / "new.py"; q.write_text("x=1\n"); added, _ = digest(root); assert added != base
    q.unlink()
    p.rename(root / "src" / "renamed.py"); renamed, _ = digest(root); assert renamed != base

def test_generated_python_cache_is_ignored(tmp_path):
    root = make_tree(tmp_path)
    base, _ = digest(root)
    cache = root / "src" / "__pycache__"; cache.mkdir()
    (cache / "a.cpython.pyc").write_bytes(b"noise")
    (root / "src" / "loose.pyc").write_bytes(b"noise")
    assert digest(root)[0] == base

def test_freeze_verify_and_modified_tree_fails(tmp_path, monkeypatch):
    root = make_tree(tmp_path / "repo")
    out = tmp_path / "freeze"
    monkeypatch.setattr(freeze, "ROOT", root)
    monkeypatch.setattr(sys, "argv", ["freeze", "--out-dir", str(out)])
    freeze.main()
    monkeypatch.setattr(sys, "argv", ["freeze", "--out-dir", str(out), "--verify"])
    freeze.main()
    (root / "src" / "a.py").write_text("changed\n")
    with pytest.raises(SystemExit, match="differs"):
        freeze.main()

def test_production_source_has_no_legacy_env_fault_injection():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for p in (root / "src").rglob("*.py"):
        if "EQS_ATOMIC_CRASH_POINT" in p.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(p.relative_to(root).as_posix())
    assert offenders == []
