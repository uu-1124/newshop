package com.macro.mall.portal.service;

import com.macro.mall.model.*;
import com.macro.mall.portal.dao.PortalProductDao;
import com.macro.mall.portal.domain.CartPromotionItem;
import com.macro.mall.portal.domain.PromotionProduct;
import com.macro.mall.portal.service.impl.OmsPromotionServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.math.BigDecimal;
import java.util.List;
import java.util.ArrayList;
import java.util.Arrays;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.Mockito.*;

class OmsPromotionPriceSecurityTest {
    private final PortalProductDao dao = mock(PortalProductDao.class);
    private final OmsPromotionServiceImpl service = new OmsPromotionServiceImpl();
    private PromotionProduct product;
    private OmsCartItem cart;

    @BeforeEach
    void setUp() {
        ReflectionTestUtils.setField(service, "portalProductDao", dao);
        product = new PromotionProduct();
        product.setId(1L);
        product.setGiftPoint(0);
        product.setGiftGrowth(0);
        PmsSkuStock sku = new PmsSkuStock();
        sku.setId(2L);
        sku.setPrice(new BigDecimal("10.00"));
        sku.setPromotionPrice(new BigDecimal("8.00"));
        sku.setStock(20);
        sku.setLockStock(1);
        product.setSkuStockList(List.of(sku));
        product.setProductLadderList(new ArrayList<>());
        product.setProductFullReductionList(new ArrayList<>());
        when(dao.getPromotionProductList(anyList())).thenReturn(List.of(product));
        cart = new OmsCartItem();
        cart.setProductId(1L);
        cart.setProductSkuId(2L);
        cart.setQuantity(1);
        cart.setPrice(new BigDecimal("0.01"));
    }

    private void check(int type, String discount) {
        product.setPromotionType(type);
        CartPromotionItem priced = service.calcCartPromotion(List.of(cart)).get(0);
        assertEquals(0, new BigDecimal("10.00").compareTo(priced.getPrice()));
        assertEquals(0, new BigDecimal(discount).compareTo(priced.getReduceAmount()));
        assertEquals(19, priced.getRealStock());
        assertEquals(new BigDecimal("0.01"), cart.getPrice(), "do not mutate the cart/cache object");
    }

    @Test void noPromotionUsesServerPrice() { check(0, "0"); }
    @Test void singlePromotionStillUsesServerPrice() { check(1, "2"); }
    @Test void ladderFallbackUsesServerPrice() { check(3, "0"); }
    @Test void reductionFallbackUsesServerPrice() { check(4, "0"); }
    @Test void ladderDiscountUsesServerPrice() {
        PmsProductLadder ladder = new PmsProductLadder();
        ladder.setCount(1);
        ladder.setDiscount(new BigDecimal("0.90"));
        product.setProductLadderList(new ArrayList<>(List.of(ladder)));
        check(3, "1");
    }
    @Test void fullReductionUsesServerPrice() {
        PmsProductFullReduction rule = new PmsProductFullReduction();
        rule.setFullPrice(new BigDecimal("10"));
        rule.setReducePrice(new BigDecimal("2"));
        product.setProductFullReductionList(new ArrayList<>(List.of(rule)));
        check(4, "2");
    }
    @Test void normalPriceAlsoKeepsOriginalPricing() {
        cart.setPrice(new BigDecimal("10.00"));
        product.setPromotionType(0);
        CartPromotionItem priced = service.calcCartPromotion(List.of(cart)).get(0);
        assertEquals(cart.getPrice(), priced.getPrice());
        assertEquals(BigDecimal.ZERO, priced.getReduceAmount());
    }

