from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import inspect
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from quant_system.backtest.events import BarEvent
from quant_system.core.enums import OrderType, Side
from quant_system.execution.models import OrderRequest


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
ALLOWED_EXECUTION_MODES = frozenset({"RESEARCH_REPLAY", "INTERNAL_PAPER", "PAPER_CANARY"})


class ExecutableStrategyError(RuntimeError):
    pass


class ExecutableStrategy(Protocol):
    def on_event(self, event: object) -> object | None: ...
    def export_state(self) -> object: ...
    def restore_state(self, state: object) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutableStrategyManifest:
    strategy_id: str
    strategy_version: str
    family: str
    campaign_fingerprint: str
    implementation_id: str
    parameters: dict[str, Any]
    feature_ids: tuple[str, ...]
    instrument_ids: tuple[str, ...]
    allowed_modes: tuple[str, ...]
    warmup_events: int
    max_event_age_seconds: int
    state_schema_version: int
    source_class: str
    broker_submission_enabled: bool = False
    live_authority: bool = False

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.strategy_version or not self.implementation_id:
            raise ValueError("strategy identity fields are required")
        if not _SHA256.fullmatch(self.campaign_fingerprint):
            raise ValueError("campaign_fingerprint must be SHA-256")
        if self.warmup_events < 0:
            raise ValueError("warmup_events must be >= 0")
        if self.max_event_age_seconds <= 0:
            raise ValueError("max_event_age_seconds must be > 0")
        if self.state_schema_version <= 0:
            raise ValueError("state_schema_version must be > 0")
        modes = set(self.allowed_modes)
        if not modes or not modes.issubset(ALLOWED_EXECUTION_MODES):
            raise ValueError("allowed_modes contain unauthorized execution mode")
        if self.broker_submission_enabled:
            raise ValueError("executable manifest cannot enable broker submission")
        if self.live_authority:
            raise ValueError("executable manifest cannot grant LIVE authority")


