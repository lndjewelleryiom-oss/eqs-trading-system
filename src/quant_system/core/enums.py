from enum import StrEnum


class StrategyState(StrEnum):
    PROPOSED = "PROPOSED"
    EXPERIMENTAL = "EXPERIMENTAL"
    REJECTED = "REJECTED"
    OOS_VALIDATED = "OOS_VALIDATED"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE_1 = "LIVE_1"
    LIVE_2 = "LIVE_2"
    LIVE_3 = "LIVE_3"
    LIVE_4 = "LIVE_4"
    WATCH = "WATCH"
    REDUCED = "REDUCED"
    PAUSED = "PAUSED"
    RESEARCH = "RESEARCH"
    RETIRED = "RETIRED"


class ComponentStatus(StrEnum):
    NOT_STARTED = "NOT STARTED"
    IN_PROGRESS = "IN PROGRESS"
    BLOCKED = "BLOCKED"
    TESTING = "TESTING"
    PASSED = "PASSED"
    LIVE = "LIVE"
    FAILED = "FAILED"


class RiskAction(StrEnum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    BLOCK = "BLOCK"
    HALT = "HALT"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
