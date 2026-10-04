from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "artifacts/commissioning/code_tree_freeze_v1"
INCLUDE_ROOTS = ("src", "tests", "scripts", ".github", "interface")
INCLUDE_FILES = ("pyproject.toml",)

def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def build_rows():
    files = []
    for name in INCLUDE_ROOTS:
        base = ROOT / name
        if base.exists():
            files.extend(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    files.extend(ROOT / name for name in INCLUDE_FILES if (ROOT / name).is_file())
    files = sorted(set(files), key=lambda p: p.relative_to(ROOT).as_posix())
    return [{"path": p.relative_to(ROOT).as_posix(), "sha256": file_sha256(p), "size": p.stat().st_size} for p in files]

def tree_sha256(rows) -> str:
    ordered = sorted(rows, key=lambda r: r["path"])
    canonical = "".join(f'{r["path"]} {r["sha256"]}\n' for r in ordered).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    out = Path(args.out_dir)
    if not out.is_absolute():
        out = ROOT / out
    manifest = out / "CODE_TREE_FREEZE.json"
    rows = build_rows()
    tree = tree_sha256(rows)
    if args.verify:
        if not manifest.is_file():
            raise SystemExit("BLOCKED: code-tree freeze manifest missing")
        frozen = json.loads(manifest.read_text(encoding="utf-8"))
        ok = frozen.get("tree_sha256") == tree and frozen.get("files") == rows
        print(json.dumps({"status": "PASS" if ok else "FAIL", "tree_sha256": tree, "file_count": len(rows)}, indent=2))
        if not ok:
            raise SystemExit("BLOCKED: code tree differs from frozen manifest")
        return
    out.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "eqs-code-tree-freeze-v1", "method": "SHA-256 over sorted relative-path/file-SHA rows", "tree_sha256": tree, "file_count": len(rows), "files": rows}
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "manifest": str(manifest), "manifest_sha256": file_sha256(manifest), "tree_sha256": tree, "file_count": len(rows)}, indent=2))

if __name__ == "__main__":
    main()
