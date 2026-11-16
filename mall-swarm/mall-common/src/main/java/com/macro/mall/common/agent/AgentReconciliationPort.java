package com.macro.mall.common.agent;

import static com.macro.mall.common.agent.AgentBridgeContracts.*;

/** Recovery receives only this port, so it cannot call executeConfirmed after a crash. */
public interface AgentReconciliationPort {
    OperationResult queryOriginalOperation(ReconcileRequest request);
    OperationResult reconcileOrRejectAbsent(ReconcileRequest request);
}
