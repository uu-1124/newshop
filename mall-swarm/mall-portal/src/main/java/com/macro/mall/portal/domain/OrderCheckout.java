package com.macro.mall.portal.domain;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.model.*;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.security.MessageDigest;
import java.util.*;

/** The same calculation is used by the original checkout and the agent confirmation. */
public record OrderCheckout(OmsOrder order, List<OmsOrderItem> items,
                            List<CartPromotionItem> cartItems, UmsMemberReceiveAddress address) {
    public static String money(BigDecimal value) {
        return value.setScale(2, RoundingMode.HALF_UP).toPlainString();
    }
    public static Map<String,Object> fields(Object... values) {
        Map<String,Object> out = new TreeMap<>();
        for (int i=0;i<values.length;i+=2) out.put((String)values[i],values[i+1]);
        return out;
    }
    public List<Map<String,Object>> lines() {
        Map<Object,List<OmsOrderItem>> grouped = new LinkedHashMap<>();
        for (OmsOrderItem item : items) {
            Object key = item instanceof CheckoutItem source && source.getCartItemId() != null
                    ? source.getCartItemId() : item;
            grouped.computeIfAbsent(key, ignored -> new ArrayList<>()).add(item);
        }
        return grouped.values().stream().map(group -> {
            OmsOrderItem first = group.get(0);
            int quantity = group.stream().mapToInt(OmsOrderItem::getProductQuantity).reduce(0, Math::addExact);
            BigDecimal payable = group.stream().map(item -> item.getRealAmount()
                    .multiply(BigDecimal.valueOf(item.getProductQuantity()))).reduce(BigDecimal.ZERO, BigDecimal::add);
            return fields("product_id",String.valueOf(first.getProductId()),
                    "sku_id",String.valueOf(first.getProductSkuId()),"name",Objects.toString(first.getProductName(),""),
                    "specification",Objects.toString(first.getProductAttr(),""),"quantity",quantity,
                    "unit_price",money(first.getProductPrice()),"line_payable",money(payable));
        }).toList();
    }
    public Map<String,Object> pricing() {
        return fields("subtotal",money(order.getTotalAmount()),"promotion_discount",money(order.getPromotionAmount()),
            "coupon_discount",money(order.getCouponAmount()),"points_discount",money(order.getIntegrationAmount()),
            "freight",money(order.getFreightAmount()),"payable",money(order.getPayAmount()),"currency","CNY");
    }
    public static String addressLabel(UmsMemberReceiveAddress a) {
        String phone=Objects.toString(a.getPhoneNumber(),"");
        if(phone.length()>7) phone=phone.substring(0,3)+"****"+phone.substring(phone.length()-4);
        return String.join(" ",Objects.toString(a.getName(),""),phone,Objects.toString(a.getProvince(),""),
            Objects.toString(a.getCity(),""),Objects.toString(a.getRegion(),""),Objects.toString(a.getDetailAddress(),""));
    }
    public String fingerprint() {
        // Include the full address and cart identities, even where the public label is masked.
        var snapshot=fields("lines",lines(),"pricing",pricing(),"address",fields(
            "id",address.getId(),"name",address.getName(),"phone",address.getPhoneNumber(),
            "postcode",address.getPostCode(),"province",address.getProvince(),"city",address.getCity(),
            "region",address.getRegion(),"detail",address.getDetailAddress()),
            "cart_ids",cartItems.stream().map(CartPromotionItem::getId).toList(),
            "allocation",items.stream().map(item -> fields("sku",item.getProductSkuId(),
                    "quantity",item.getProductQuantity(),"promotion",money(item.getPromotionAmount()),
                    "coupon",money(item.getCouponAmount()),"points",money(item.getIntegrationAmount()),
                    "paid",money(item.getRealAmount()))).toList());
        try {return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
            .digest(new ObjectMapper().writeValueAsBytes(snapshot)));}
        catch(Exception ex){throw new IllegalStateException("Checkout fingerprint unavailable",ex);}
    }
}
