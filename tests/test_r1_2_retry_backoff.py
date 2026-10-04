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


def test_retry_policy_respects_retry_after_and_recovers():
    result = _node("""
import {fetchWithBackoff} from './scripts/r1_2_retry_backoff.mjs';
let attempts=0; const sleeps=[];
const fake=async()=>{attempts++; return attempts<3
 ? {status:429,ok:false,headers:{get:()=> '0.01'}}
 : {status:200,ok:true,headers:{get:()=> null}}};
const response=await fetchWithBackoff('fixture',{fetchFn:fake,sleepFn:async ms=>sleeps.push(ms),maxAttempts:5});
console.log(JSON.stringify({attempts,sleeps,status:response.status}));
""")
    assert result == {"attempts": 3, "sleeps": [10, 10], "status": 200}


def test_retry_policy_does_not_retry_permanent_4xx():
    result = _node("""
import {fetchWithBackoff} from './scripts/r1_2_retry_backoff.mjs';
let attempts=0; const sleeps=[];
const response=await fetchWithBackoff('fixture',{fetchFn:async()=>{attempts++;return {status:400,ok:false,headers:{get:()=>null}}},sleepFn:async ms=>sleeps.push(ms)});
console.log(JSON.stringify({attempts,sleeps,status:response.status}));
""")
    assert result == {"attempts": 1, "sleeps": [], "status": 400}


def test_retry_policy_bounded_exponential_backoff_and_transport_retry():
    result = _node("""
import {fetchWithBackoff,retryDelayMs,shouldRetryStatus} from './scripts/r1_2_retry_backoff.mjs';
let attempts=0; const sleeps=[];
const response=await fetchWithBackoff('fixture',{fetchFn:async()=>{attempts++;if(attempts<3)throw new TypeError('network');return {status:200,ok:true,headers:{get:()=>null}}},sleepFn:async ms=>sleeps.push(ms),maxAttempts:4});
console.log(JSON.stringify({attempts,sleeps,status:response.status,delays:[retryDelayMs(0),retryDelayMs(1),retryDelayMs(8)],retry429:shouldRetryStatus(429),retry503:shouldRetryStatus(503),retry400:shouldRetryStatus(400)}));
""")
    assert result["attempts"] == 3
    assert result["sleeps"] == [250, 500]
    assert result["status"] == 200
    assert result["delays"] == [250, 500, 5000]
    assert result["retry429"] is True and result["retry503"] is True
    assert result["retry400"] is False
