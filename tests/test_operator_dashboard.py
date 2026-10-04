from pathlib import Path
import sqlite3
from quant_system.interface.dashboard import OperatorDashboard
from quant_system.interface.runtime_reader import RuntimeStoreReadOnlyReader, RuntimeReadError
from test_interface_live_runtime import _seed_store, _file_hash

ROOT=Path(__file__).resolve().parents[1]

def test_default_is_real_archive_without_mock_account():
    s=OperatorDashboard(ROOT).snapshot()
    assert s['meta']['fixtures'] is False
    assert not s['meta']['runtime_connected']
    assert s['execution']['paper']=={}
    assert s['market']['manifest']['event_count']==39
    assert len(s['market']['trades'])==30
    assert all(p['sequence_audit']=='UNAVAILABLE' for p in s['market']['partitions'])
    assert s['performance']['series']==[]

def test_missing_evidence_cannot_become_pass_or_zero(tmp_path):
    s=OperatorDashboard(tmp_path).snapshot()
    assert s['market']['state']=='UNAVAILABLE'
    assert s['market']['manifest']=={}
    assert s['health']=='ATTENTION'

def test_missing_db_not_created_and_incompatible_columns_fail_closed(tmp_path):
    db=tmp_path/'missing.db'
    s=OperatorDashboard(ROOT,db).snapshot()
    assert not db.exists()
    assert not s['meta']['runtime_connected']
    with sqlite3.connect(db) as c:
        for table in ('runtime_state','runtime_events','runtime_checkpoints'):
            c.execute(f'CREATE TABLE {table} (bad INTEGER)')
    s=OperatorDashboard(ROOT,db).snapshot()
    assert s['health']=='CRITICAL'
    assert not s['meta']['runtime_connected']

def test_live_records_provenance_and_no_writes(tmp_path):
    db=_seed_store(tmp_path/'runtime.db');before=_file_hash(db)
    s=OperatorDashboard(ROOT,db).snapshot()
    assert _file_hash(db)==before
    assert s['execution']['paper']['cash']==99800.25
    assert s['execution']['paper']['currency'] is None
    assert s['execution']['paper']['commissions']==.75
    assert s['execution']['paper']['fills'][0]['slippage_bps']=='1'
    assert s['execution']['runtimes'][0]['recent_events']
    assert s['market']['manifest']['decision_time'].startswith('2026-09-22')

def test_bounded_reader_does_not_report_partial_integrity(tmp_path):
    db=_seed_store(tmp_path/'runtime.db')
    with sqlite3.connect(db) as c:
        row=c.execute('SELECT * FROM runtime_events LIMIT 1').fetchone()
        cols=[x[1] for x in c.execute('PRAGMA table_info(runtime_events)')]
        base=dict(zip(cols,row))
        # Duplicate one payload with a new sequence, respecting the original schema.
        for seq in range(3,10002):
            values={**base,'sequence':seq}
            values.pop('id',None)
            c.execute('INSERT INTO runtime_events ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',list(values.values()))
    try:RuntimeStoreReadOnlyReader(db).snapshot()
    except RuntimeReadError as e:assert 'limit' in str(e) or 'read incompatible' in str(e)
    else:raise AssertionError('Unbounded log should not be represented as fully verified')

def test_programme_evidence_is_fail_closed_and_canonical():
    s=OperatorDashboard(ROOT).snapshot()
    p=s['programme']
    assert p['read_only'] is True
    assert p['programme_state']=='FOUNDATION_ACTIVE'
    assert p['hard_boundaries']['broker_submission']=='DISABLED'
    assert p['hard_boundaries']['live_authority'] is False
    assert any(x['workstream']=='EQS-06' and x['scope_state']=='FROZEN_EXCLUDED'
               for x in p['asset_workstreams'])
    assert p['workstreams']['SHARED-04 Execution']['state']=='COMPLETE'
    assert p['workstreams']['SHARED-05 Portfolio Construction']['state']=='COMPLETE'
    assert p['paper_admission']['state']=='AUTHORISED'
    assert p['paper_admission']['decision_sha256']
    assert 'EQS00_PAPER_ADMISSION_NOT_AUTHORISED' not in p['blockers']


def test_programme_missing_evidence_never_becomes_ready(tmp_path):
    s=OperatorDashboard(tmp_path).snapshot()
    p=s['programme']
    assert p['evidence_state']=='DEGRADED'
    assert p['programme_state']=='UNKNOWN'
    assert p['paper_admission']['state']=='BLOCKED'
    assert p['first_paper_run']['state']=='NOT_STARTED'
