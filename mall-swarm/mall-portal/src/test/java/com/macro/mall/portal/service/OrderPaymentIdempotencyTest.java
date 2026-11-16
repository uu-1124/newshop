package com.macro.mall.portal.service;

import com.macro.mall.mapper.OmsOrderMapper;
import com.macro.mall.order.OrderResourceService;
import com.macro.mall.model.*;
import com.macro.mall.portal.dao.PortalOrderDao;
import com.macro.mall.portal.domain.OmsOrderDetail;
import com.macro.mall.portal.service.impl.OmsPortalOrderServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.List;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

class OrderPaymentIdempotencyTest {
    private final OmsPortalOrderServiceImpl service = new OmsPortalOrderServiceImpl();
    private final OmsOrderMapper orders = mock(OmsOrderMapper.class);
    private final PortalOrderDao dao = mock(PortalOrderDao.class);
    private final OrderResourceService resources = mock(OrderResourceService.class);
    @BeforeEach void setUp() {
        ReflectionTestUtils.setField(service, "orderResources", resources);
        ReflectionTestUtils.setField(service, "orderMapper", orders);
        ReflectionTestUtils.setField(service, "portalOrderDao", dao);
    }
    @Test void onlyPendingNonDeletedOrderCanAdvanceAndDeduct() {
        when(orders.updateByExampleSelective(any(), any())).thenReturn(1);
        OmsOrderDetail detail = new OmsOrderDetail();
        detail.setOrderItemList(List.of(new OmsOrderItem()));
        when(dao.getDetail(10L)).thenReturn(detail);
        when(resources.deductStock(detail.getOrderItemList())).thenReturn(1);
        assertEquals(1, service.paySuccess(10L, 1));
        ArgumentCaptor<OmsOrderExample> c = ArgumentCaptor.forClass(OmsOrderExample.class);
        verify(orders).updateByExampleSelective(any(), c.capture());
        var filters = c.getValue().getOredCriteria().get(0).getAllCriteria();
        assertTrue(filters.stream().anyMatch(x -> x.getCondition().equals("status =") && x.getValue().equals(0)));
        assertTrue(filters.stream().anyMatch(x -> x.getCondition().equals("delete_status =") && x.getValue().equals(0)));
        assertTrue(filters.stream().anyMatch(x -> x.getCondition().equals("id =") && x.getValue().equals(10L)));
        verify(resources, times(1)).deductStock(any());
    }
    private OmsOrder existing(int status, int deleted) {
        OmsOrder o = new OmsOrder();o.setStatus(status);o.setDeleteStatus(deleted);
        when(orders.selectByPrimaryKey(10L)).thenReturn(o);return o;
    }
    @Test void paidReplayDoesNotTouchStock() { existing(1, 0);assertEquals(0, service.paySuccess(10L, 1));verifyNoInteractions(dao, resources); }
    @Test void shippedReplayDoesNotTouchStock() { existing(2, 0);assertEquals(0, service.paySuccess(10L, 1));verifyNoInteractions(dao, resources); }
    @Test void completedReplayDoesNotTouchStock() { existing(3, 0);assertEquals(0, service.paySuccess(10L, 1));verifyNoInteractions(dao, resources); }
    @Test void closedOrderIsNotResurrected() { existing(4, 0);assertThrows(RuntimeException.class, () -> service.paySuccess(10L, 1));verifyNoInteractions(dao, resources); }
    @Test void deletedOrderCannotBePaid() { existing(1, 1);assertThrows(RuntimeException.class, () -> service.paySuccess(10L, 1));verifyNoInteractions(dao, resources); }
    @Test void missingOrderCannotBePaid() { assertThrows(RuntimeException.class, () -> service.paySuccess(10L, 1));verifyNoInteractions(dao, resources); }
}
