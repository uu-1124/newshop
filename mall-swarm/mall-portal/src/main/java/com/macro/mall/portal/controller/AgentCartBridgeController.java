package com.macro.mall.portal.controller;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.common.exception.BridgeNonceUnavailableException;
import com.macro.mall.model.UmsMember;
import com.macro.mall.portal.service.*;
import com.macro.mall.portal.util.StpMemberUtil;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import java.util.*;

@RestController
@RequestMapping("/agent-bridge/v1")
public class AgentCartBridgeController {
    @org.springframework.beans.factory.annotation.Autowired
    private AgentOrderLifecycle lifecycle;
    private final AgentCartService carts;
    private final com.macro.mall.portal.service.AgentOrderService orders;private final AgentCartSecurity security;private final UmsMemberService members;private final ObjectMapper json;private final boolean enabled;private final boolean ordersEnabled;
    public AgentCartBridgeController(com.macro.mall.portal.service.AgentOrderService orders,AgentCartService carts,AgentCartSecurity security,UmsMemberService members,ObjectMapper json,@Value("${MALL_AGENT_ACTIONS_ENABLED:false}") boolean enabled,@Value("${MALL_AGENT_ORDERS_ENABLED:false}") boolean ordersEnabled){this.ordersEnabled=ordersEnabled;this.orders=orders;this.carts=carts;this.security=security;this.members=members;this.json=json;this.enabled=enabled;}
    interface Command{Object run(JsonNode body);}
    private ResponseEntity<Map<String,Object>> command(HttpServletRequest request,byte[] raw,Command command){
        int status=200;String code="OK";Object data=null;
        try{
            if(!enabled)throw new AgentCartService.Rejected("FEATURE_NOT_ACTIVE");
            if(!security.signed(request,raw)){status=403;throw new AgentCartService.Rejected("BRIDGE_SIGNATURE_INVALID");}
            JsonNode body=raw.length==0?json.createObjectNode():json.reader().with(JsonParser.Feature.STRICT_DUPLICATE_DETECTION).readTree(raw);
            if(!ordersEnabled && (request.getRequestURI().endsWith("/checkout-options") || "CREATE_ORDER".equals(body.path("action_type").asText()) || "CREATE_ORDER".equals(body.path("parameters").path("action_type").asText())))throw new AgentCartService.Rejected("FEATURE_NOT_ACTIVE");
            data=command.run(body);
        }catch(com.macro.mall.order.AgentAfterSaleStore.Rejected ex){code=ex.code;status=ex.status;}
        catch(AgentCartService.Rejected ex){code=ex.code;if(status==200)status=code.equals("AUTH_EXPIRED")?401:code.equals("CONFIRMATION_INVALID")?403:409;}
        catch(IllegalArgumentException|java.io.IOException ex){code="VALIDATION_ERROR";status=422;}
        catch(BridgeNonceUnavailableException ex){throw ex;}
        catch(Exception ex){code="JAVA_UNAVAILABLE";status=503;}
        Map<String,Object> out=new LinkedHashMap<>();out.put("code",code);out.put("message",code.equals("OK")?"成功":"商城操作暂不可用，请刷新核对");out.put("data",data);String rid;try{rid=UUID.fromString(request.getHeader("X-Request-ID")).toString();}catch(Exception ex){rid=UUID.randomUUID().toString();}out.put("request_id",rid);return ResponseEntity.status(status).body(out);
    }
    private UmsMember member(){
        if(!StpMemberUtil.isLogin())throw new AgentCartService.Rejected("AUTH_EXPIRED");
        UmsMember member=members.getCurrentMember();if(member==null || !Integer.valueOf(1).equals(member.getStatus()))throw new AgentCartService.Rejected("AUTH_EXPIRED");return member;
    }
    @PostMapping("/checkout-options")
    public ResponseEntity<Map<String,Object>> options(HttpServletRequest request,@RequestBody byte[] raw){return command(request,raw,body->{member();return orders.options(body);});}
    @GetMapping("/cart")
    public ResponseEntity<Map<String,Object>> cart(HttpServletRequest request,@RequestParam(defaultValue="20") String limit,@RequestParam(required=false) String after){
        return command(request,new byte[0],body->{
            var current=member();
            if(!Set.of("limit","after").containsAll(request.getParameterMap().keySet())
                || request.getParameterMap().values().stream().anyMatch(values->values.length!=1)
                || !limit.matches("[1-9][0-9]?"))throw new AgentCartService.Rejected("VALIDATION_ERROR");
            return carts.snapshot(current.getId(),Integer.parseInt(limit),after);
        });
    }
    @PostMapping("/quotes")
    public ResponseEntity<Map<String,Object>> quote(HttpServletRequest request,@RequestBody byte[] raw){
        return command(request,raw,body->{var m=member();var key=UUID.fromString(request.getHeader("Idempotency-Key"));return switch(body.path("action_type").asText()){case "CREATE_ORDER"->orders.quote(m,key,body);case "CANCEL_ORDER","CREATE_RETURN"->lifecycle.quote(m,key,body);default->carts.quote(m.getId(),key,body);};});
    }
    @PostMapping("/operations")
    public ResponseEntity<Map<String,Object>> execute(HttpServletRequest request,@RequestBody byte[] raw){
        return command(request,raw,body->{UmsMember member=member();String op=body.path("operation_no").asText();if(!op.equals(request.getHeader("Idempotency-Key")))throw new AgentCartService.Rejected("IDEMPOTENCY_CONFLICT");JsonNode receipt=security.receipt(body.path("confirmation_receipt").asText(),"execute_confirmed",op,body.path("parameters_hash").asText(),member.getId());return switch(body.path("parameters").path("action_type").asText()){case "CREATE_ORDER"->orders.execute(member,body,receipt);case "CANCEL_ORDER","CREATE_RETURN"->lifecycle.execute(member,body,receipt);default->carts.execute(member,body,receipt);};});
    }
    @GetMapping("/operations/{operationId}")
    public ResponseEntity<Map<String,Object>> get(HttpServletRequest request,@PathVariable UUID operationId){return command(request,new byte[0],body->carts.get(member().getId(),operationId.toString()));}
    @PostMapping("/operations/{operationId}/reconcile")
    public ResponseEntity<Map<String,Object>> reconcile(HttpServletRequest request,@PathVariable UUID operationId,@RequestBody byte[] raw){
        return command(request,raw,body->{if(body.size()!=2 || !body.has("parameters_hash") || !body.has("mode") || !Set.of("QUERY_ONLY","FINALIZE_IF_ABSENT").contains(body.path("mode").asText()))throw new AgentCartService.Rejected("VALIDATION_ERROR");JsonNode receipt=security.receipt(request.getHeader("X-Operation-Receipt"),"reconcile_only",operationId.toString(),body.path("parameters_hash").asText(),null);return carts.reconcile(operationId.toString(),body.path("parameters_hash").asText(),receipt,"FINALIZE_IF_ABSENT".equals(body.path("mode").asText()));});
    }
}
