package com.macro.mall.portal.service;

import com.macro.mall.model.*;
import com.macro.mall.portal.dao.SmsCouponHistoryDao;
import com.macro.mall.portal.domain.CartPromotionItem;
import com.macro.mall.portal.domain.SmsCouponHistoryDetail;
import com.macro.mall.portal.service.impl.UmsMemberCouponServiceImpl;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.math.BigDecimal;
import java.util.Date;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class CouponThresholdTest {
    @Test void allCouponScopesCompareExactCentsIncludingSubYuanTotals() {
        var service = new UmsMemberCouponServiceImpl();
        var members = mock(UmsMemberService.class);
        var member = new UmsMember(); member.setId(1L);
        when(members.getCurrentMember()).thenReturn(member);
        var dao = mock(SmsCouponHistoryDao.class);
        ReflectionTestUtils.setField(service, "memberService", members);
        ReflectionTestUtils.setField(service, "couponHistoryDao", dao);
        var item = new CartPromotionItem(); item.setProductId(1L); item.setProductCategoryId(2L);
        item.setQuantity(3); item.setPrice(BigDecimal.ONE); item.setReduceAmount(BigDecimal.ZERO);
        item.setPromotionTotal(new BigDecimal("2.01")); // Exact payable is 0.99, including the residual cent.
        for (int scope : List.of(0, 1, 2)) {
            var coupon = new SmsCoupon(); coupon.setId(1L); coupon.setUseType(scope);
            coupon.setMinPoint(BigDecimal.ONE); coupon.setEndTime(new Date(System.currentTimeMillis() + 60000));
            var detail = new SmsCouponHistoryDetail(); detail.setCoupon(coupon);
            var product = new SmsCouponProductRelation(); product.setProductId(1L);
            var category = new SmsCouponProductCategoryRelation(); category.setProductCategoryId(2L);
            detail.setProductRelationList(List.of(product)); detail.setCategoryRelationList(List.of(category));
            when(dao.getDetailList(1L)).thenReturn(List.of(detail));
            assertTrue(service.listCart(List.of(item), 1).isEmpty(), "0.99 must not reach 1.00 for scope " + scope);
            coupon.setMinPoint(new BigDecimal("0.99"));
            assertEquals(1, service.listCart(List.of(item), 1).size(), "exact sub-yuan threshold qualifies");
        }
    }
}
