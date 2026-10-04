-- Roll back the 0001 core schema in reverse dependency order.
DROP TABLE IF EXISTS component_tracker;
DROP TABLE IF EXISTS audit_events;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS fills;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS risk_decisions;
DROP TABLE IF EXISTS risk_policies;
DROP TABLE IF EXISTS deployment_records;
DROP TABLE IF EXISTS multiple_testing_ledger;
DROP TABLE IF EXISTS validation_results;
DROP TABLE IF EXISTS experiments;
DROP TABLE IF EXISTS research_hypotheses;
DROP TABLE IF EXISTS strategy_state_history;
DROP TABLE IF EXISTS strategies;
DROP TABLE IF EXISTS dataset_observations_meta;
DROP TABLE IF EXISTS datasets;

DROP TYPE IF EXISTS risk_action;
DROP TYPE IF EXISTS deployment_stage;
DROP TYPE IF EXISTS experiment_state;
DROP TYPE IF EXISTS strategy_state;

-- pgcrypto is intentionally retained because it is a shared extension that may be
-- used by schemas outside this migration. Removing shared extensions in a rollback
-- is unsafe on managed PostgreSQL instances.
