package com.macro.mall.portal.service;

import com.macro.mall.common.exception.ApiException;
import com.macro.mall.mapper.*;
import com.macro.mall.model.*;
import com.macro.mall.portal.dao.PortalProductDao;
import com.macro.mall.portal.service.impl.PmsPortalProductServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.NullSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.test.util.ReflectionTestUtils;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

class ProductDetailValidationTest {
    private final PmsPortalProductServiceImpl service = new PmsPortalProductServiceImpl();
    private final PmsProductMapper products = mock(PmsProductMapper.class);
    private final PmsBrandMapper brands = mock(PmsBrandMapper.class);
    private final PmsProductAttributeMapper attributes = mock(PmsProductAttributeMapper.class);
    private final PmsProductAttributeValueMapper values = mock(PmsProductAttributeValueMapper.class);
    private final PmsSkuStockMapper stocks = mock(PmsSkuStockMapper.class);
    private final PmsProductLadderMapper ladders = mock(PmsProductLadderMapper.class);
    private final PmsProductFullReductionMapper reductions = mock(PmsProductFullReductionMapper.class);
    private final PortalProductDao coupons = mock(PortalProductDao.class);
    private final List<PmsProductAttribute> attributeList = List.of(new PmsProductAttribute());
    private final List<PmsProductAttributeValue> valueList = List.of(new PmsProductAttributeValue());
    private final List<PmsSkuStock> stockList = List.of(new PmsSkuStock());
    private final List<PmsProductLadder> ladderList = List.of(new PmsProductLadder());
    private final List<PmsProductFullReduction> reductionList = List.of(new PmsProductFullReduction());
    private final List<SmsCoupon> couponList = List.of(new SmsCoupon());
    private final PmsBrand brand = new PmsBrand();
    private PmsProduct product;

    @BeforeEach void setUp() {
        Map<String, Object> dependencies = Map.of(
                "productMapper", products, "brandMapper", brands,
                "productAttributeMapper", attributes, "productAttributeValueMapper", values,
                "skuStockMapper", stocks, "productLadderMapper", ladders,
                "productFullReductionMapper", reductions, "portalProductDao", coupons);
        dependencies.forEach((field, dependency) -> ReflectionTestUtils.setField(service, field, dependency));
        product = new PmsProduct();
        product.setId(26L);
        product.setBrandId(6L);
        product.setProductCategoryId(19L);
        product.setProductAttributeCategoryId(3L);
        product.setDeleteStatus(0);
        product.setPublishStatus(1);
        product.setPromotionType(0);
        attributeList.get(0).setId(7L);
        when(products.selectByPrimaryKey(26L)).thenReturn(product);
        when(brands.selectByPrimaryKey(6L)).thenReturn(brand);
        when(attributes.selectByExample(any())).thenReturn(attributeList);
        when(values.selectByExample(any())).thenReturn(valueList);
        when(stocks.selectByExample(any())).thenReturn(stockList);
        when(ladders.selectByExample(any())).thenReturn(ladderList);
        when(reductions.selectByExample(any())).thenReturn(reductionList);
        when(coupons.getAvailableCouponList(26L, 19L)).thenReturn(couponList);
    }

    private void assertUnavailable(Long id) {
        ApiException error = assertThrows(ApiException.class, () -> service.detail(id));
        assertEquals("商品不存在或已下架", error.getMessage());
        verifyNoInteractions(brands, attributes, values, stocks, ladders, reductions, coupons);
    }

    @Test void missingProductReturnsBusinessErrorBeforeRelatedQueries() {
        assertUnavailable(999999999L);
    }

    @ParameterizedTest
    @NullSource
    @ValueSource(ints = {1})
    void deletedOrUnknownDeletionStateIsNotPublic(Integer state) {
        product.setDeleteStatus(state);
        // Deleted demo products can also have incomplete attribute data.
        product.setProductAttributeCategoryId(null);
        assertUnavailable(26L);
    }

    @ParameterizedTest
    @NullSource
    @ValueSource(ints = {0, 2})
    void unpublishedOrUnknownPublicationStateIsNotPublic(Integer state) {
        product.setPublishStatus(state);
        assertUnavailable(26L);
    }

    @ParameterizedTest
    @ValueSource(ints = {0, 3, 4})
    void publishedProductRetainsDetailsAndPromotionBranches(int promotionType) {
        product.setPromotionType(promotionType);
        var result = service.detail(26L);
        assertSame(product, result.getProduct());
        assertSame(brand, result.getBrand());
        assertSame(attributeList, result.getProductAttributeList());
        assertSame(valueList, result.getProductAttributeValueList());
        assertSame(stockList, result.getSkuStockList());
        assertSame(couponList, result.getCouponList());
        if (promotionType == 3) {
            assertSame(ladderList, result.getProductLadderList());
        } else {
            verifyNoInteractions(ladders);
        }
        if (promotionType == 4) {
            assertSame(reductionList, result.getProductFullReductionList());
        } else {
            verifyNoInteractions(reductions);
        }
    }
}
