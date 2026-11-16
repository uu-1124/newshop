package com.macro.mall.portal.service;

import com.macro.mall.mapper.*;
import com.macro.mall.model.*;
import com.macro.mall.portal.dao.PortalProductDao;
import com.macro.mall.portal.domain.*;
import com.macro.mall.portal.service.impl.OmsPortalOrderServiceImpl;
import com.macro.mall.portal.service.impl.OmsPromotionServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.math.BigDecimal;
import java.util.*;
import java.util.function.Function;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class CheckoutPricingConservationTest {
    private final OmsPortalOrderServiceImpl service = new OmsPortalOrderServiceImpl();
    private final OmsPromotionServiceImpl promotion = new OmsPromotionServiceImpl();
    private final UmsMemberCouponService coupons = mock(UmsMemberCouponService.class);
    private final List<OmsCartItem> cart = new ArrayList<>();
    private final List<PromotionProduct> products = new ArrayList<>();
    private final OrderParam param = new OrderParam();

    @BeforeEach void setUp() {
        var members = mock(UmsMemberService.class);
        var member = new UmsMember(); member.setId(1L); member.setIntegration(10000);
        when(members.getCurrentMember()).thenReturn(member);
        var carts = mock(OmsCartItemService.class);
        when(carts.list(1L)).thenReturn(cart);
        when(carts.listPromotion(eq(1L), anyList())).thenAnswer(call -> promotion.calcCartPromotion(cart));
        var addresses = mock(UmsMemberReceiveAddressService.class);
        when(addresses.getItem(1L)).thenReturn(new UmsMemberReceiveAddress());
        var productMapper = mock(PmsProductMapper.class);
        var product = new PmsProduct(); product.setPublishStatus(1); product.setDeleteStatus(0);
        when(productMapper.selectByPrimaryKey(anyLong())).thenReturn(product);
        var skus = mock(PmsSkuStockMapper.class);
        when(skus.selectByPrimaryKey(anyLong())).thenAnswer(call -> {
            long id = call.getArgument(0);
            return products.stream().flatMap(p -> p.getSkuStockList().stream())
                    .filter(s -> s.getId() == id).findFirst().orElseThrow();
        });
        var dao = mock(PortalProductDao.class);
        when(dao.getPromotionProductList(anyList())).thenReturn(products);
        ReflectionTestUtils.setField(promotion, "portalProductDao", dao);
        var rules = new UmsIntegrationConsumeSetting(); rules.setUseUnit(100);
        rules.setMaxPercentPerOrder(100); rules.setCouponStatus(1);
        var settings = mock(UmsIntegrationConsumeSettingMapper.class);
        when(settings.selectByPrimaryKey(1L)).thenReturn(rules);
        ReflectionTestUtils.setField(service, "memberService", members);
        ReflectionTestUtils.setField(service, "cartItemService", carts);
        ReflectionTestUtils.setField(service, "memberReceiveAddressService", addresses);
        ReflectionTestUtils.setField(service, "memberCouponService", coupons);
        ReflectionTestUtils.setField(service, "productMapper", productMapper);
        ReflectionTestUtils.setField(service, "skuStockMapper", skus);
        ReflectionTestUtils.setField(service, "integrationConsumeSettingMapper", settings);
        param.setMemberReceiveAddressId(1L); param.setUseIntegration(0);
    }

    private void line(long id, String price, int quantity, String reduction) {
        var item = new OmsCartItem(); item.setId(id); item.setProductId(id); item.setProductSkuId(id);
        item.setQuantity(quantity); item.setProductCategoryId(id); item.setProductName("商品" + id);
        cart.add(item);
        var sku = new PmsSkuStock(); sku.setId(id); sku.setProductId(id);
        sku.setPrice(new BigDecimal(price)); sku.setStock(100); sku.setLockStock(0);
        var product = new PromotionProduct(); product.setId(id); product.setGiftPoint(0); product.setGiftGrowth(0);
        product.setSkuStockList(List.of(sku)); product.setPromotionType(reduction == null ? 0 : 4);
        var rules = new ArrayList<PmsProductFullReduction>();
        if (reduction != null) {
            var rule = new PmsProductFullReduction(); rule.setFullPrice(BigDecimal.ZERO);
            rule.setReducePrice(new BigDecimal(reduction)); rules.add(rule);
        }
        product.setProductFullReductionList(rules); products.add(product);
        param.setCartIds(cart.stream().map(OmsCartItem::getId).toList());
    }

    private void coupon(String amount, Long productId) {
        var coupon = new SmsCoupon(); coupon.setId(1L); coupon.setAmount(new BigDecimal(amount));
        coupon.setUseType(productId == null ? 0 : 2);
        var detail = new SmsCouponHistoryDetail(); detail.setCoupon(coupon);
        if (productId != null) {
            var relation = new SmsCouponProductRelation(); relation.setProductId(productId);
            detail.setProductRelationList(List.of(relation));
        }
        when(coupons.listCart(anyList(), eq(1))).thenReturn(List.of(detail));
        param.setCouponId(1L);
    }

    private BigDecimal total(OrderCheckout checkout, Function<OmsOrderItem, BigDecimal> field) {
        return checkout.items().stream().map(i -> field.apply(i).multiply(BigDecimal.valueOf(i.getProductQuantity())))
                .reduce(BigDecimal.ZERO, BigDecimal::add);
    }

    private OrderCheckout checked(String payable) {
        OrderCheckout checkout = service.previewOrder(param);
        assertEquals(0, new BigDecimal(payable).compareTo(checkout.order().getPayAmount()));
        assertEquals(0, checkout.order().getPayAmount().compareTo(total(checkout, OmsOrderItem::getRealAmount)));
        assertEquals(0, checkout.order().getPromotionAmount().compareTo(total(checkout, OmsOrderItem::getPromotionAmount)));
        assertEquals(0, checkout.order().getCouponAmount().compareTo(total(checkout, OmsOrderItem::getCouponAmount)));
        assertEquals(0, checkout.order().getIntegrationAmount().compareTo(total(checkout, OmsOrderItem::getIntegrationAmount)));
        assertEquals(cart.stream().mapToInt(OmsCartItem::getQuantity).sum(),
                checkout.items().stream().mapToInt(OmsOrderItem::getProductQuantity).sum());
        for (OmsOrderItem item : checkout.items()) {
            for (BigDecimal value : List.of(item.getPromotionAmount(), item.getCouponAmount(),
                    item.getIntegrationAmount(), item.getRealAmount())) {
                assertTrue(value.signum() >= 0);
                assertDoesNotThrow(() -> value.setScale(2, java.math.RoundingMode.UNNECESSARY));
            }
        }
        assertEquals(cart.size(), checkout.lines().size(), "quote retains one line per cart selection");
        assertEquals(checkout.fingerprint(), service.previewOrder(param).fingerprint());
        return checkout;
    }

    @Test void fixedReductionWithThreeUnitsPreservesTheWholeYuan() {
        line(1, "1.00", 3, "1.00");
        var checkout = checked("2.00");
        assertEquals(2, checkout.items().size());
        assertEquals("2.00", checkout.lines().get(0).get("line_payable"));
        assertEquals("1.00", checkout.pricing().get("promotion_discount"));
    }

    @Test void couponWithThreeUnitsPreservesTheWholeYuan() {
        line(1, "1.00", 3, null); coupon("1.00", null); checked("2.00");
    }

    @Test void pointsWithThreeUnitsPreserveTheWholeYuan() {
        line(1, "1.00", 3, null); param.setUseIntegration(100); checked("2.00");
    }

    @Test void stackedDiscountsRemainExactAndNeverMakeAUnitNegative() {
        line(1, "1.00", 3, "1.00"); coupon("1.00", null); param.setUseIntegration(100);
        checked("0.00");
    }

    @Test void restrictedCouponDoesNotDiscountUnrelatedProduct() {
        line(1, "1.00", 3, null); line(2, "2.00", 2, null); coupon("1.00", 1L);
        var checkout = checked("6.00");
        assertTrue(checkout.items().stream().filter(i -> i.getProductId() == 2L)
                .allMatch(i -> i.getCouponAmount().signum() == 0));
    }

    @Test void excessiveStackedDiscountIsRejectedBeforeAnyOrderWrite() {
        line(1, "1.00", 3, "2.50"); coupon("1.00", null);
        assertThrows(RuntimeException.class, () -> service.previewOrder(param));
    }

    @Test void multipleRowsAndResidualCentsKeepTotalsAndStableQuotes() {
        line(2, "0.07", 7, "0.03"); line(1, "0.11", 3, "0.01");
        coupon("0.05", null); checked("0.73");
    }
}
