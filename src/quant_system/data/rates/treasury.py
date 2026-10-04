from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import hashlib
import json

from .models import TreasuryCurveSnapshot


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_treasury_curve_history(
    normalized_path: str | Path,
    seal_path: str | Path,
) -> tuple[TreasuryCurveSnapshot, ...]:
    normalized_path=Path(normalized_path)
    seal_path=Path(seal_path)
    seal=json.loads(seal_path.read_text(encoding="utf-8"))
    if seal.get("result")!="PASS":
        raise ValueError("Treasury curve source seal is not PASS")
    if seal.get("normalized_sha256")!=_sha(normalized_path):
        raise ValueError("Treasury curve normalized hash does not match seal")
    doc=json.loads(normalized_path.read_text(encoding="utf-8"))
    known_from=datetime.fromisoformat(doc["retrieved_at"].replace("Z","+00:00"))
    raw_sha256=seal["raw_html_sha256"]
    snapshots=[]
    for row in doc["rows"]:
        curve={
            tenor:Decimal(str(value))*Decimal("100")
            for tenor,value in row["curve_percent"].items()
            if value is not None
        }
        snapshots.append(TreasuryCurveSnapshot(
            as_of_date=date.fromisoformat(row["date"]),
            known_from=known_from,
            curve_bps=curve,
            source=doc["source"],
            raw_sha256=raw_sha256,
        ))
    if not snapshots:
        raise ValueError("Treasury curve history is empty")
    if any(b.as_of_date<=a.as_of_date for a,b in zip(snapshots,snapshots[1:])):
        raise ValueError("Treasury curve dates must be strictly increasing")
    return tuple(snapshots)
