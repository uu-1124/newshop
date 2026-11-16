package com.macro.mall.common.agent;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.UUID;

/** Internal bridge contracts. Controllers must authenticate and authorize before constructing contexts.
 * No Spring bean, HTTP route, or transaction execution is enabled by these types.
 */
public final class AgentBridgeContracts {
    private AgentBridgeContracts() { }

    public record MemberContext(long memberId, UUID requestId) { }
    public record StaffContext(long staffId, Set<String> permissions, UUID requestId) { }
    public record Line(long productId, long skuId, int quantity) { }
    public record QuoteRequest(String mode, List<Line> lines, List<Long> cartItemIds,
                               Long addressId, Long couponId, Integer points) { }
    public record Quote(UUID quoteId, String canonicalSha256, BigDecimal payable,
                        Instant expiresAt, String ruleVersion) { }
    public enum ActionKind { ADD_CART, CREATE_ORDER, CANCEL_ORDER, CREATE_RETURN }
    public enum OperationState { COMMITTED, REJECTED, NOT_COMMITTED }
    public record ConfirmedOperation(UUID actionId, String operationNo, ActionKind kind,
                                     String canonicalSha256, UUID quoteId, Instant expiresAt) { }
    public record OperationResult(String operationNo, OperationState state,
                                  String canonicalSha256, String businessId, String errorCode) { }
    public record ReconcileRequest(long memberId, String operationNo, String canonicalSha256) { }
    public record ReturnRequest(long orderItemId, int quantity, String reason, String ruleVersion) { }
    public record StaffTransition(UUID requestId, long resourceId, long expectedVersion,
                                  String transition, String reason) { }
    public record LogisticsEvent(UUID requestId, long shipmentId, long expectedVersion,
                                 String stage, Instant occurredAt, String description) { }
}
