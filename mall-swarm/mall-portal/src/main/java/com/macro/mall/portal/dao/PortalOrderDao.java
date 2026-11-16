package com.macro.mall.portal.dao;

import com.macro.mall.model.OmsOrder;
import com.macro.mall.portal.domain.OmsOrderDetail;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 前台订单自定义Dao
 * Created by macro on 2018/9/4.
 */
public interface PortalOrderDao {
    /** Serializes return requests for the same order within a transaction. */
    OmsOrder lockOrderForReturn(@Param("orderId") Long orderId);

    /**
     * 获取订单及下单商品详情
     */
    OmsOrderDetail getDetail(@Param("orderId") Long orderId);

    /**
     * 获取超时订单
     * @param minute 超时时间（分）
     */
    List<OmsOrderDetail> getTimeOutOrders(@Param("minute") Integer minute);


}