@dataclass(frozen=True, slots=True)
class ExecutableInterfaceCertification:
    certification_id: str
    strategy_id: str
    strategy_version: str
    source_class: str
    executable_sha256: str
    interface_certification_sha256: str
    implementation_source_sha256: str
    config_sha256: str
    campaign_fingerprint: str
    allowed_modes: tuple[str, ...]
    deterministic_replay: bool
    restart_equivalent: bool
    duplicate_or_nonmonotonic_rejected: bool
    no_broker_boundary: bool
    test_event_count: int
    certified_at: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _hash(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _factory_source_hash(factory: Callable[[], ExecutableStrategy]) -> str:
    try:
        instance = factory()
    except Exception as exc:
        raise ExecutableStrategyError("IMPLEMENTATION_FACTORY_FAILED") from exc
    source_path = inspect.getsourcefile(type(instance))
    if not source_path:
        raise ExecutableStrategyError("IMPLEMENTATION_SOURCE_PATH_UNAVAILABLE")
    path = Path(source_path)
    if not path.is_file():
        raise ExecutableStrategyError("IMPLEMENTATION_SOURCE_FILE_MISSING")
    return sha256(path.read_bytes()).hexdigest()


def _intent_payload(intent: object | None) -> object:
    if intent is None:
        return None
    if not isinstance(intent, OrderRequest):
        raise ExecutableStrategyError("STRATEGY_OUTPUT_NOT_ORDER_REQUEST")
    return {
        "strategy_id": str(intent.strategy_id),
        "symbol": intent.symbol,
        "side": intent.side.value,
        "quantity": str(intent.quantity),
        "order_type": intent.order_type.value,
        "decision_time": intent.decision_time.isoformat(),
        "reference_price": str(intent.reference_price),
        "limit_price": None if intent.limit_price is None else str(intent.limit_price),
        "reduce_only": intent.reduce_only,
        "order_id": str(intent.order_id),
    }


def _run(factory: Callable[[], ExecutableStrategy], events: Iterable[object]) -> list[object]:
    strategy = factory()
    output: list[object] = []
    for event in events:
        output.append(_intent_payload(strategy.on_event(event)))
    return output


class ExecutableInterfaceCertificationLedger:
    """Immutable certification records for executable strategy/runtime parity.

    This ledger certifies software/interface mechanics only. It does not certify
    research profitability, admit R1.3 data, or grant PAPER/LIVE authority.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS executable_interface_certifications(
                    certification_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    source_class TEXT NOT NULL,
                    executable_sha256 TEXT NOT NULL,
                    interface_certification_sha256 TEXT NOT NULL UNIQUE,
                    implementation_source_sha256 TEXT NOT NULL,
                    config_sha256 TEXT NOT NULL,
                    campaign_fingerprint TEXT NOT NULL,
                    allowed_modes_json TEXT NOT NULL,
                    deterministic_replay INTEGER NOT NULL,
                    restart_equivalent INTEGER NOT NULL,
                    duplicate_or_nonmonotonic_rejected INTEGER NOT NULL,
                    no_broker_boundary INTEGER NOT NULL,
                    test_event_count INTEGER NOT NULL,
                    certified_at TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    UNIQUE(strategy_id,strategy_version,executable_sha256)
                );
                CREATE TRIGGER IF NOT EXISTS executable_cert_no_update
                BEFORE UPDATE ON executable_interface_certifications
                BEGIN SELECT RAISE(ABORT,'immutable executable certification'); END;
                CREATE TRIGGER IF NOT EXISTS executable_cert_no_delete
                BEFORE DELETE ON executable_interface_certifications
                BEGIN SELECT RAISE(ABORT,'immutable executable certification'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def certify(
        self,
        *,
        manifest: ExecutableStrategyManifest,
        factory: Callable[[], ExecutableStrategy],
        events: tuple[object, ...],
    ) -> ExecutableInterfaceCertification:
        if not events:
            raise ExecutableStrategyError("CERTIFICATION_EVENTS_REQUIRED")
        if manifest.broker_submission_enabled or manifest.live_authority:
            raise ExecutableStrategyError("AUTHORITY_BOUNDARY_INVALID")

        implementation_sha = _factory_source_hash(factory)
        manifest_payload = asdict(manifest)
        config_sha = _hash(
            {
                "parameters": manifest.parameters,
                "feature_ids": manifest.feature_ids,
                "instrument_ids": manifest.instrument_ids,
                "allowed_modes": manifest.allowed_modes,
                "warmup_events": manifest.warmup_events,
                "max_event_age_seconds": manifest.max_event_age_seconds,
                "state_schema_version": manifest.state_schema_version,
            }
        )
        executable_sha = _hash(
            {
                "manifest": manifest_payload,
                "implementation_source_sha256": implementation_sha,
                "config_sha256": config_sha,
            }
        )

        first = _run(factory, events)
        second = _run(factory, events)
        deterministic = first == second
        if not deterministic:
            raise ExecutableStrategyError("NONDETERMINISTIC_REPLAY")

        split = max(1, len(events) // 2)
        baseline = factory()
        prefix: list[object] = []
        for event in events[:split]:
            prefix.append(_intent_payload(baseline.on_event(event)))
        state = baseline.export_state()

        resumed = factory()
        resumed.restore_state(state)
        resumed_suffix = [
            _intent_payload(resumed.on_event(event))
            for event in events[split:]
        ]
        restart_equivalent = prefix + resumed_suffix == first
        if not restart_equivalent:
            raise ExecutableStrategyError("RESTART_STATE_DIVERGENCE")

        monotonic_guard = factory()
        monotonic_guard.on_event(events[0])
        duplicate_rejected = False
        try:
            monotonic_guard.on_event(events[0])
        except (ValueError, ExecutableStrategyError):
            duplicate_rejected = True
        if not duplicate_rejected:
            raise ExecutableStrategyError("NONMONOTONIC_EVENT_NOT_REJECTED")

        no_broker = (
            manifest.broker_submission_enabled is False
            and manifest.live_authority is False
            and set(manifest.allowed_modes).issubset(ALLOWED_EXECUTION_MODES)
        )
        if not no_broker:
            raise ExecutableStrategyError("NO_BROKER_BOUNDARY_FAILED")

        certified_at = datetime.now().astimezone().isoformat()
        body = {
            "schema_id": "EQS-EXECUTABLE-INTERFACE-CERTIFICATION-V1",
            "strategy_id": manifest.strategy_id,
            "strategy_version": manifest.strategy_version,
            "source_class": manifest.source_class,
            "executable_sha256": executable_sha,
            "implementation_source_sha256": implementation_sha,
            "config_sha256": config_sha,
            "campaign_fingerprint": manifest.campaign_fingerprint,
            "allowed_modes": list(manifest.allowed_modes),
            "deterministic_replay": True,
            "restart_equivalent": True,
            "duplicate_or_nonmonotonic_rejected": True,
            "no_broker_boundary": True,
            "test_event_count": len(events),
            "certified_at": certified_at,
            "broker_submission_enabled": False,
            "live_authority": False,
        }
        cert_sha = _hash(body)
        cert_id = "exec-cert-" + cert_sha[:20]

        con = self._connect()
        try:
            with con:
                con.execute(
                    """
                    INSERT INTO executable_interface_certifications(
                        certification_id,strategy_id,strategy_version,source_class,
                        executable_sha256,interface_certification_sha256,
                        implementation_source_sha256,config_sha256,campaign_fingerprint,
                        allowed_modes_json,deterministic_replay,restart_equivalent,
                        duplicate_or_nonmonotonic_rejected,no_broker_boundary,
                        test_event_count,certified_at,manifest_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        cert_id,
                        manifest.strategy_id,
                        manifest.strategy_version,
                        manifest.source_class,
                        executable_sha,
                        cert_sha,
                        implementation_sha,
                        config_sha,
                        manifest.campaign_fingerprint,
                        json.dumps(list(manifest.allowed_modes)),
                        1, 1, 1, 1,
                        len(events),
                        certified_at,
                        json.dumps(manifest_payload, sort_keys=True),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ExecutableStrategyError("CERTIFICATION_ALREADY_EXISTS") from exc
        finally:
            con.close()

        return ExecutableInterfaceCertification(
            certification_id=cert_id,
            strategy_id=manifest.strategy_id,
            strategy_version=manifest.strategy_version,
            source_class=manifest.source_class,
            executable_sha256=executable_sha,
            interface_certification_sha256=cert_sha,
            implementation_source_sha256=implementation_sha,
            config_sha256=config_sha,
            campaign_fingerprint=manifest.campaign_fingerprint,
            allowed_modes=manifest.allowed_modes,
            deterministic_replay=True,
            restart_equivalent=True,
            duplicate_or_nonmonotonic_rejected=True,
            no_broker_boundary=True,
            test_event_count=len(events),
            certified_at=certified_at,
        )

    def get_by_hash(self, certification_sha256: str) -> ExecutableInterfaceCertification | None:
        if not _SHA256.fullmatch(certification_sha256):
            return None
        con = self._connect()
        try:
            row = con.execute(
                """
                SELECT * FROM executable_interface_certifications
                WHERE interface_certification_sha256=?
                """,
                (certification_sha256,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        return ExecutableInterfaceCertification(
            certification_id=row["certification_id"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            source_class=row["source_class"],
            executable_sha256=row["executable_sha256"],
            interface_certification_sha256=row["interface_certification_sha256"],
            implementation_source_sha256=row["implementation_source_sha256"],
            config_sha256=row["config_sha256"],
            campaign_fingerprint=row["campaign_fingerprint"],
            allowed_modes=tuple(json.loads(row["allowed_modes_json"])),
            deterministic_replay=bool(row["deterministic_replay"]),
            restart_equivalent=bool(row["restart_equivalent"]),
            duplicate_or_nonmonotonic_rejected=bool(row["duplicate_or_nonmonotonic_rejected"]),
            no_broker_boundary=bool(row["no_broker_boundary"]),
            test_event_count=int(row["test_event_count"]),
            certified_at=row["certified_at"],
        )

    def verify_binding(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        executable_sha256: str,
        interface_certification_sha256: str,
        require_source_class: str | None = None,
    ) -> bool:
        cert = self.get_by_hash(interface_certification_sha256)
        if cert is None:
            return False
        if require_source_class is not None and cert.source_class != require_source_class:
            return False
        return (
            cert.strategy_id == strategy_id
            and cert.strategy_version == strategy_version
            and cert.executable_sha256 == executable_sha256
            and cert.deterministic_replay
            and cert.restart_equivalent
            and cert.duplicate_or_nonmonotonic_rejected
            and cert.no_broker_boundary
            and "PAPER_CANARY" in cert.allowed_modes
        )


class DeterministicThresholdFixtureStrategy:
    """Mechanics-only fixture used to certify the executable interface contract."""

    def __init__(
        self,
        *,
        strategy_id: str,
        symbol: str,
        quantity: Decimal,
        entry_above: Decimal,
        exit_below: Decimal,
    ):
        self.strategy_uuid = uuid5(NAMESPACE_URL, strategy_id)
        self.symbol = symbol
        self.quantity = Decimal(quantity)
        self.entry_above = Decimal(entry_above)
        self.exit_below = Decimal(exit_below)
        self._last_time: datetime | None = None
        self._position = Decimal("0")
        self._event_index = 0

    def on_event(self, event: object) -> OrderRequest | None:
        if not isinstance(event, BarEvent):
            raise ValueError("fixture strategy accepts BarEvent only")
        if event.symbol != self.symbol:
            raise ValueError("unexpected instrument")
        if self._last_time is not None and event.timestamp <= self._last_time:
            raise ValueError("events must be strictly increasing")
        self._last_time = event.timestamp
        self._event_index += 1

        side: Side | None = None
        if self._position == 0 and event.close >= self.entry_above:
            side = Side.BUY
            self._position = self.quantity
        elif self._position > 0 and event.close <= self.exit_below:
            side = Side.SELL
            self._position = Decimal("0")
        if side is None:
            return None

        order_id = uuid5(
            NAMESPACE_URL,
            f"{self.strategy_uuid}:{event.timestamp.isoformat()}:{side.value}:{self._event_index}",
        )
        return OrderRequest(
            strategy_id=self.strategy_uuid,
            symbol=self.symbol,
            side=side,
            quantity=self.quantity,
            order_type=OrderType.MARKET,
            decision_time=event.timestamp,
            reference_price=event.close,
            reduce_only=(side is Side.SELL),
            order_id=order_id,
        )

    def export_state(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "last_time": None if self._last_time is None else self._last_time.isoformat(),
            "position": str(self._position),
            "event_index": self._event_index,
        }

    def restore_state(self, state: object) -> None:
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("invalid state schema")
        last_time = state.get("last_time")
        self._last_time = None if last_time is None else datetime.fromisoformat(str(last_time))
        self._position = Decimal(str(state.get("position")))
        self._event_index = int(state.get("event_index"))
