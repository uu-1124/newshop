package com.macro.mall.controller;

import cn.dev33.satoken.stp.StpUtil;
import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.order.AgentAfterSaleStore;
import com.macro.mall.service.UmsAdminService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import java.util.*;

/** Separate, current resource permissions; demonstration refund has no external payment adapter. */
@RestController @RequestMapping("/agent-bridge/v1")
public class AgentAfterSaleBridgeController {
    private final AgentStaffBridgeController auth;private final UmsAdminService admins;private final AgentAfterSaleStore store;private final JdbcTemplate db;private final ObjectMapper json;
    public AgentAfterSaleBridgeController(AgentStaffBridgeController auth,UmsAdminService admins,AgentAfterSaleStore store,JdbcTemplate db,ObjectMapper json){this.auth=auth;this.admins=admins;this.store=store;this.db=db;this.json=json;}
    interface Command {Object run(long staff,JsonNode body);}
    private ResponseEntity<?> run(HttpServletRequest r,byte[] raw,String permission,Command command){
        int status=200;String code="OK";Object data=null;
        try{
            if(raw.length>16000 || !auth.signed(r,raw))throw new AgentAfterSaleStore.Rejected("BRIDGE_SIGNATURE_INVALID",403);
            if(!StpUtil.isLogin())throw new AgentAfterSaleStore.Rejected("AUTH_EXPIRED",401);
            long id=StpUtil.getLoginIdAsLong();var a=admins.getItem(id);if(a==null || !Integer.valueOf(1).equals(a.getStatus()))throw new AgentAfterSaleStore.Rejected("AUTH_EXPIRED",401);
            if(!auth.agentPermissions(id).contains(permission))throw new AgentAfterSaleStore.Rejected("FORBIDDEN",403);
            data=command.run(id,raw.length==0?json.createObjectNode():json.reader().with(JsonParser.Feature.STRICT_DUPLICATE_DETECTION).readTree(raw));
        }catch(AgentAfterSaleStore.Rejected e){status=e.status;code=e.code;}catch(IllegalArgumentException|java.io.IOException e){status=422;code="VALIDATION_ERROR";}
        return ResponseEntity.status(status).body(AgentAfterSaleStore.map("code",code,"message",code.equals("OK")?"成功":code.equals("LOGISTICS_TIME_INVALID")?"物流时间须在订单或发货时间之后、当前时间之前，并保持节点先后顺序":"订单、售后状态或权限已变化，请刷新核对","data",data,"request_id",UUID.randomUUID().toString()));
    }
    private long id(String s){return AgentAfterSaleStore.decimalId(json.valueToTree(s));}
    private void fields(JsonNode b,String... names){Set<String> actual=new HashSet<>();b.fieldNames().forEachRemaining(actual::add);if(!b.isObject() || !actual.equals(Set.of(names)))throw new AgentAfterSaleStore.Rejected("VALIDATION_ERROR",422);}
    @GetMapping("/return-rules") public ResponseEntity<?> rules(HttpServletRequest r){return run(r,new byte[0],"agent:return:read",(staff,b)->AgentAfterSaleStore.map("items",store.ruleVersions(),"active_rule_version",store.ruleVersion(store.activeRule(false))));}
    @PostMapping("/return-rules") public ResponseEntity<?> publishRule(@RequestBody byte[] raw,HttpServletRequest r){return run(r,raw,"agent:return:review",(staff,b)->store.publishRule(staff,r.getHeader("Idempotency-Key"),b));}
    @GetMapping("/returns") public ResponseEntity<?> list(HttpServletRequest r,@RequestParam(required=false) String stage,@RequestParam(defaultValue="0") long before,@RequestParam(defaultValue="20") int limit){return run(r,new byte[0],"agent:return:read",(staff,b)->{
        if(limit<1 || limit>100 || before<0 || (stage!=null && !Set.of("REQUESTED","APPROVED","GOODS_RECEIVED","SIMULATED_REFUNDED","REJECTED","LEGACY").contains(stage)))throw new AgentAfterSaleStore.Rejected("VALIDATION_ERROR",422);
        var ids=db.queryForList("SELECT a.id FROM oms_order_return_apply a LEFT JOIN oms_agent_return x ON x.return_id=a.id WHERE a.id<? AND (? IS NULL OR COALESCE(x.stage,'LEGACY')=?) ORDER BY a.id DESC LIMIT ?",Long.class,before==0?Long.MAX_VALUE:before,stage,stage,limit+1);
        boolean more=ids.size()>limit;var page=ids.subList(0,Math.min(ids.size(),limit));return AgentAfterSaleStore.map("items",page.stream().map(store::staffReturn).toList(),"has_more",more,"next_before",more?page.get(page.size()-1).toString():null);
    });}
    @GetMapping("/return-addresses") public ResponseEntity<?> addresses(HttpServletRequest r){return run(r,new byte[0],"agent:return:review",(staff,b)->Map.of("items",db.queryForList("SELECT id,address_name,name,phone,province,city,region,detail_address FROM oms_company_address ORDER BY send_status DESC,id LIMIT 100").stream().map(a->AgentAfterSaleStore.map("id",a.get("id").toString(),"label",String.join(" ",Objects.toString(a.get("address_name"),""),Objects.toString(a.get("name"),""),Objects.toString(a.get("phone"),""),Objects.toString(a.get("province"),""),Objects.toString(a.get("city"),""),Objects.toString(a.get("region"),""),Objects.toString(a.get("detail_address"),"")))).toList()));}
    @GetMapping("/returns/{rid}") public ResponseEntity<?> detail(@PathVariable String rid,HttpServletRequest r){return run(r,new byte[0],"agent:return:read",(staff,b)->store.staffReturn(id(rid)));}
    @PostMapping("/returns/{rid}/review") public ResponseEntity<?> review(@PathVariable String rid,@RequestBody byte[] raw,HttpServletRequest r){return run(r,raw,"agent:return:review",(staff,b)->{
        if(b.path("decision").asText().equals("APPROVE"))fields(b,"expected_version","decision","approved_amount","company_address_id","note");else fields(b,"expected_version","decision","note");
        return store.staffCommand(staff,r.getHeader("Idempotency-Key"),"REVIEW",id(rid),b);
    });}
    @PostMapping("/returns/{rid}/receive") public ResponseEntity<?> receive(@PathVariable String rid,@RequestBody byte[] raw,HttpServletRequest r){return run(r,raw,"agent:return:receive",(staff,b)->{fields(b,"expected_version","note");return store.staffCommand(staff,r.getHeader("Idempotency-Key"),"RECEIVE",id(rid),b);});}
    @PostMapping("/returns/{rid}/simulate-refund") public ResponseEntity<?> refund(@PathVariable String rid,@RequestBody byte[] raw,HttpServletRequest r){return run(r,raw,"agent:return:refund",(staff,b)->{fields(b,"expected_version","note");return store.staffCommand(staff,r.getHeader("Idempotency-Key"),"REFUND",id(rid),b);});}
    @GetMapping("/orders/{oid}/logistics") public ResponseEntity<?> logistics(@PathVariable String oid,HttpServletRequest r){return run(r,new byte[0],"agent:logistics:read",(staff,b)->store.logistics(store.memberForOrder(id(oid)),id(oid)));}
    @PutMapping("/orders/{oid}/logistics") public ResponseEntity<?> shipment(@PathVariable String oid,@RequestBody byte[] raw,HttpServletRequest r){return run(r,raw,"agent:logistics:write",(staff,b)->{fields(b,"expected_version","carrier","tracking_no");return store.staffCommand(staff,r.getHeader("Idempotency-Key"),"SHIPMENT",id(oid),b);});}
    @PostMapping("/shipments/{sid}/events") public ResponseEntity<?> node(@PathVariable String sid,@RequestBody byte[] raw,HttpServletRequest r){return run(r,raw,"agent:logistics:write",(staff,b)->{fields(b,"expected_shipment_version","occurred_at","location","description");return store.staffCommand(staff,r.getHeader("Idempotency-Key"),"NODE",id(sid),b);});}
    @PatchMapping("/shipments/{sid}/events/{eid}") public ResponseEntity<?> edit(@PathVariable String sid,@PathVariable String eid,@RequestBody byte[] raw,HttpServletRequest r){return run(r,raw,"agent:logistics:write",(staff,b)->{
        fields(b,"expected_version","occurred_at","location","description","reason");if(db.queryForObject("SELECT COUNT(*) FROM oms_agent_shipment_event WHERE id=? AND shipment_id=?",Long.class,id(eid),id(sid))!=1)throw new AgentAfterSaleStore.Rejected("NOT_FOUND",404);
        return store.staffCommand(staff,r.getHeader("Idempotency-Key"),"EDIT_NODE",id(eid),b);
    });}
}
