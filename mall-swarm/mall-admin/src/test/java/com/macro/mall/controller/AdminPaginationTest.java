package com.macro.mall.controller;

import com.github.pagehelper.Page;
import com.macro.mall.common.exception.GlobalExceptionHandler;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class AdminPaginationTest {
    static Stream<Arguments> endpoints() {
        return Stream.of(
                Arguments.of(CmsSubjectController.class, "/subject/list"),
                Arguments.of(OmsOrderController.class, "/order/list"),
                Arguments.of(OmsOrderReturnApplyController.class, "/returnApply/list"),
                Arguments.of(OmsOrderReturnReasonController.class, "/returnReason/list"),
                Arguments.of(PmsBrandController.class, "/brand/list"),
                Arguments.of(PmsProductAttributeCategoryController.class, "/productAttribute/category/list"),
                Arguments.of(PmsProductAttributeController.class, "/productAttribute/list/1"),
                Arguments.of(PmsProductCategoryController.class, "/productCategory/list/0"),
                Arguments.of(PmsProductController.class, "/product/list"),
                Arguments.of(SmsCouponController.class, "/coupon/list"),
                Arguments.of(SmsCouponHistoryController.class, "/couponHistory/list"),
                Arguments.of(SmsFlashPromotionController.class, "/flash/list"),
                Arguments.of(SmsFlashPromotionProductRelationController.class, "/flashProductRelation/list"),
                Arguments.of(SmsHomeAdvertiseController.class, "/home/advertise/list"),
                Arguments.of(SmsHomeBrandController.class, "/home/brand/list"),
                Arguments.of(SmsHomeNewProductController.class, "/home/newProduct/list"),
                Arguments.of(SmsHomeRecommendProductController.class, "/home/recommendProduct/list"),
                Arguments.of(SmsHomeRecommendSubjectController.class, "/home/recommendSubject/list"),
                Arguments.of(UmsAdminController.class, "/admin/list"),
                Arguments.of(UmsMenuController.class, "/menu/list/0"),
                Arguments.of(UmsResourceController.class, "/resource/list"),
                Arguments.of(UmsRoleController.class, "/role/list"));
    }

    private final List<Object> services = new ArrayList<>();

    private MockMvc controller(Class<?> type) throws Exception {
        services.clear();
        Object controller = type.getDeclaredConstructor().newInstance();
        for (var field : type.getDeclaredFields()) {
            if (!field.isAnnotationPresent(Autowired.class)) continue;
            Object service = mock(field.getType(), invocation -> {
                if (invocation.getMethod().getReturnType() == List.class) {
                    int pageNum = 1, pageSize = 5;
                    var params = invocation.getMethod().getParameters();
                    for (int i = 0; i < params.length; i++) {
                        if (params[i].getName().equals("pageNum")) pageNum = invocation.getArgument(i);
                        if (params[i].getName().equals("pageSize")) pageSize = invocation.getArgument(i);
                    }
                    return new Page<>(pageNum, pageSize);
                }
                return RETURNS_DEFAULTS.answer(invocation);
            });
            ReflectionTestUtils.setField(controller, field.getName(), service);
            services.add(service);
        }
        return MockMvcBuilders.standaloneSetup(controller)
                .setControllerAdvice(new GlobalExceptionHandler()).build();
    }

    private MockHttpServletRequestBuilder request(String path) {
        return get(path).param("type", "0").param("couponId", "1")
                .param("flashPromotionId", "1").param("flashPromotionSessionId", "1");
    }

    @ParameterizedTest(name = "{1}: invalid pagination")
    @MethodSource("endpoints")
    void invalidPaginationNeverQueriesTheService(Class<?> type, String path) throws Exception {
        MockMvc mvc = controller(type);
        for (String[] invalid : new String[][]{{"0", "5"}, {"-1", "5"}, {"1", "0"},
                {"1", "-1"}, {"1", "101"}, {"2147483647", "100"}}) {
            mvc.perform(request(path).param("pageNum", invalid[0]).param("pageSize", invalid[1]))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.code").value(404));
            services.forEach(service -> verifyNoInteractions(service));
        }
    }

    @ParameterizedTest(name = "{1}: defaults and valid boundaries")
    @MethodSource("endpoints")
    void defaultAndExplicitPaginationReachTheService(Class<?> type, String path) throws Exception {
        MockMvc mvc = controller(type);
        mvc.perform(request(path)).andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data.pageNum").value(1))
                .andExpect(jsonPath("$.data.pageSize").value(5));
        mvc.perform(request(path).param("pageNum", "2").param("pageSize", "100"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data.pageNum").value(2))
                .andExpect(jsonPath("$.data.pageSize").value(100));
    }
}
