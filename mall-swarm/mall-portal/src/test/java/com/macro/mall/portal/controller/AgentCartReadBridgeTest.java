package com.macro.mall.portal.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.model.UmsMember;
import com.macro.mall.portal.service.*;
import com.macro.mall.portal.util.StpMemberUtil;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import java.util.List;
import java.util.Map;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class AgentCartReadBridgeTest {
    private final AgentCartService carts=mock(AgentCartService.class);
    private final AgentCartSecurity security=mock(AgentCartSecurity.class);
    private final UmsMemberService members=mock(UmsMemberService.class);
    private final ObjectMapper json=new ObjectMapper();
    private final AgentCartBridgeController controller=new AgentCartBridgeController(mock(AgentOrderService.class),carts,security,members,json,true,true);

    @Test void unsignedReadNeverLoadsMemberOrCart(){
        var request=new MockHttpServletRequest("GET","/agent-bridge/v1/cart");
        assertEquals(403,controller.cart(request,"20",null).getStatusCode().value());
        verifyNoInteractions(carts,members);
    }

    @Test void signedReadUsesCurrentMemberAndRejectsScopeInjection(){
        var request=new MockHttpServletRequest("GET","/agent-bridge/v1/cart");
        request.addParameter("limit","1");request.addParameter("after","30");
        when(security.signed(eq(request),any(byte[].class))).thenReturn(true);
        var member=new UmsMember();member.setId(22L);member.setStatus(1);when(members.getCurrentMember()).thenReturn(member);
        var snapshot=json.valueToTree(Map.of("items",List.of()));when(carts.snapshot(22L,1,"30")).thenReturn(snapshot);
        try(var stp=mockStatic(StpMemberUtil.class)){
            stp.when(StpMemberUtil::isLogin).thenReturn(true);
            var response=controller.cart(request,"1","30");assertEquals(200,response.getStatusCode().value());
            assertEquals(snapshot,response.getBody().get("data"));verify(carts).snapshot(22L,1,"30");
            request.addParameter("member_id","99");
            assertEquals(409,controller.cart(request,"1","30").getStatusCode().value());
            verifyNoMoreInteractions(carts);
        }
    }

    @Test void expiredLoginAndDisabledMemberCannotRead(){
        var request=new MockHttpServletRequest("GET","/agent-bridge/v1/cart");
        when(security.signed(eq(request),any(byte[].class))).thenReturn(true);
        try(var stp=mockStatic(StpMemberUtil.class)){
            stp.when(StpMemberUtil::isLogin).thenReturn(false);
            assertEquals(401,controller.cart(request,"20",null).getStatusCode().value());
            stp.when(StpMemberUtil::isLogin).thenReturn(true);
            var disabled=new UmsMember();disabled.setId(22L);disabled.setStatus(0);when(members.getCurrentMember()).thenReturn(disabled);
            assertEquals(401,controller.cart(request,"20",null).getStatusCode().value());
            verifyNoInteractions(carts);
        }
    }
}
