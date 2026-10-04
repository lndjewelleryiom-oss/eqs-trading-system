from pathlib import Path
import argparse, json
from .validator import SemanticValidator

def main():
    ap=argparse.ArgumentParser("eqs-pc-validate")
    ap.add_argument("bundle")
    ap.add_argument("--schema-dir",default=str(Path(__file__).resolve().parents[1]/"schemas"))
    a=ap.parse_args(); data=json.loads(Path(a.bundle).read_text())
    report=SemanticValidator(a.schema_dir).validate_chain(**data)
    print(json.dumps(report,indent=2)); raise SystemExit(0 if report["overall_status"]=="PASS" else 2)
if __name__=="__main__": main()
