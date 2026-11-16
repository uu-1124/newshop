package com.macro.mall.common.agent;

import static com.macro.mall.common.agent.AgentBridgeContracts.*;

/** M18 extension point. The future implementation owns the shared calculator and MySQL transaction.
 * This port must never be registered as an LLM tool; only a real member confirmation can execute.
 */
public interface AgentTransactionPort {
    Quote prepareQuote(MemberContext context, QuoteRequest request);
    OperationResult executeConfirmed(MemberContext context, ConfirmedOperation operation);
}
