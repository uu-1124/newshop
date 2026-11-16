package com.macro.mall.portal.controller;

import com.macro.mall.order.AgentAfterSaleStore;
import com.macro.mall.portal.service.*;
import com.macro.mall.portal.util.StpMemberUtil;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.ResponseEntity;
import java.util.*;

@RestController @RequestMapping("/agent-bridge/v1/orders")
public class AgentOrdersBridgeController {
    private final AgentCartSecurity security;private final UmsMemberService members;private final AgentOrderQueries queries;private final AgentAfterSaleStore store;
    public AgentOrdersBridgeController(AgentCartSecurity security,UmsMemberService members,AgentOrderQueries queries,AgentAfterSaleStore store){this.security=security;this.members=members;this.queries=queries;this.store=store;}
    interface Read {Object run(long member);}
    private ResponseEntity<?> read(HttpServletRequest request,Read read){
        int status=200;String code="OK";Object data=null;
        try{if(!security.signed(request,new byte[0]))throw new AgentAfterSaleStore.Rejected("BRIDGE_SIGNATURE_INVALID",403);
            if(!StpMemberUtil.isLogin())throw new AgentAfterSaleStore.Rejected("AUTH_EXPIRED",401);
            var member=members.getCurrentMember();if(member==null || !Integer.valueOf(1).equals(member.getStatus()))throw new AgentAfterSaleStore.Rejected("AUTH_EXPIRED",401);
            if(request.getParameterMap().values().stream().anyMatch(v->v.length!=1))throw new AgentAfterSaleStore.Rejected("VALIDATION_ERROR",422);
            data=read.run(member.getId());
        }catch(AgentAfterSaleStore.Rejected e){code=e.code;status=e.status;}catch(IllegalArgumentException e){code="VALIDATION_ERROR";status=422;}
        return ResponseEntity.status(status).body(AgentAfterSaleStore.map("code",code,"message",code.equals("OK")?"成功":"订单或售后信息不可访问，请刷新核对","data",data,"request_id",UUID.randomUUID().toString()));
    }
    private long id(String s){try{if(!s.matches("[1-9][0-9]{0,18}"))throw new IllegalArgumentException();return Long.parseLong(s);}catch(Exception e){throw new AgentAfterSaleStore.Rejected("VALIDATION_ERROR",422);}}
    private void noQuery(HttpServletRequest r){if(!r.getParameterMap().isEmpty())throw new AgentAfterSaleStore.Rejected("VALIDATION_ERROR",422);}
    @GetMapping public ResponseEntity<?> list(HttpServletRequest r){return read(r,m->{Map<String,String> p=new HashMap<>();r.getParameterMap().forEach((k,v)->p.put(k,v[0]));return queries.list(m,p);});}
    @GetMapping("/{order}") public ResponseEntity<?> detail(@PathVariable String order,HttpServletRequest r){return read(r,m->{noQuery(r);return store.orderSummary(m,id(order));});}
    @GetMapping("/{order}/logistics") public ResponseEntity<?> logistics(@PathVariable String order,HttpServletRequest r){return read(r,m->{noQuery(r);return store.logistics(m,id(order));});}
    @GetMapping("/{order}/returns") public ResponseEntity<?> returns(@PathVariable String order,HttpServletRequest r){return read(r,m->{noQuery(r);return AgentAfterSaleStore.map("items",store.returns(m,id(order)),"next_cursor",null,"has_more",false);});}
    @GetMapping("/{order}/items/{item}/return-eligibility") public ResponseEntity<?> eligibility(@PathVariable String order,@PathVariable String item,HttpServletRequest r){return read(r,m->{noQuery(r);return store.eligibility(m,id(order),id(item));});}
}
