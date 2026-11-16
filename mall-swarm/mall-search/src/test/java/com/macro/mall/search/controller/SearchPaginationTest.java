package com.macro.mall.search.controller;

import com.macro.mall.common.exception.GlobalExceptionHandler;
import com.macro.mall.search.domain.EsProduct;
import com.macro.mall.search.service.EsProductService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.data.domain.PageImpl;
import org.springframework.data.domain.PageRequest;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import java.util.List;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class SearchPaginationTest {
    private final EsProductService service = mock(EsProductService.class);
    private MockMvc mvc;

    @BeforeEach void setup() {
        var controller = new EsProductController();
        ReflectionTestUtils.setField(controller, "esProductService", service);
        mvc = MockMvcBuilders.standaloneSetup(controller).setControllerAdvice(new GlobalExceptionHandler()).build();
    }

    @ParameterizedTest
    @CsvSource({"-1,5", "0,0", "0,-1", "0,101", "100,100", "2147483647,100"})
    void invalidPaginationNeverReachesSearchEngine(String page, String size) throws Exception {
        for (String endpoint : List.of("/esProduct/search/simple", "/esProduct/search", "/esProduct/recommend/1")) {
            mvc.perform(get(endpoint).param("pageNum", page).param("pageSize", size))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.code").value(404));
        }
        verifyNoInteractions(service);
    }

    @ParameterizedTest @ValueSource(ints = {0, 99})
    void zeroBasedPagesAndLastResultWindowRemainValid(int page) throws Exception {
        when(service.search(nullable(String.class), eq(page), eq(100)))
                .thenReturn(new PageImpl<EsProduct>(List.of(), PageRequest.of(page, 100), 10000));
        mvc.perform(get("/esProduct/search/simple").param("pageNum", "" + page).param("pageSize", "100"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.code").value(200));
        verify(service).search(null, page, 100);
    }
}
