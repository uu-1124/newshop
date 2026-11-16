package com.macro.mall.portal.controller;

import com.macro.mall.portal.component.CancelOrderReceiver;
import com.macro.mall.portal.component.OrderTimeOutCancelTask;
import com.macro.mall.portal.service.OmsPortalOrderService;
import com.macro.mall.portal.service.OrderBusinessOutbox;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpMethod;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class GlobalOrderCancellationEntryTest {
    private final OmsPortalOrderService service = mock(OmsPortalOrderService.class);

    @ParameterizedTest
    @ValueSource(strings = {"GET", "POST", "PUT", "DELETE"})
    void globalCancellationCannotBeTriggeredOverHttp(String method) throws Exception {
        var controller = new OmsPortalOrderController();
        ReflectionTestUtils.setField(controller, "portalOrderService", service);
        var mvc = MockMvcBuilders.standaloneSetup(controller).build();
        mvc.perform(request(HttpMethod.valueOf(method), "/order/cancelTimeOutOrder"))
                .andExpect(status().isNotFound());
        verifyNoInteractions(service);
    }

    @Test void scheduledCancellationStillCallsTheInternalService() {
        var task = new OrderTimeOutCancelTask();
        ReflectionTestUtils.setField(task, "portalOrderService", service);
        when(service.cancelTimeOutOrder()).thenReturn(0);
        ReflectionTestUtils.invokeMethod(task, "cancelTimeOutOrder");
        verify(service).cancelTimeOutOrder();
    }

    @Test void queueCancellationUsesTheDurableOutboxConsumer() {
        var receiver = new CancelOrderReceiver();
        var outbox = mock(OrderBusinessOutbox.class);
        ReflectionTestUtils.setField(receiver, "outbox", outbox);
        receiver.handle(77L);
        verify(outbox).consume(77L);
        verifyNoInteractions(service);
    }
}
