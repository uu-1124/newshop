package com.macro.mall.portal.service;

import com.macro.mall.mapper.OmsOrderItemMapper;
import com.macro.mall.mapper.OmsOrderReturnApplyMapper;
import com.macro.mall.model.*;
import com.macro.mall.portal.dao.PortalOrderDao;
import com.macro.mall.portal.domain.OmsOrderReturnApplyParam;
import com.macro.mall.portal.service.impl.OmsPortalOrderReturnApplyServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.test.util.ReflectionTestUtils;

import java.math.BigDecimal;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

class ReturnApplicationSecurityTest {
    private final OmsPortalOrderReturnApplyServiceImpl service = new OmsPortalOrderReturnApplyServiceImpl();
    private final PortalOrderDao orders = mock(PortalOrderDao.class);
    private final OmsOrderItemMapper items = mock(OmsOrderItemMapper.class);
    private final OmsOrderReturnApplyMapper applications = mock(OmsOrderReturnApplyMapper.class);
    private final UmsMemberService members = mock(UmsMemberService.class);
    private OmsOrder order;
    private OmsOrderItem item;
    private OmsOrderReturnApplyParam param;

    @BeforeEach void setUp() {
        ReflectionTestUtils.setField(service, "orderDao", orders);
        ReflectionTestUtils.setField(service, "orderItemMapper", items);
        ReflectionTestUtils.setField(service, "returnApplyMapper", applications);
        ReflectionTestUtils.setField(service, "memberService", members);
        UmsMember member = new UmsMember(); member.setId(1L);
        when(members.getCurrentMember()).thenReturn(member);
        order = new OmsOrder(); order.setId(10L); order.setMemberId(1L);
        order.setDeleteStatus(0); order.setStatus(2); order.setOrderSn("server-order");
        order.setMemberUsername("owner");
        when(orders.lockOrderForReturn(10L)).thenReturn(order);
        item = new OmsOrderItem(); item.setProductId(20L); item.setProductQuantity(3);
        item.setProductPrice(new BigDecimal("10.00")); item.setRealAmount(new BigDecimal("8.00"));
        item.setProductName("server-product"); item.setProductAttr("server-attr");
        item.setProductPic("server-pic"); item.setProductBrand("server-brand");
        when(items.selectByExample(any())).thenReturn(List.of(item));
        when(applications.selectByExample(any())).thenReturn(List.of());
        when(applications.insert(any())).thenReturn(1);
        param = new OmsOrderReturnApplyParam(); param.setOrderId(10L);
        param.setProductId(20L); param.setProductCount(2);
        param.setOrderSn("forged"); param.setMemberUsername("forged");
        param.setProductPrice(new BigDecimal("9999")); param.setProductRealPrice(new BigDecimal("9999"));
        param.setProductName("forged"); param.setProductAttr("forged");
        param.setProductPic("forged"); param.setProductBrand("forged");
        param.setReturnName("contact"); param.setReason("reason");
    }
    private void rejects() {
        assertThrows(RuntimeException.class, () -> service.create(param));
        verify(applications, never()).insert(any());
    }
    private OmsOrderReturnApply prior(int status, Integer quantity) {
        OmsOrderReturnApply a = new OmsOrderReturnApply(); a.setStatus(status); a.setProductCount(quantity); return a;
    }
    @Test void serverSnapshotOverridesForgedFieldsAndKeepsUnitPrice() {
        assertEquals(1, service.create(param));
        ArgumentCaptor<OmsOrderReturnApply> c = ArgumentCaptor.forClass(OmsOrderReturnApply.class);
        verify(applications).insert(c.capture()); OmsOrderReturnApply a = c.getValue();
        assertEquals("server-order", a.getOrderSn()); assertEquals("owner", a.getMemberUsername());
        assertEquals(item.getProductPrice(), a.getProductPrice());
        assertEquals(new BigDecimal("8.00"), a.getProductRealPrice());
        assertEquals("server-product", a.getProductName()); assertEquals("server-attr", a.getProductAttr());
        assertEquals("server-pic", a.getProductPic()); assertEquals("server-brand", a.getProductBrand());
        assertEquals(2, a.getProductCount()); assertEquals("contact", a.getReturnName());
        assertEquals("reason", a.getReason()); assertEquals(0, a.getStatus());
        assertNotNull(a.getCreateTime()); assertNull(a.getReturnAmount());
    }
    @Test void anotherMemberCannotApply() { order.setMemberId(2L); rejects(); verifyNoInteractions(items); }
    @Test void missingOrderCannotApply() { when(orders.lockOrderForReturn(10L)).thenReturn(null); rejects(); }
    @Test void deletedOrderCannotApply() { order.setDeleteStatus(1); rejects(); }
    @Test void unpaidUnshippedAndClosedOrdersCannotApply() {
        for (int status : List.of(0, 1, 4)) { order.setStatus(status); rejects(); }
    }
    @Test void completedOrderCanApply() { order.setStatus(3); assertEquals(1, service.create(param)); }
    @Test void invalidQuantityCannotApply() {
        for (Integer q : new Integer[]{null, 0, -1, 4}) { param.setProductCount(q); rejects(); }
    }
    @Test void missingIdentifiersCannotApply() { param.setOrderId(null); rejects(); verifyNoInteractions(orders); }
    @Test void productOutsideOrderCannotApply() { when(items.selectByExample(any())).thenReturn(List.of()); rejects(); }
    @Test void ambiguousOrderLinesCannotApply() { when(items.selectByExample(any())).thenReturn(List.of(item, item)); rejects(); }
    @Test void pendingProcessingAndCompletedReturnsConsumeQuantity() {
        for (int status : List.of(0, 1, 2)) {
            when(applications.selectByExample(any())).thenReturn(List.of(prior(status, 2))); rejects();
        }
    }
    @Test void rejectedReturnDoesNotConsumeQuantity() {
        when(applications.selectByExample(any())).thenReturn(List.of(prior(3, 3), prior(0, 1)));
        assertEquals(1, service.create(param));
    }
    @Test void invalidHistoricalDataRequiresManualHandling() {
        when(applications.selectByExample(any())).thenReturn(List.of(prior(0, null))); rejects();
    }
    @Test void invalidServerAmountCannotApply() { item.setRealAmount(new BigDecimal("-1")); rejects(); }
}
