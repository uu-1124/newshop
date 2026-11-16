package com.macro.mall.portal.service;

import com.macro.mall.common.service.RedisService;
import com.macro.mall.mapper.*;
import com.macro.mall.model.*;
import com.macro.mall.order.OrderResourceService;
import com.macro.mall.portal.component.CancelOrderSender;
import com.macro.mall.portal.dao.PortalOrderItemDao;
import com.macro.mall.portal.domain.CartPromotionItem;
import com.macro.mall.portal.domain.OrderParam;
import com.macro.mall.portal.service.impl.OmsPortalOrderServiceImpl;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import java.math.BigDecimal;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class OrderPointsPersistenceTest {
    @Test void pointsUsedArePersistedBeforeOrderInsertAndConsumedAtomically() {
        var service = new OmsPortalOrderServiceImpl();
        var members = mock(UmsMemberService.class);
        var carts = mock(OmsCartItemService.class);
        var addresses = mock(UmsMemberReceiveAddressService.class);
        var products = mock(PmsProductMapper.class);
        var skus = mock(PmsSkuStockMapper.class);
        var settings = mock(OmsOrderSettingMapper.class);
        var integrationSettings = mock(UmsIntegrationConsumeSettingMapper.class);
        var orders = mock(OmsOrderMapper.class);
        var items = mock(PortalOrderItemDao.class);
        var redis = mock(RedisService.class);
        var resources = mock(OrderResourceService.class);
        ReflectionTestUtils.setField(service,"cartGuard",org.mockito.Mockito.mock(CartGuard.class));
        ReflectionTestUtils.setField(service, "memberService", members);
        ReflectionTestUtils.setField(service, "cartItemService", carts);
        ReflectionTestUtils.setField(service, "memberReceiveAddressService", addresses);
        ReflectionTestUtils.setField(service, "productMapper", products);
        ReflectionTestUtils.setField(service, "skuStockMapper", skus);
        ReflectionTestUtils.setField(service, "orderSettingMapper", settings);
        ReflectionTestUtils.setField(service, "integrationConsumeSettingMapper", integrationSettings);
        ReflectionTestUtils.setField(service, "orderMapper", orders);
        ReflectionTestUtils.setField(service, "orderItemDao", items);
        ReflectionTestUtils.setField(service, "redisService", redis);
        ReflectionTestUtils.setField(service, "orderResources", resources);
        ReflectionTestUtils.setField(service, "cancelOrderSender", mock(CancelOrderSender.class));
        var member = new UmsMember(); member.setId(1L); member.setIntegration(5000);
        when(members.getCurrentMember()).thenReturn(member);
        when(addresses.getItem(1L)).thenReturn(new UmsMemberReceiveAddress());
        var cart = new CartPromotionItem();
        cart.setId(1L); cart.setProductId(10L); cart.setProductSkuId(100L); cart.setQuantity(1);
        cart.setPrice(new BigDecimal("100")); cart.setReduceAmount(BigDecimal.ZERO);
        cart.setRealStock(10); cart.setIntegration(0); cart.setGrowth(0);
        when(carts.list(1L)).thenReturn(List.of(cart));
        when(carts.listPromotion(1L, List.of(1L))).thenReturn(List.of(cart));
        var product = new PmsProduct(); product.setDeleteStatus(0); product.setPublishStatus(1);
        when(products.selectByPrimaryKey(10L)).thenReturn(product);
        var sku = new PmsSkuStock(); sku.setProductId(10L);
        when(skus.selectByPrimaryKey(100L)).thenReturn(sku);
        var rules = new UmsIntegrationConsumeSetting(); rules.setUseUnit(100); rules.setMaxPercentPerOrder(100);
        when(integrationSettings.selectByPrimaryKey(1L)).thenReturn(rules);
        var orderSetting = new OmsOrderSetting(); orderSetting.setNormalOrderOvertime(30);
        when(settings.selectByPrimaryKey(1L)).thenReturn(orderSetting);
        when(redis.incr(anyString(), eq(1L))).thenReturn(1L);
        when(orders.insert(any())).thenAnswer(call -> {
            OmsOrder order = call.getArgument(0);
            assertEquals(100, order.getUseIntegration());
            order.setId(1L);
            return 1;
        });
        var param = new OrderParam(); param.setMemberReceiveAddressId(1L); param.setCartIds(List.of(1L));
        param.setUseIntegration(100); param.setPayType(1);
        service.generateOrder(param);
        verify(resources).consumePoints(1L, 100);
        verify(resources).reserveStock(any());
        verify(members, never()).updateIntegration(any(), any());
    }
}
