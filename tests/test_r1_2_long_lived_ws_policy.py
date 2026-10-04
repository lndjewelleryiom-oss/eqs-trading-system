from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def _node(source: str) -> dict:
    cp = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    return json.loads(cp.stdout)


def test_long_lived_policy_requires_more_than_ten_minutes_for_heartbeat_proof():
    result = _node("""
import {heartbeatSurvivalProved} from './scripts/r1_2_long_lived_policy.mjs';
console.log(JSON.stringify({before:heartbeatSurvivalProved(10*60*1000,100),after:heartbeatSurvivalProved(11*60*1000,1),noMessages:heartbeatSurvivalProved(12*60*1000,0)}));
""")
    assert result == {"before": False, "after": True, "noMessages": False}


def test_long_lived_policy_classifies_only_near_24h_close_as_rollover():
    result = _node("""
import {classifyConnectionClose,rolloverDeviationSeconds} from './scripts/r1_2_long_lived_policy.mjs';
console.log(JSON.stringify({early:classifyConnectionClose(60*60*1000),near:classifyConnectionClose((23*60+45)*60*1000),exact:classifyConnectionClose(24*60*60*1000),deviation:rolloverDeviationSeconds(24*60*60*1000)}));
""")
    assert result["early"] == "PREMATURE_CLOSE"
    assert result["near"] == "EXPECTED_24H_ROLLOVER_WINDOW"
    assert result["exact"] == "EXPECTED_24H_ROLLOVER_WINDOW"
    assert result["deviation"] == 0


def test_long_lived_policy_samples_raw_at_most_once_per_minute_after_first_message():
    result = _node("""
import {shouldPersistRawSample} from './scripts/r1_2_long_lived_policy.mjs';
console.log(JSON.stringify({first:shouldPersistRawSample(NaN,1000,1),tooSoon:shouldPersistRawSample(1000,60999,2),due:shouldPersistRawSample(1000,61000,3)}));
""")
    assert result == {"first": True, "tooSoon": False, "due": True}
