from __future__ import annotations
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
SCHEMA_ID="EQS-FEASIBILITY-CRYPTO-CAMPAIGN-V2-3"
CAMPAIGN_ID="FEAS-BINANCE-BTC-FUNDING-003"
VERSION="2.3.0"
SOURCE_MANIFEST_SHA256="0a466c8ba272fddfcc1b92adbce3571818554f9b7af4122eef608a6deb71245f"
def canonical(value: object)->bytes:return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
def fingerprint(campaign:dict[str,Any])->str:return sha256(canonical(campaign)).hexdigest()
def validate_campaign3(record:dict[str,Any])->tuple[str,...]:
    failures=[]; campaign=record.get("campaign")
    if record.get("schema_id")!=SCHEMA_ID: failures.append("SCHEMA_ID_INVALID")
    if not isinstance(campaign,dict): return tuple(failures+["CAMPAIGN_REQUIRED"])
    if record.get("campaign_fingerprint")!=fingerprint(campaign): failures.append("FINGERPRINT_MISMATCH")
    if campaign.get("campaign_id")!=CAMPAIGN_ID: failures.append("CAMPAIGN_ID_INVALID")
    if campaign.get("version")!=VERSION: failures.append("VERSION_INVALID")
    if campaign.get("classification")!="EXPLORATORY_NON_EVIDENTIARY": failures.append("CLASSIFICATION_INVALID")
    source=campaign.get("source_contract",{})
    if source.get("shared_acquisition_manifest_file_sha256")!=SOURCE_MANIFEST_SHA256: failures.append("SOURCE_MANIFEST_BINDING_INVALID")
    if source.get("required_series")!=["KLINES_5M","FUNDING_RATE"]: failures.append("SOURCE_SERIES_INVALID")
    strategy=campaign.get("strategy",{})
    expected={"family":"FUNDING_PERSISTENCE_CONTRARIAN","absolute_funding_entry_threshold":"0.0005","holding_bars":288,"direction":"LONG_SHORT","quantity":"1","parameter_tuning_allowed":False}
    if strategy!=expected: failures.append("STRATEGY_CONTRACT_INVALID")
    try:
        if Decimal(strategy.get("absolute_funding_entry_threshold","0"))!=Decimal("0.0005"): failures.append("FUNDING_THRESHOLD_INVALID")
    except Exception: failures.append("FUNDING_THRESHOLD_INVALID")
    research=campaign.get("research_rules",{})
    for key in ("single_fixed_configuration","all_runs_counted"):
        if research.get(key) is not True: failures.append(f"RESEARCH_{key.upper()}_INVALID")
    for key in ("empirical_outcomes_consumed","may_write_r13_admission_ledger","may_open_locked_oos","prior_campaign_parameters_modified"):
        if research.get(key) is not False: failures.append(f"RESEARCH_{key.upper()}_INVALID")
    if campaign.get("periods",{}).get("locked_oos_access")!="SEALED_NOT_PERMITTED": failures.append("LOCKED_OOS_ACCESS_INVALID")
    if campaign.get("programme_exit_rule",{}).get("campaign_number")!=3: failures.append("CAMPAIGN_NUMBER_INVALID")
    if campaign.get("safety")!={"broker_submission_enabled":False,"live_authority":False,"options_included":False,"futures_dependency":False}: failures.append("SAFETY_CONTRACT_INVALID")
    return tuple(sorted(set(failures)))
def load_campaign3(project_root:str|Path)->dict[str,Any]:
    path=Path(project_root)/"research"/"preregistrations"/"v2"/"FEAS-BINANCE-BTC-FUNDING-003.json"
    record=json.loads(path.read_text(encoding="utf-8")); failures=validate_campaign3(record)
    if failures: raise ValueError(";".join(failures))
    return record
