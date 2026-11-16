package com.macro.mall.controller;

import cn.dev33.satoken.stp.StpUtil;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.model.UmsAdmin;
import com.macro.mall.order.AgentAfterSaleStore;
import com.macro.mall.service.UmsAdminService;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockHttpServletRequest;
import java.nio.charset.StandardCharsets;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class AgentAfterSaleBridgeTest {
    @Test void rulePublicationRequiresCurrentReviewPermissionOnEveryReplay()throws Exception{
        var auth=mock(AgentStaffBridgeController.class);var admins=mock(UmsAdminService.class);var store=mock(AgentAfterSaleStore.class);var json=new ObjectMapper();
        var controller=new AgentAfterSaleBridgeController(auth,admins,store,mock(JdbcTemplate.class),json);
        var request=new MockHttpServletRequest("POST","/agent-bridge/v1/return-rules");request.addHeader("Idempotency-Key",UUID.randomUUID().toString());
        byte[] raw="{\"expected_latest_version\":1,\"return_window_hours\":168,\"allow_shipped\":true,\"effective_at\":\"2026-11-14T10:00:00Z\",\"note\":\"demo\"}".getBytes(StandardCharsets.UTF_8);
        when(auth.signed(request,raw)).thenReturn(true);var admin=new UmsAdmin();admin.setId(3L);admin.setStatus(1);when(admins.getItem(3L)).thenReturn(admin);
        try(var stp=mockStatic(StpUtil.class)){
            stp.when(StpUtil::isLogin).thenReturn(true);stp.when(StpUtil::getLoginIdAsLong).thenReturn(3L);
            when(auth.agentPermissions(3L)).thenReturn(Set.of("agent:return:read"));assertEquals(403,controller.publishRule(raw,request).getStatusCode().value());verifyNoInteractions(store);
            when(auth.agentPermissions(3L)).thenReturn(Set.of("agent:return:review"));when(store.publishRule(eq(3L),anyString(),any())).thenReturn(Map.of("version_no",2));assertEquals(200,controller.publishRule(raw,request).getStatusCode().value());
            when(auth.agentPermissions(3L)).thenReturn(Set.of());assertEquals(403,controller.publishRule(raw,request).getStatusCode().value());verify(store,times(1)).publishRule(anyLong(),anyString(),any());
        }
    }
    @Test void currentStaffPermissionIsRequiredForEveryRefundReplay()throws Exception{
        var auth=mock(AgentStaffBridgeController.class);var admins=mock(UmsAdminService.class);var store=mock(AgentAfterSaleStore.class);var json=new ObjectMapper();
        var controller=new AgentAfterSaleBridgeController(auth,admins,store,mock(JdbcTemplate.class),json);
        var request=new MockHttpServletRequest("POST","/agent-bridge/v1/returns/9/simulate-refund");request.addHeader("Idempotency-Key",UUID.randomUUID().toString());
        byte[] raw="{\"expected_version\":3,\"note\":\"demo refund\"}".getBytes(StandardCharsets.UTF_8);
        when(auth.signed(request,raw)).thenReturn(true);var admin=new UmsAdmin();admin.setId(3L);admin.setStatus(1);when(admins.getItem(3L)).thenReturn(admin);
        try(var stp=mockStatic(StpUtil.class)){
            stp.when(StpUtil::isLogin).thenReturn(true);stp.when(StpUtil::getLoginIdAsLong).thenReturn(3L);
            when(auth.agentPermissions(3L)).thenReturn(Set.of("agent:return:read"));
            assertEquals(403,controller.refund("9",raw,request).getStatusCode().value());verifyNoInteractions(store);
            when(auth.agentPermissions(3L)).thenReturn(Set.of("agent:return:refund"));
            when(store.staffCommand(eq(3L),anyString(),eq("REFUND"),eq(9L),any())).thenReturn(json.readTree("{\"stage\":\"SIMULATED_REFUNDED\"}"));
            assertEquals(200,controller.refund("9",raw,request).getStatusCode().value());
            when(auth.agentPermissions(3L)).thenReturn(Set.of());
            assertEquals(403,controller.refund("9",raw,request).getStatusCode().value());verify(store,times(1)).staffCommand(anyLong(),anyString(),anyString(),anyLong(),any());
            admin.setStatus(0);assertEquals(401,controller.refund("9",raw,request).getStatusCode().value());
        }
    }
    @Test void unsignedCommandsNeverReachAuthenticationOrStore(){
        var auth=mock(AgentStaffBridgeController.class);var admins=mock(UmsAdminService.class);var store=mock(AgentAfterSaleStore.class);var controller=new AgentAfterSaleBridgeController(auth,admins,store,mock(JdbcTemplate.class),new ObjectMapper());
        var request=new MockHttpServletRequest("POST","/agent-bridge/v1/returns/9/receive");
        assertEquals(403,controller.receive("9",new byte[0],request).getStatusCode().value());verifyNoInteractions(admins,store);
    }
}
