package com.macro.mall.portal.service;

import com.macro.mall.mapper.OmsOrderMapper;
import com.macro.mall.mapper.OmsOrderItemMapper;
import com.macro.mall.model.*;
import com.macro.mall.portal.controller.OmsPortalOrderController;
import com.macro.mall.portal.service.impl.OmsPortalOrderServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class OrderOwnershipSecurityTest {
    private final OmsPortalOrderServiceImpl service = new OmsPortalOrderServiceImpl();
    private final OmsOrderMapper orders = mock(OmsOrderMapper.class);
    private final OmsOrderItemMapper items = mock(OmsOrderItemMapper.class);
    private final UmsMemberService members = mock(UmsMemberService.class);
    private OmsOrder order;

    @BeforeEach void setUp() {
        ReflectionTestUtils.setField(service, "orderMapper", orders);
        ReflectionTestUtils.setField(service, "orderItemMapper", items);
        ReflectionTestUtils.setField(service, "memberService", members);
        UmsMember member = new UmsMember(); member.setId(1L);
        when(members.getCurrentMember()).thenReturn(member);
        order = new OmsOrder(); order.setId(10L); order.setMemberId(1L); order.setDeleteStatus(0);
        when(orders.selectByPrimaryKey(10L)).thenReturn(order);
        when(items.selectByExample(any())).thenReturn(List.of());
    }

    @Test void ownerCanRead() { assertEquals(10L, service.detail(10L).getId()); }
    @Test void otherMemberCannotReadItems() {
        order.setMemberId(2L);
        assertThrows(RuntimeException.class, () -> service.detail(10L));
        verifyNoInteractions(items);
    }
    @Test void deletedOrderCannotBeRead() {
        order.setDeleteStatus(1);
        assertThrows(RuntimeException.class, () -> service.detail(10L));
        verifyNoInteractions(items);
    }
    @Test void missingOrderIsRejected() {
        assertThrows(RuntimeException.class, () -> service.detail(99L));
        verifyNoInteractions(items);
    }
    @Test void bothPublicCancellationRoutesCheckOwnershipBeforeSideEffects() {
        OmsPortalOrderService dependency = mock(OmsPortalOrderService.class);
        OmsPortalOrderController controller = new OmsPortalOrderController();
        ReflectionTestUtils.setField(controller, "portalOrderService", dependency);
        when(dependency.detail(10L)).thenThrow(new IllegalArgumentException("not owned"));
        assertThrows(RuntimeException.class, () -> controller.cancelUserOrder(10L));
        assertThrows(RuntimeException.class, () -> controller.cancelOrder(10L));
        verify(dependency, never()).cancelOrder(anyLong());
        verify(dependency, never()).sendDelayMessageCancelOrder(anyLong());
    }
    @Test void ownedOrderCanStillUseBothCancellationRoutes() {
        OmsPortalOrderService dependency = mock(OmsPortalOrderService.class);
        OmsPortalOrderController controller = new OmsPortalOrderController();
        ReflectionTestUtils.setField(controller, "portalOrderService", dependency);
        controller.cancelUserOrder(10L);
        controller.cancelOrder(10L);
        verify(dependency).cancelOrder(10L);
        verify(dependency).sendDelayMessageCancelOrder(10L);
    }
}