    @Test void currentCatalogMetadataReplacesEmptyOrForgedCartLabelsForEveryPromotionBranch() {
        product.setName("当前可信商品"); product.setProductSn("CURRENT-PRODUCT");
        product.setProductCategoryId(12L); product.setBrandName("当前品牌");
        product.setSubTitle("当前说明"); product.setPic("current-product.jpg");
        var sku = product.getSkuStockList().get(0);
        sku.setSkuCode("CURRENT-SKU"); sku.setSpData("[{\"key\":\"颜色\",\"value\":\"蓝色\"}]");
        sku.setPic("current-sku.jpg");
        for (int type : List.of(0,1,3,4)) {
            product.setPromotionType(type);
            var ladder = new PmsProductLadder(); ladder.setCount(1); ladder.setDiscount(new BigDecimal("0.90"));
            product.setProductLadderList(new ArrayList<>(List.of(ladder)));
            var reduction = new PmsProductFullReduction(); reduction.setFullPrice(BigDecimal.ZERO); reduction.setReducePrice(BigDecimal.ONE);
            product.setProductFullReductionList(new ArrayList<>(List.of(reduction)));
            for (String label : Arrays.asList(null,"伪造商品名称")) {
                cart.setProductName(label); cart.setProductAttr("伪造规格"); cart.setProductSkuCode("FAKE");
                var priced = service.calcCartPromotion(List.of(cart)).get(0);
                assertEquals("当前可信商品", priced.getProductName());
                assertEquals(sku.getSpData(), priced.getProductAttr());
                assertEquals("CURRENT-SKU", priced.getProductSkuCode());
                assertEquals("CURRENT-PRODUCT", priced.getProductSn());
                assertEquals(12L, priced.getProductCategoryId());
                assertEquals("current-sku.jpg", priced.getProductPic());
                assertEquals(label, cart.getProductName(), "Read-only calculation cannot rewrite the stored cart");
                assertEquals("伪造规格", cart.getProductAttr());
            }
        }
    }

    private void reduction(String threshold, String discount) {
        var rule = new PmsProductFullReduction();
        rule.setFullPrice(new BigDecimal(threshold));
        rule.setReducePrice(new BigDecimal(discount));
        product.setPromotionType(4);
        product.setProductFullReductionList(new ArrayList<>(List.of(rule)));
    }

    @Test void subtotalOneCentBelowThresholdGetsNoReduction() {
        product.getSkuStockList().get(0).setPrice(new BigDecimal("99.99"));
        reduction("100.00", "10.00");
        assertEquals(0, BigDecimal.ZERO.compareTo(service.calcCartPromotion(List.of(cart)).get(0).getPromotionTotal()));
    }

    @Test void exactThresholdQualifiesAndHighestFractionalThresholdWins() {
        product.getSkuStockList().get(0).setPrice(new BigDecimal("100.50"));
        reduction("100.10", "1.00");
        var higher = new PmsProductFullReduction();
        higher.setFullPrice(new BigDecimal("100.50")); higher.setReducePrice(new BigDecimal("2.00"));
        product.getProductFullReductionList().add(higher);
        assertEquals(0, new BigDecimal("2.00").compareTo(service.calcCartPromotion(List.of(cart)).get(0).getPromotionTotal()));
    }

    @Test void threeUnitsKeepTheWholeFixedDiscount() {
        product.getSkuStockList().get(0).setPrice(new BigDecimal("1.00"));
        cart.setQuantity(3); reduction("3.00", "1.00");
        var priced = service.calcCartPromotion(List.of(cart)).get(0);
        assertEquals(new BigDecimal("1.00"), priced.getPromotionTotal());
        assertEquals(new BigDecimal("2.00"), priced.getPayableTotal());
    }

    @Test void residualCentUsesStableCartIdentityEvenIfInputOrderChanges() {
        product.getSkuStockList().get(0).setPrice(new BigDecimal("1.00"));
        cart.setId(10L); reduction("0.00", "0.01");
        var second = new OmsCartItem(); second.setId(20L); second.setProductId(1L);
        second.setProductSkuId(2L); second.setQuantity(1);
        var priced = service.calcCartPromotion(List.of(second, cart));
        assertEquals(10L, priced.get(0).getId());
        assertEquals(new BigDecimal("0.01"), priced.get(0).getPromotionTotal());
        assertEquals(new BigDecimal("0.00"), priced.get(1).getPromotionTotal());
        assertEquals(20L, second.getId());
    }
}
