package com.macro.mall.order;

import com.macro.mall.common.exception.Asserts;
import com.macro.mall.common.service.RedisService;
import com.macro.mall.mapper.OmsOrderItemMapper;
import com.macro.mall.mapper.OmsOrderMapper;
import com.macro.mall.model.*;
import com.macro.mall.order.dao.OrderResourceDao;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/** Shared transactional resource handling for portal cancellation and admin closure. */
public class OrderResourceService {
    private static final Logger LOGGER = LoggerFactory.getLogger(OrderResourceService.class);
    private final OmsOrderMapper orders;
    private final OmsOrderItemMapper items;
    private final OrderResourceDao resources;
    private final RedisService redis;
    private final String memberCachePrefix;

    public OrderResourceService(OmsOrderMapper orders, OmsOrderItemMapper items, OrderResourceDao resources,
                                RedisService redis, @Value("${redis.database}") String database,
                                @Value("${redis.key.member:ums:member}") String memberKey) {
        this.orders = orders;
        this.items = items;
        this.resources = resources;
        this.redis = redis;
        this.memberCachePrefix = database + ":" + memberKey + ":";
    }

    @Transactional
    public boolean closePendingOrder(Long orderId) {
        OmsOrderExample pending = new OmsOrderExample();
        pending.createCriteria().andIdEqualTo(orderId).andStatusEqualTo(0).andDeleteStatusEqualTo(0);
        OmsOrder closed = new OmsOrder();
        closed.setStatus(4);
        // This update serializes payment and every cancellation entry point.
        if (orders.updateByExampleSelective(closed, pending) == 0) {
            return false;
        }
        OmsOrder order = orders.selectByPrimaryKey(orderId);
        OmsOrderItemExample example = new OmsOrderItemExample();
        example.createCriteria().andOrderIdEqualTo(orderId);
        for (Map.Entry<Long, Integer> stock : quantities(items.selectByExample(example)).entrySet()) {
            if (resources.releaseStock(stock.getKey(), stock.getValue()) != 1) {
                Asserts.fail("订单锁定库存异常，已回滚关闭操作，请核查订单：" + orderId);
            }
        }
        if (order.getCouponId() != null
                && resources.releaseCoupon(orderId, order.getCouponId(), order.getMemberId()) != 1) {
            // Never guess which historical coupon belongs to an unbound order.
            Asserts.fail("订单优惠券关联异常，已回滚关闭操作，请核查订单：" + orderId);
        }
        Integer used = order.getUseIntegration();
        if (used != null && used < 0) {
            Asserts.fail("订单使用积分异常，已回滚关闭操作");
        }
        if (used != null && used > 0) {
            if (resources.refundPoints(order.getMemberId(), used) != 1) {
                Asserts.fail("订单积分返还失败，已回滚关闭操作");
            }
            invalidateMember(order.getMemberId());
        }
        return true;
    }

    @Transactional
    public void reserveStock(List<OmsOrderItem> orderItems) {
        for (Map.Entry<Long, Integer> stock : quantities(orderItems).entrySet()) {
            if (resources.reserveStock(stock.getKey(), stock.getValue()) != 1) {
                Asserts.fail("库存不足或锁定库存异常，无法下单");
            }
        }
    }

    @Transactional
    public int deductStock(List<OmsOrderItem> orderItems) {
        Map<Long, Integer> stockItems = quantities(orderItems);
        for (Map.Entry<Long, Integer> stock : stockItems.entrySet()) {
            if (resources.deductStock(stock.getKey(), stock.getValue()) != 1) {
                Asserts.fail("订单库存异常，已回滚支付操作");
            }
        }
        return stockItems.size();
    }

    @Transactional
    public void consumeCoupon(OmsOrder order) {
        Long historyId = resources.lockUnusedCoupon(order.getCouponId(), order.getMemberId());
        if (historyId == null || resources.consumeCoupon(historyId, order.getId(), order.getOrderSn()) != 1) {
            Asserts.fail("优惠券已使用，请刷新后重试");
        }
    }

    @Transactional
    public void consumePoints(Long memberId, int quantity) {
        if (quantity <= 0 || resources.consumePoints(memberId, quantity) != 1) {
            Asserts.fail("可用积分不足，请刷新后重试");
        }
        invalidateMember(memberId);
    }

    private Map<Long, Integer> quantities(List<OmsOrderItem> orderItems) {
        if (orderItems == null || orderItems.isEmpty()) {
            Asserts.fail("订单商品为空，已停止库存操作");
        }
        // Aggregate duplicate SKU lines and acquire stock locks in a stable order.
        Map<Long, Integer> result = new TreeMap<>();
        for (OmsOrderItem item : orderItems) {
            if (item.getProductSkuId() == null || item.getProductQuantity() == null || item.getProductQuantity() <= 0) {
                Asserts.fail("订单商品规格或数量异常，已停止库存操作");
            }
            result.merge(item.getProductSkuId(), item.getProductQuantity(), Math::addExact);
        }
        return result;
    }

    private void invalidateMember(Long memberId) {
        String key = memberCachePrefix + memberId;
        redis.del(key);
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    try {
                        redis.del(key);
                    } catch (RuntimeException ex) {
                        LOGGER.error("订单提交后清除会员缓存失败，memberId={}", memberId, ex);
                    }
                }
            });
        }
    }
}
