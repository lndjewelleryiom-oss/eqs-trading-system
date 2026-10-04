from __future__ import annotations

from quant_system.execution.broker import BrokerGateway, SubmissionResult
from quant_system.execution.models import OrderRequest

from .controller import CommissioningController, LiveLevel


class CommissionedSubmissionBoundary:
    """Binds venue submission to commissioning state.

    The wrapped BrokerGateway may be technically capable of transmission, but this
    boundary refuses to call it while the commissioning controller is LIVE_0.
    It contains no credential provisioning or authorization escalation capability.
    """

    def __init__(self, controller: CommissioningController, gateway: BrokerGateway):
        self.controller = controller
        self.gateway = gateway

    def submit(self, order: OrderRequest, *, lineage: dict[str, object] | None = None) -> SubmissionResult:
        if lineage and lineage.get("infrastructure_boundary_fingerprint"):
            return SubmissionResult(False, order.order_id, None, "INFRASTRUCTURE_ONLY_DATASET_BLOCKED")
        if self.controller.state == LiveLevel.LIVE_0:
            return SubmissionResult(False, order.order_id, None, "COMMISSIONING_LIVE_0")
        return self.gateway.submit(order, lineage=lineage)
