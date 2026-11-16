package com.macro.mall.portal.service;

import com.macro.mall.mapper.OmsOrderSettingMapper;
import com.macro.mall.model.OmsOrderSetting;
import com.macro.mall.order.OrderResourceService;
import com.macro.mall.portal.component.OrderTimeOutCancelTask;
import com.macro.mall.portal.dao.PortalOrderDao;
import com.macro.mall.portal.domain.OmsOrderDetail;
import com.macro.mall.portal.service.impl.OmsPortalOrderServiceImpl;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.test.util.ReflectionTestUtils;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class OrderTimeoutConsistencyTest {
    @Test void schedulerRemainsOffUnlessExplicitlyEnabled() {
        new ApplicationContextRunner().withUserConfiguration(OrderTimeOutCancelTask.class)
                .run(context -> assertFalse(context.containsBean("orderTimeOutCancelTask")));
    }

    @Test void anomalousOrderDoesNotPreventOtherOrdersFromClosing() {
        var service = new OmsPortalOrderServiceImpl();
        var dao = mock(PortalOrderDao.class);
        var settings = mock(OmsOrderSettingMapper.class);
        var resources = mock(OrderResourceService.class);
        ReflectionTestUtils.setField(service, "portalOrderDao", dao);
        ReflectionTestUtils.setField(service, "orderSettingMapper", settings);
        ReflectionTestUtils.setField(service, "orderResources", resources);
        OmsOrderSetting setting = new OmsOrderSetting(); setting.setNormalOrderOvertime(30);
        when(settings.selectByPrimaryKey(1L)).thenReturn(setting);
        OmsOrderDetail a = new OmsOrderDetail(); a.setId(1L);
        OmsOrderDetail b = new OmsOrderDetail(); b.setId(2L);
        OmsOrderDetail c = new OmsOrderDetail(); c.setId(3L);
        when(dao.getTimeOutOrders(30)).thenReturn(List.of(a, b, c));
        when(resources.closePendingOrder(1L)).thenThrow(new IllegalStateException("missing SKU"));
        when(resources.closePendingOrder(2L)).thenReturn(true);
        assertEquals(1, service.cancelTimeOutOrder());
        verify(resources).closePendingOrder(3L);
    }
}
