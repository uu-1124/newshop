package com.macro.mall.portal.controller;

import com.macro.mall.common.exception.ApiException;
import com.macro.mall.common.exception.GlobalExceptionHandler;
import com.macro.mall.portal.domain.OmsOrderDetail;
import com.macro.mall.portal.service.OmsPortalOrderService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class DemoPaymentOwnershipTest {
    private final OmsPortalOrderService service = mock(OmsPortalOrderService.class);
    private MockMvc mvc;

    @BeforeEach void setUp() {
        var controller = new OmsPortalOrderController();
        ReflectionTestUtils.setField(controller, "portalOrderService", service);
        mvc = MockMvcBuilders.standaloneSetup(controller)
                .setControllerAdvice(new GlobalExceptionHandler()).build();
    }

    @ParameterizedTest
    @ValueSource(longs = {13L, 999999999L})
    void rejectedOwnershipOrMissingOrderCannotReachPayment(Long orderId) throws Exception {
        when(service.detail(orderId)).thenThrow(new ApiException("订单不存在"));
        mvc.perform(post("/order/paySuccess").param("orderId", orderId.toString()).param("payType", "1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(500))
                .andExpect(jsonPath("$.message").value("订单不存在"));
        verify(service, never()).paySuccess(anyLong(), anyInt());
    }

    @Test void ownedOrderRetainsTheDemoPaymentResponse() throws Exception {
        when(service.detail(77L)).thenReturn(new OmsOrderDetail());
        when(service.paySuccess(77L, 1)).thenReturn(1);
        mvc.perform(post("/order/paySuccess").param("orderId", "77").param("payType", "1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data").value(1))
                .andExpect(jsonPath("$.message").value("支付成功"));
        var sequence = inOrder(service);
        sequence.verify(service).detail(77L);
        sequence.verify(service).paySuccess(77L, 1);
    }

    @Test void ownedPaymentReplayRetainsTheIdempotentResponse() throws Exception {
        when(service.detail(77L)).thenReturn(new OmsOrderDetail());
        when(service.paySuccess(77L, 1)).thenReturn(0);
        mvc.perform(post("/order/paySuccess").param("orderId", "77").param("payType", "1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data").value(0));
        verify(service).detail(77L);
    }
}
