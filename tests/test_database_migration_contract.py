from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
UP = MIGRATIONS / "0001_core_schema.sql"
DOWN = MIGRATIONS / "0001_core_schema.down.sql"


def test_every_forward_migration_has_explicit_down_path():
    forward = sorted(
        path for path in MIGRATIONS.glob("*.sql") if not path.name.endswith(".down.sql")
    )
    assert forward, "at least one forward migration is required"
    for path in forward:
        rollback = MIGRATIONS / f"{path.stem}.down.sql"
        assert rollback.exists(), f"missing rollback migration for {path.name}"


def test_0001_down_migration_covers_every_core_table_and_enum():
    up = UP.read_text()
    down = DOWN.read_text()
    tables = [
        "datasets", "dataset_observations_meta", "strategies",
        "strategy_state_history", "research_hypotheses", "experiments",
        "validation_results", "multiple_testing_ledger", "deployment_records",
        "risk_policies", "risk_decisions", "orders", "fills", "positions",
        "audit_events", "component_tracker",
    ]
    enums = ["strategy_state", "experiment_state", "deployment_stage", "risk_action"]

    for table in tables:
        assert f"CREATE TABLE {table}" in up
        assert f"DROP TABLE IF EXISTS {table}" in down
    for enum in enums:
        assert f"CREATE TYPE {enum}" in up
        assert f"DROP TYPE IF EXISTS {enum}" in down


def test_rollback_intentionally_retains_shared_pgcrypto_extension():
    down = DOWN.read_text()
    assert "DROP EXTENSION" not in down
    assert "pgcrypto" in down
