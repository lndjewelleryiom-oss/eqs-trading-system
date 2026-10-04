"""Operator presentation contract. No execution imports or write operations.

Packaged evidence is an archival source, not a continuously connected data feed.
The v2 endpoint remains available; this contract removes its bootstrap runtime.
"""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .live_adapter import LiveReadOnlyEqsAdapter
from .mock_adapter import MockReadOnlyEqsAdapter
from .programme_evidence import ProgrammeEvidenceReader
from quant_system.research.campaigns import institutional_crypto_perp_campaigns
from quant_system.performance.publication import read_published_json

CAPTURE = 'artifacts/real-market/r1_3_2026-09-22/materialized'


def _read(path: Path, maximum: int = 4_000_000):
    pointer = path.with_name(path.stem + '.pointer.json')
    if pointer.is_file():
        return read_published_json(path, maximum_bytes=maximum)
    if path.stat().st_size > maximum:
        raise ValueError('Evidence exceeds bounded read limit')
    return json.loads(path.read_text())


class OperatorDashboard:
    def __init__(self, root: Path, runtime_db=None):
        self.root = root.resolve()
        self.runtime_db = runtime_db
        self.adapter = LiveReadOnlyEqsAdapter(runtime_db, root) if runtime_db else None
        self.programme = ProgrammeEvidenceReader(self.root)

    def evidence(self):
        records, partitions, trades = {}, [], []
        try:
            report_path = self.root / CAPTURE / 'acceptance_report.json'
            report = _read(report_path)
            fp = report['manifest_fingerprint']
            manifest_path = self.root / CAPTURE / 'manifests' / (fp + '.json')
            manifest = _read(manifest_path)
            descriptors = manifest['partitions']
            if len(descriptors) > 200:
                raise ValueError('Too many partitions for the operator evidence view')
            records['capture-report'] = {'path': str(report_path.relative_to(self.root)), 'sha256': sha256(report_path.read_bytes()).hexdigest(), 'content': report}
            records['dataset-manifest'] = {'path': str(manifest_path.relative_to(self.root)), 'sha256': sha256(manifest_path.read_bytes()).hexdigest(), 'content': manifest}
            for i, part in enumerate(descriptors):
                base = self.root / CAPTURE / 'partitions'
                path = (base / part['relative_path']).resolve()
                if not path.is_relative_to(base.resolve()) or path.stat().st_size > 4_000_000:
                    raise ValueError('Invalid or oversized partition')
                raw = path.read_bytes()
                if sha256(raw).hexdigest() != part['content_hash']:
                    raise ValueError('Partition SHA-256 mismatch')
                lines = raw.splitlines()
                if len(lines) > 5000 or len(lines) != part['row_count']:
                    raise ValueError('Partition row count incompatible')
                rows = [json.loads(line) for line in lines]
                evidence_id = f'partition-{i}'
                records[evidence_id] = {'path': str(path.relative_to(self.root)), 'sha256': part['content_hash'], 'content': part}
                partitions.append({**part, 'evidence_id': evidence_id,
                    'integrity': 'VERIFIED', 'gap_audit': 'UNAVAILABLE',
                    'duplicate_audit': 'UNAVAILABLE', 'sequence_audit': 'UNAVAILABLE',
                    'lineage_audit': 'REPORTED_COMPLETE' if report.get('raw_lineage_complete') is True else 'UNAVAILABLE'})
                if part['kind'] == 'TRADE':
                    for row in rows:
                        trades.append({'time': row['meta']['event_time'], 'price': row['price'], 'quantity': row['quantity'], 'venue': part['venue'], 'trade_id': row['trade_id'], 'source_sequence': row['meta'].get('source_sequence'), 'raw_sha256': row['meta'].get('raw_sha256'), 'evidence_id': evidence_id})
            return {'state': 'ARCHIVAL_EVIDENCE', 'report': report, 'manifest': manifest, 'partitions': partitions, 'trades': sorted(trades, key=lambda x: x['time']), 'records': records, 'error': None}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {'state': 'UNAVAILABLE', 'report': {}, 'manifest': {}, 'partitions': [], 'trades': [], 'records': {}, 'error': str(exc)}

    def snapshot(self):
        now = datetime.now(timezone.utc)
        # Campaigns are contracts from source code, never empirical results.
        campaigns = []
        for c in institutional_crypto_perp_campaigns():
            mins = dict(c.minimum_requirements)
            campaigns.append({'campaign_id': c.campaign_id, 'family': c.family,
                'fingerprint': c.fingerprint, 'stage': 'PREREGISTERED',
                'empirical_status': c.empirical_status, 'minimum_trades': mins.get('trades'),
                'minimum_samples': mins.get('independent_samples'),
                'fold_status': 'BLOCKED_BY_HISTORY', 'sample_status': 'BLOCKED_BY_HISTORY',
                'locked_oos': 'SEALED_BY_CONTRACT'})
        archived = self.evidence()
        if self.adapter:
            try:
                runtime = self.adapter.snapshot(now=now).to_payload()
                execution, risk = runtime['execution'], runtime['risk']
                error = runtime['system_health']['persistence'].get('detail') if execution['source'] == 'RUNTIME_READ_FAILURE' else None
            except (OSError, ValueError, TypeError, KeyError) as exc:
                execution, risk, error = {}, {}, 'Incompatible persisted runtime: ' + str(exc)
        else:
            execution, risk, error = {}, {}, None
        available = self.adapter is not None and bool(execution) and execution.get('source') != 'RUNTIME_READ_FAILURE'
        if not available:
            execution = {'source': 'UNAVAILABLE', 'paper': {}, 'shadow': {}, 'runtimes': []}
            risk = {'state': 'UNKNOWN', 'limits': [], 'conditions': [], 'limits_state': 'UNAVAILABLE'}
        runtimes = execution.get('runtimes', [])
        for mode in ('paper', 'shadow'):
            selected = execution.get(mode, {})
            summary = next((r for r in runtimes if r['runtime_id'] == selected.get('runtime_id')), None)
            if summary and not summary.get('checkpoint_verified'):
                execution[mode] = {'available': False, 'runtime_id': summary['runtime_id'],
                    'message': 'A runtime record exists but no verified checkpoint is available.'}
        alerts = []
        def alert(code, title, detail, severity, target, record=None):
            alerts.append({'code': code, 'title': title, 'detail': detail, 'severity': severity, 'target': target, 'record': record})
        if error:
            alert('RUNTIME_READ_FAILURE', 'Runtime store cannot be read', error, 'CRITICAL', 'runtime')
        elif not available:
            alert('RUNTIME_NOT_CONFIGURED', 'Runtime telemetry is disconnected', 'No F5.6 database is configured. Cash, positions, risk and activity are unavailable.', 'WARNING', 'runtime')
        elif not runtimes:
            alert('EMPTY_STORE', 'No persisted runtimes', 'The compatible store contains no runtime records. This is not a healthy trading observation.', 'WARNING', 'runtime')
        for item in runtimes:
            stamp = item.get('last_heartbeat_at')
            age = (now-datetime.fromisoformat(stamp)).total_seconds() if stamp else None
            item['heartbeat_age_seconds'] = age
            item['freshness'] = 'UNKNOWN' if age is None else 'CLOCK_SKEW' if age < -5 else 'STALE' if age > 60 else 'RECENT'
            if item['status'] == 'HALTED':
                alert('HALTED', item['mode']+' runtime halted', item.get('halt_reason') or 'Reason not persisted', 'CRITICAL', 'risk', item['runtime_id'])
            if item['freshness'] != 'RECENT' or item.get('lease_state') == 'EXPIRED':
                alert('HEARTBEAT', item['mode']+' heartbeat needs attention', 'Persisted heartbeat: '+str(stamp)+'. Display threshold 60s; no risk limit is changed.', 'WARNING', 'jobs', item['runtime_id'])
            recon = item.get('latest_reconciliation') or {}
            rp = recon.get('payload', {})
            if rp.get('ok') is False or rp.get('ledger_reconciled') is False or rp.get('action') in {'HALT', 'PAUSE'}:
                alert('RECONCILIATION', 'Reconciliation requires investigation', 'Inspect the persisted reconciliation event and runtime state.', 'CRITICAL', 'risk', item['runtime_id'])
        if execution.get('shadow', {}).get('submitted_orders', 0):
            alert('SHADOW_SUBMIT', 'SHADOW submission observed', 'Persisted sent=true results require investigation.', 'CRITICAL', 'risk')
        if archived['error']:
            alert('EVIDENCE_UNAVAILABLE', 'Packaged market evidence unavailable', archived['error'], 'WARNING', 'data')
        else:
            alert('ARCHIVAL_DATA', 'Market coverage is an acceptance slice', '2026-09-22 capture; no ongoing ingestion or long-horizon audit is connected.', 'WARNING', 'data', 'dataset-manifest')
        alert('ALPHA_BINDING', 'Alpha-v1 awaits historical validation', 'A18 / A21 / A22 are recorded history blockers in this package. Current remote campaign status is not connected.', 'WARNING', 'research')
        alerts.sort(key=lambda x: x['severity'] != 'CRITICAL')
        for campaign in campaigns:
            archived['records'][campaign['campaign_id']] = {'path': 'src/quant_system/research/campaigns.py', 'content': campaign, 'classification': 'PREREGISTERED CONTRACT; NOT EMPIRICAL RESULTS'}
        for item in runtimes:
            archived['records'][item['runtime_id']] = {'path': 'F5.6 read-only runtime store', 'content': item, 'classification': 'PERSISTED NONLIVE RUNTIME'}
        lineage_rows = [
            (r, e, (e.get('payload') or {}).get('lineage') or {})
            for r in runtimes for e in (r.get('recent_events') or [])
            if ((e.get('payload') or {}).get('lineage') or {}).get('infrastructure_boundary_fingerprint')
        ]
        boundary_fps = sorted({lineage.get('infrastructure_boundary_fingerprint') for _, _, lineage in lineage_rows})
        replay_fingerprints = sorted({fp for _, _, lineage in lineage_rows for fp in (lineage.get('dataset_fingerprints') or [])})
        feature_fingerprints = sorted({lineage.get('feature_manifest_fingerprint') for _, _, lineage in lineage_rows if lineage.get('feature_manifest_fingerprint')})
        decision_fingerprints = sorted({lineage.get('strategy_decision_fingerprint') for _, _, lineage in lineage_rows if lineage.get('strategy_decision_fingerprint')})
        risk_fingerprints = sorted({lineage.get('risk_decision_fingerprint') for _, _, lineage in lineage_rows if lineage.get('risk_decision_fingerprint')})
        containment = {
            'classification': 'INFRASTRUCTURE_ONLY',
            'alpha': 'REJECTED', 'oos': 'REJECTED', 'live': 'REJECTED', 'broker_submission': 'REJECTED',
            'shadow_zero_submit': 'PASS' if execution.get('shadow', {}).get('submitted_orders', 0) == 0 else 'BREACH',
        }
        execution['provenance'] = {
            'infrastructure_boundary_fingerprints': boundary_fps,
            'dataset_fingerprints': replay_fingerprints,
            'feature_fingerprints': feature_fingerprints,
            'decision_fingerprints': decision_fingerprints,
            'risk_fingerprints': risk_fingerprints,
            'containment': containment,
        }

        autonomy = {}
        autonomy_path = self.root / 'artifacts' / 'test-evidence' / 'EQS_PERFORMANCE_AUTONOMY_STATE.json'
        if available:
            try:
                autonomy = _read(autonomy_path)
            except (OSError, ValueError, KeyError, TypeError):
                autonomy = {}

        lifecycle = (autonomy.get('lifecycle') or {}) if isinstance(autonomy, dict) else {}
        strategy_rows = list(lifecycle.get('strategies') or [])
        strategies_payload = {
            'state': autonomy.get('status', 'UNAVAILABLE') if autonomy else 'UNAVAILABLE',
            'strategy_count': lifecycle.get('strategy_count', 0),
            'strategies': strategy_rows,
            'candidates': [
                row for row in strategy_rows
                if row.get('state') in {'VALIDATED', 'PAPER_CANARY'}
            ],
            'survivors': [
                row for row in strategy_rows
                if row.get('state') in {'PAPER_ACTIVE', 'REDUCED'}
            ],
            'paused': [row for row in strategy_rows if row.get('state') == 'PAUSED'],
            'performance_autonomy_ready': bool(autonomy.get('performance_autonomy_ready', False)) if autonomy else False,
            'next_valid_action': autonomy.get('next_valid_action') if autonomy else None,
            'blockers': list(autonomy.get('blockers') or []) if autonomy else [],
        }

        managed = {}
        readiness = {}
        for name, target in (
            ('EQS_MANAGED_PAPER_PERFORMANCE_EVIDENCE.json', 'managed'),
            ('EQS_PERFORMANCE_AUTONOMY_READINESS.json', 'readiness'),
        ):
            try:
                doc = _read(self.root / 'artifacts' / 'test-evidence' / name)
            except (OSError, ValueError, KeyError, TypeError):
                doc = {}
            if target == 'managed':
                managed = doc
            else:
                readiness = doc

        perf = (autonomy.get('performance_ledger') or {}) if autonomy else {}
        equity_by_runtime = perf.get('equity_series') or {}
        combined_series = []
        for runtime_id, rows in sorted(equity_by_runtime.items()):
            for row in rows:
                combined_series.append({**row, 'runtime_id': runtime_id})
        combined_series.sort(key=lambda row: (row.get('observed_at') or '', row.get('runtime_id') or ''))
        performance_payload = {
            'state': 'PAPER_LEDGER_CONNECTED' if perf else 'UNAVAILABLE',
            'series': combined_series,
            'series_by_runtime': equity_by_runtime,
            'runtime_snapshots': perf.get('runtime_snapshots') or {},
            'eligible_strategy_activity_count': perf.get('eligible_strategy_activity_count', 0),
            'hash_chain_valid': perf.get('hash_chain_valid'),
            'performance_claim_state': (
                'ELIGIBLE_STRATEGY_EVIDENCE_PRESENT'
                if perf.get('eligible_strategy_activity_count', 0)
                else 'INFRASTRUCTURE_ONLY_NO_STRATEGY_PERFORMANCE_CLAIM'
            ) if perf else 'UNAVAILABLE',
            'managed_acceptance': {
                'status': managed.get('status', 'UNAVAILABLE') if managed else 'UNAVAILABLE',
                'elapsed_hours': managed.get('elapsed_hours', 0.0) if managed else 0.0,
                'required_hours': managed.get('required_hours', 168.0) if managed else 168.0,
                'attributed_trades': managed.get('total_attributed_trades', 0) if managed else 0,
                'required_attributed_trades': managed.get('required_attributed_trades', 100) if managed else 100,
                'genuine_strategy_count': managed.get('genuine_strategy_count', 0) if managed else 0,
                'continuity_window_started_at': managed.get('continuity_window_started_at') if managed else None,
                'blockers': list(managed.get('blockers') or []) if managed else [],
            },
            'autonomy_readiness': {
                'result': readiness.get('result', 'UNAVAILABLE') if readiness else 'UNAVAILABLE',
                'checks': readiness.get('checks') or {} if readiness else {},
                'blockers': list(readiness.get('blockers') or []) if readiness else [],
                'next_state': readiness.get('next_state') if readiness else None,
                'certification_issued': bool(readiness.get('certification_issued', False)) if readiness else False,
            },
        }
        return {
            'schema_version': 'eqs-operator-v3',
            'programme': self.programme.snapshot(),
            'meta': {'generated_at': now.isoformat(), 'environment': 'LOCAL OBSERVER', 'access': 'READ ONLY', 'runtime_connected': available, 'source': 'F5.6 + PACKAGED EVIDENCE' if available else 'PACKAGED EVIDENCE', 'mode': 'PAPER / SHADOW', 'mutations_enabled': False, 'fixtures': False, 'heartbeat_stale_after_seconds': 60, 'infrastructure_boundary_fingerprints': boundary_fps},
            'health': 'CRITICAL' if any(a['severity']=='CRITICAL' for a in alerts) else 'ATTENTION',
            'alerts': alerts, 'market': archived, 'campaigns': campaigns,
            'gates': MockReadOnlyEqsAdapter._gate_rows(),
            'strategies': strategies_payload,
            'execution': execution, 'risk': risk,
            'performance': performance_payload,
            'jobs': {'runtimes': runtimes, 'workers_state': 'ACTIVE', 'schedules_state': 'SUPERVISOR_MANAGED'},
        }
