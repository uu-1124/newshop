package com.macro.mall.service;

import com.macro.mall.dao.OmsOrderOperateHistoryDao;
import com.macro.mall.model.OmsOrderOperateHistory;
import com.macro.mall.order.OrderResourceService;
import com.macro.mall.service.impl.OmsOrderServiceImpl;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.test.util.ReflectionTestUtils;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class OrderCloseConsistencyTest {
    private final OrderResourceService resources = mock(OrderResourceService.class);
    private final OmsOrderOperateHistoryDao histories = mock(OmsOrderOperateHistoryDao.class);
    private final OmsOrderServiceImpl service = new OmsOrderServiceImpl();

    OrderCloseConsistencyTest() {
        ReflectionTestUtils.setField(service, "orderResources", resources);
        ReflectionTestUtils.setField(service, "orderOperateHistoryDao", histories);
    }

    @Test void onlySuccessfulTransitionsProduceHistoryAndDuplicatesAreIgnored() {
        when(resources.closePendingOrder(1L)).thenReturn(true);
        assertEquals(1, service.close(List.of(2L, 1L, 1L), "test"));
        ArgumentCaptor<List<OmsOrderOperateHistory>> captured = ArgumentCaptor.forClass(List.class);
        verify(histories).insertList(captured.capture());
        assertEquals(1L, captured.getValue().get(0).getOrderId());
        assertEquals(4, captured.getValue().get(0).getOrderStatus());
        verify(resources, times(1)).closePendingOrder(1L);
    }

    @Test void paidClosedOrMissingOrdersProduceNoHistory() {
        assertEquals(0, service.close(List.of(1L, 2L), "test"));
        verifyNoInteractions(histories);
    }

    @Test void failedResourceReleasePropagatesToTransactionalCaller() {
        when(resources.closePendingOrder(1L)).thenThrow(new IllegalStateException("stock"));
        assertThrows(IllegalStateException.class, () -> service.close(List.of(1L), "test"));
        verifyNoInteractions(histories);
    }
}
