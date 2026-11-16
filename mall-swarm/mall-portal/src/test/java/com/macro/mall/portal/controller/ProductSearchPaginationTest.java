package com.macro.mall.portal.controller;

import com.github.pagehelper.Page;
import com.macro.mall.model.PmsProduct;
import com.macro.mall.common.exception.GlobalExceptionHandler;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import com.macro.mall.portal.service.PmsPortalProductService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class ProductSearchPaginationTest {
    private final PmsPortalProductService service = mock(PmsPortalProductService.class);
    private MockMvc mvc;

    @BeforeEach void setUp() {
        PmsPortalProductController controller = new PmsPortalProductController();
        ReflectionTestUtils.setField(controller, "portalProductService", service);
        when(service.search(nullable(String.class), nullable(Long.class), nullable(Long.class), anyInt(), anyInt(), anyInt()))
                .thenAnswer(call -> {
                    Page<PmsProduct> page = new Page<>(call.getArgument(3, Integer.class), call.getArgument(4, Integer.class));
                    page.setTotal(12);
                    PmsProduct product = new PmsProduct();
                    product.setId(26L);
                    page.add(product);
                    return page;
                });
        mvc = MockMvcBuilders.standaloneSetup(controller).setControllerAdvice(new GlobalExceptionHandler()).build();
    }

    @Test void omittedPageStartsAtOneAndRetainsFilters() throws Exception {
        mvc.perform(get("/product/search").param("brandId", "6").param("productCategoryId", "19"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data.pageNum").value(1))
                .andExpect(jsonPath("$.data.pageSize").value(5))
                .andExpect(jsonPath("$.data.total").value(12))
                .andExpect(jsonPath("$.data.list[0].id").value(26));
        verify(service).search(null, 6L, 19L, 1, 5, 0);
    }

    @Test void explicitPageSizeKeywordAndSortArePreserved() throws Exception {
        mvc.perform(get("/product/search").param("pageNum", "2").param("pageSize", "2")
                        .param("keyword", "phone").param("sort", "4"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.pageNum").value(2))
                .andExpect(jsonPath("$.data.pageSize").value(2));
        verify(service).search("phone", null, null, 2, 2, 4);
    }

    @ParameterizedTest
    @CsvSource({"0,5", "-1,5", "1,0", "1,-1", "1,101", "2147483647,100"})
    void invalidPaginationIsRejectedBeforeQuery(String page, String size) throws Exception {
        mvc.perform(get("/product/search").param("pageNum", page).param("pageSize", size))
                .andExpect(status().isOk()).andExpect(jsonPath("$.code").value(404));
        verify(service, never()).search(any(), any(), any(), any(), any(), any());
    }
}
