from datetime import timedelta
import pytest
from quant_system.runtime import PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode, RuntimeLeaseError
from test_execution_v1_reservation_settlement_journal import NOW, paper_engine, order, bind_paper, bar

@pytest.mark.parametrize("point", ["AFTER_BEGIN", "AFTER_V1_SETTLEMENTS", "BEFORE_EVENT_INSERT", "AFTER_EVENT_INSERT", "AFTER_CHECKPOINT_INSERT", "AFTER_COMMIT"])
def test_v1_fill_checkpoint_transaction_recovers_together(tmp_path, point):
    store = PersistentRuntimeStore(tmp_path / "atomic.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="atomic", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    runtime.start()
    bind_paper(runtime, order("10"))
    before = runtime._state_payload()["paper"]
    def fault(actual):
        if actual == point:
            raise OSError("injected atomic persistence failure")
    store._test_fault_injector = fault
    with pytest.raises(OSError, match="injected"):
        runtime.on_bar(bar(1, "1000"))
    store._test_fault_injector = None
    committed = point == "AFTER_COMMIT"
    assert len(store.load_v1_reservation_settlements("atomic")) == int(committed)
    assert sum(e.event_type == "PAPER_BAR_PROCESSED" for e in store.load_events("atomic")) == int(committed)
    assert (runtime._state_payload()["paper"] != before) == committed
    assert runtime._state_payload()["paper"] == store.latest_checkpoint("atomic").payload["paper"]
    runtime.stop()
    resumed = PersistentPaperShadowRuntime(runtime_id="atomic", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    resumed.start()
    assert resumed._state_payload()["paper"] == store.latest_checkpoint("atomic").payload["paper"]
    assert len(store.load_v1_reservation_settlements("atomic")) == int(committed)
    resumed.stop()

@pytest.mark.parametrize("change", ["generation", "expiry"])
def test_atomic_commit_rejects_stale_lease(tmp_path, change):
    store = PersistentRuntimeStore(tmp_path / "fence.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="fence", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    runtime.start()
    before = store.latest_checkpoint("fence")
    with store._connection() as conn:
        if change == "generation":
            conn.execute("UPDATE runtime_state SET generation=generation+1 WHERE runtime_id='fence'")
        else:
            conn.execute("UPDATE runtime_state SET lease_expires_at=? WHERE runtime_id='fence'", ((NOW-timedelta(seconds=1)).isoformat(),))
        conn.commit()
    with pytest.raises(RuntimeLeaseError):
        store.append_event_and_checkpoint("fence", runtime.owner_id, event_type="TEST", occurred_at=NOW, event_payload={}, generation=runtime.generation, checkpoint_payload=runtime._state_payload())
    assert store.latest_checkpoint("fence") == before

