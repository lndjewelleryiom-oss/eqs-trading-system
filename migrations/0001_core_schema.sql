-- PostgreSQL core schema. Production migrations should be managed with Alembic or equivalent.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE strategy_state AS ENUM (
  'PROPOSED','EXPERIMENTAL','REJECTED','OOS_VALIDATED','PAPER','SHADOW',
  'LIVE_1','LIVE_2','LIVE_3','LIVE_4','WATCH','REDUCED','PAUSED','RESEARCH','RETIRED'
);
CREATE TYPE experiment_state AS ENUM ('QUEUED','RUNNING','PASSED','FAILED','CANCELLED');
CREATE TYPE deployment_stage AS ENUM ('LIVE_0','LIVE_1','LIVE_2','LIVE_3','LIVE_4');
CREATE TYPE risk_action AS ENUM ('ALLOW','REDUCE','BLOCK','HALT');

CREATE TABLE datasets (
  dataset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  source TEXT NOT NULL,
  asset_class TEXT,
  schema_version TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  latency_class TEXT,
  revision_policy TEXT,
  quality_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dataset_observations_meta (
  observation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_id UUID NOT NULL REFERENCES datasets(dataset_id),
  instrument TEXT,
  event_time TIMESTAMPTZ NOT NULL,
  published_at TIMESTAMPTZ,
  available_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  confidence DOUBLE PRECISION CHECK (confidence >= 0 AND confidence <= 1),
  missing BOOLEAN NOT NULL DEFAULT FALSE,
  suspected_corruption BOOLEAN NOT NULL DEFAULT FALSE,
  payload_uri TEXT,
  content_hash TEXT,
  UNIQUE(dataset_id, instrument, event_time, revision)
);
CREATE INDEX idx_observation_available ON dataset_observations_meta(dataset_id, available_at);

CREATE TABLE strategies (
  strategy_id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  state strategy_state NOT NULL DEFAULT 'PROPOSED',
  hypothesis TEXT NOT NULL,
  rationale TEXT NOT NULL,
  asset_universe JSONB NOT NULL,
  timeframe TEXT NOT NULL,
  data_sources JSONB NOT NULL,
  features JSONB NOT NULL,
  entry_rules TEXT NOT NULL,
  exit_rules TEXT NOT NULL,
  position_sizing TEXT NOT NULL,
  expected_holding_period TEXT,
  expected_transaction_cost_bps NUMERIC,
  expected_capacity_usd NUMERIC,
  risks JSONB NOT NULL DEFAULT '[]',
  regime_dependencies JSONB NOT NULL DEFAULT '[]',
  code_version TEXT NOT NULL,
  parent_strategy_id UUID REFERENCES strategies(strategy_id),
  reopened_from_rejection BOOLEAN NOT NULL DEFAULT FALSE,
  reopen_justification TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE strategy_state_history (
  id BIGSERIAL PRIMARY KEY,
  strategy_id UUID NOT NULL REFERENCES strategies(strategy_id),
  from_state strategy_state,
  to_state strategy_state NOT NULL,
  reason TEXT NOT NULL,
  evidence_uri TEXT,
  changed_by TEXT NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE research_hypotheses (
  hypothesis_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL,
  statement TEXT NOT NULL,
  economic_mechanism TEXT NOT NULL,
  falsification_criteria JSONB NOT NULL,
  source_context JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'OPEN',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE experiments (
  experiment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id UUID REFERENCES strategies(strategy_id),
  hypothesis_id UUID REFERENCES research_hypotheses(hypothesis_id),
  state experiment_state NOT NULL DEFAULT 'QUEUED',
  experiment_type TEXT NOT NULL,
  config JSONB NOT NULL,
  dataset_manifest JSONB NOT NULL,
  code_version TEXT NOT NULL,
  random_seed BIGINT,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  result_uri TEXT,
  failure_reason TEXT
);

CREATE TABLE validation_results (
  validation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id UUID NOT NULL REFERENCES strategies(strategy_id),
  experiment_id UUID REFERENCES experiments(experiment_id),
  validation_type TEXT NOT NULL,
  period_start TIMESTAMPTZ,
  period_end TIMESTAMPTZ,
  regime TEXT,
  metrics JSONB NOT NULL,
  acceptance_criteria JSONB NOT NULL,
  passed BOOLEAN NOT NULL,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE multiple_testing_ledger (
  test_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id TEXT NOT NULL,
  strategy_id UUID REFERENCES strategies(strategy_id),
  hypothesis_id UUID REFERENCES research_hypotheses(hypothesis_id),
  parameter_count INTEGER NOT NULL DEFAULT 1,
  trial_count INTEGER NOT NULL DEFAULT 1,
  raw_statistic JSONB,
  adjusted_statistic JSONB,
  method TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE deployment_records (
  deployment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id UUID NOT NULL REFERENCES strategies(strategy_id),
  stage deployment_stage NOT NULL,
  capital_limit NUMERIC NOT NULL DEFAULT 0,
  evidence JSONB NOT NULL,
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ
);

CREATE TABLE risk_policies (
  policy_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  version TEXT UNIQUE NOT NULL,
  limits JSONB NOT NULL,
  active BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE risk_decisions (
  risk_decision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id UUID REFERENCES strategies(strategy_id),
  order_id UUID,
  action risk_action NOT NULL,
  reason_codes JSONB NOT NULL,
  snapshot JSONB NOT NULL,
  policy_version TEXT NOT NULL,
  decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE orders (
  order_id UUID PRIMARY KEY,
  strategy_id UUID NOT NULL REFERENCES strategies(strategy_id),
  venue TEXT NOT NULL,
  instrument TEXT NOT NULL,
  side TEXT NOT NULL,
  order_type TEXT NOT NULL,
  quantity NUMERIC NOT NULL,
  requested_price NUMERIC,
  requested_at TIMESTAMPTZ NOT NULL,
  client_order_key TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL,
  risk_decision_id UUID REFERENCES risk_decisions(risk_decision_id)
);

CREATE TABLE fills (
  fill_id UUID PRIMARY KEY,
  order_id UUID NOT NULL REFERENCES orders(order_id),
  venue_fill_id TEXT,
  fill_time TIMESTAMPTZ NOT NULL,
  quantity NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  fee NUMERIC NOT NULL DEFAULT 0,
  liquidity_flag TEXT,
  UNIQUE(order_id, venue_fill_id)
);

CREATE TABLE positions (
  strategy_id UUID NOT NULL REFERENCES strategies(strategy_id),
  venue TEXT NOT NULL,
  instrument TEXT NOT NULL,
  quantity NUMERIC NOT NULL,
  avg_price NUMERIC,
  mark_price NUMERIC,
  unrealized_pnl NUMERIC,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(strategy_id, venue, instrument)
);

CREATE TABLE audit_events (
  audit_id BIGSERIAL PRIMARY KEY,
  event_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor TEXT NOT NULL,
  event_type TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  prev_hash TEXT,
  event_hash TEXT NOT NULL
);
CREATE INDEX idx_audit_entity ON audit_events(entity_type, entity_id, event_time);

CREATE TABLE component_tracker (
  component_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('NOT STARTED','IN PROGRESS','BLOCKED','TESTING','PASSED','LIVE','FAILED')),
  depends_on JSONB NOT NULL DEFAULT '[]',
  acceptance_criteria JSONB NOT NULL,
  evidence JSONB NOT NULL DEFAULT '[]',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
