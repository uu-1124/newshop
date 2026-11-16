package com.macro.mall.portal.domain;

import com.fasterxml.jackson.annotation.JsonIgnore;
import com.macro.mall.common.exception.Asserts;
import com.macro.mall.model.OmsOrderItem;
import org.springframework.beans.BeanUtils;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.List;
import java.util.function.BiConsumer;
import java.util.function.Predicate;

/** Retain the source cart row while splitting new order rows into cent-exact unit prices. */
public class CheckoutItem extends OmsOrderItem {
    private Long cartItemId;

    @JsonIgnore
    public Long getCartItemId() { return cartItemId; }

    public void setCartItemId(Long cartItemId) { this.cartItemId = cartItemId; }

    public static BigDecimal remaining(OmsOrderItem item) {
        return item.getProductPrice().subtract(item.getPromotionAmount())
                .subtract(item.getCouponAmount()).subtract(item.getIntegrationAmount());
    }

    public static List<OmsOrderItem> discount(List<OmsOrderItem> items, BigDecimal amount,
            Predicate<OmsOrderItem> eligible, BiConsumer<OmsOrderItem, BigDecimal> setter) {
        List<BigDecimal> capacities = items.stream().map(item -> eligible.test(item)
                ? remaining(item).multiply(BigDecimal.valueOf(item.getProductQuantity())) : BigDecimal.ZERO).toList();
        return split(items, MoneyAllocation.allocate(amount, capacities), setter);
    }

    public static List<OmsOrderItem> split(List<OmsOrderItem> items, List<BigDecimal> totals,
            BiConsumer<OmsOrderItem, BigDecimal> setter) {
        List<OmsOrderItem> output = new ArrayList<>();
        for (int index = 0; index < items.size(); index++) {
            OmsOrderItem item = items.get(index);
            BigInteger quantity = BigInteger.valueOf(item.getProductQuantity());
            BigDecimal total = totals.get(index);
            if (total.signum() < 0 || total.compareTo(remaining(item).multiply(new BigDecimal(quantity))) > 0)
                Asserts.fail("商品优惠超过商品金额，请重新选择");
            BigInteger[] share = total.movePointRight(2).toBigIntegerExact().divideAndRemainder(quantity);
            int extra = share[1].intValueExact();
            // Earlier purchased units receive the residual cents. No per-unit expansion is needed.
            if (extra > 0) output.add(copy(item, extra, new BigDecimal(share[0].add(BigInteger.ONE), 2), setter));
            if (extra < item.getProductQuantity())
                output.add(copy(item, item.getProductQuantity() - extra, new BigDecimal(share[0], 2), setter));
        }
        return output;
    }

    private static OmsOrderItem copy(OmsOrderItem source, int quantity, BigDecimal discount,
            BiConsumer<OmsOrderItem, BigDecimal> setter) {
        CheckoutItem target = new CheckoutItem();
        BeanUtils.copyProperties(source, target);
        target.setProductQuantity(quantity);
        setter.accept(target, discount);
        return target;
    }
}
