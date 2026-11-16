package com.macro.mall.order.dao;

import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/** Conditional arithmetic used by both order entry points. */
public interface OrderResourceDao {
    @Update("UPDATE pms_sku_stock SET lock_stock=lock_stock+#{quantity} "
            + "WHERE id=#{skuId} AND lock_stock>=0 AND stock-lock_stock>=#{quantity}")
    int reserveStock(@Param("skuId") Long skuId, @Param("quantity") int quantity);

    @Update("UPDATE pms_sku_stock SET lock_stock=lock_stock-#{quantity} "
            + "WHERE id=#{skuId} AND lock_stock>=#{quantity}")
    int releaseStock(@Param("skuId") Long skuId, @Param("quantity") int quantity);

    @Update("UPDATE pms_sku_stock SET stock=stock-#{quantity}, lock_stock=lock_stock-#{quantity} "
            + "WHERE id=#{skuId} AND stock>=#{quantity} AND lock_stock>=#{quantity}")
    int deductStock(@Param("skuId") Long skuId, @Param("quantity") int quantity);

    @Select("SELECT id FROM sms_coupon_history WHERE coupon_id=#{couponId} AND member_id=#{memberId} "
            + "AND use_status=0 ORDER BY id LIMIT 1 FOR UPDATE")
    Long lockUnusedCoupon(@Param("couponId") Long couponId, @Param("memberId") Long memberId);

    @Update("UPDATE sms_coupon_history SET use_status=1,use_time=NOW(),order_id=#{orderId},order_sn=#{orderSn} "
            + "WHERE id=#{historyId} AND use_status=0")
    int consumeCoupon(@Param("historyId") Long historyId, @Param("orderId") Long orderId,
                      @Param("orderSn") String orderSn);

    @Update("UPDATE sms_coupon_history SET use_status=0,use_time=NULL,order_id=NULL,order_sn=NULL "
            + "WHERE order_id=#{orderId} AND coupon_id=#{couponId} AND member_id=#{memberId} AND use_status=1")
    int releaseCoupon(@Param("orderId") Long orderId, @Param("couponId") Long couponId,
                      @Param("memberId") Long memberId);

    @Update("UPDATE ums_member SET integration=integration-#{quantity} "
            + "WHERE id=#{memberId} AND integration>=#{quantity}")
    int consumePoints(@Param("memberId") Long memberId, @Param("quantity") int quantity);

    @Update("UPDATE ums_member SET integration=COALESCE(integration,0)+#{quantity} WHERE id=#{memberId}")
    int refundPoints(@Param("memberId") Long memberId, @Param("quantity") int quantity);
}
