package com.macro.mall.common.agent;

import static com.macro.mall.common.agent.AgentBridgeContracts.*;

/** M19 extension point. An implementation must enforce item ownership and staff permissions.
 * No implementation is installed until the return rules and idempotent transactions are verified.
 */
public interface AgentAfterSalePort {
    Quote prepareReturn(MemberContext context, ReturnRequest request);
    OperationResult transitionReturn(StaffContext context, StaffTransition request);
    OperationResult appendLogisticsEvent(StaffContext context, LogisticsEvent request);
}
