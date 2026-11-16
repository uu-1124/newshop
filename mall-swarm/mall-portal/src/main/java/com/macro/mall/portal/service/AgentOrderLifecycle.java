package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.model.UmsMember;
import com.macro.mall.order.AgentAfterSaleStore;
import com.macro.mall.order.OrderResourceService;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.math.BigDecimal;
import java.sql.Statement;
import java.time.*;
import java.util.*;
import static com.macro.mall.portal.service.AgentCartService.*;

/** Confirmed cancellation and item-specific demo return, sharing the existing operation ledger. */
@Service
public class AgentOrderLifecycle {
    private final JdbcTemplate db;private final ObjectMapper json;private final AgentCartService operations;private final AgentAfterSaleStore store;private final OrderResourceService resources;private final AgentCartSecurity security;
    public AgentOrderLifecycle(JdbcTemplate db,ObjectMapper json,AgentCartService operations,AgentAfterSaleStore store,OrderResourceService resources,AgentCartSecurity security){this.db=db;this.json=json;this.operations=operations;this.store=store;this.resources=resources;this.security=security;}
    private JsonNode parameters(JsonNode input){
        String type=input.path("action_type").asText();
        if(type.equals("CANCEL_ORDER")){fields(input,"action_type","order_id");id(input.path("order_id"));}
        else if(type.equals("CREATE_RETURN")){
            fields(input,"action_type","order_id","order_item_id","quantity","reason_code","description","attachment_ids");id(input.path("order_id"));id(input.path("order_item_id"));
            if(!input.path("quantity").isIntegralNumber() || input.path("quantity").asInt()<1 || input.path("quantity").asInt()>999 || !input.path("quantity").canConvertToInt())throw new Rejected("VALIDATION_ERROR");
            AgentAfterSaleStore.text(input.path("description").asText(),1000,false);AgentAfterSaleStore.text(input.path("reason_code").asText(),80,true);
            var attachments=input.path("attachment_ids");if(!attachments.isArray() || attachments.size()>5)throw new Rejected("VALIDATION_ERROR");
            Set<UUID> seen=new HashSet<>();for(var a:attachments)if(!a.isTextual() || !seen.add(UUID.fromString(a.asText())))throw new Rejected("VALIDATION_ERROR");
        }else throw new Rejected("FEATURE_NOT_ACTIVE");return input.deepCopy();
    }
    private Map<String,Object> preview(long member,JsonNode p,boolean lock){
        var rule=p.path("action_type").asText().equals("CREATE_RETURN")?store.activeRule(lock):null;
        long oid=id(p.path("order_id"));var o=store.order(member,oid,lock);
        if(p.path("action_type").asText().equals("CANCEL_ORDER")){
            if(!Integer.valueOf(0).equals(o.get("status")))throw new Rejected("ORDER_NOT_CANCELLABLE");
            return map("order_id",Long.toString(oid),"order_sn",o.get("order_sn"),"pay_amount",AgentAfterSaleStore.money(o.get("pay_amount")),"status",o.get("status"));
        }
        var eligibility=store.eligibility(member,oid,id(p.path("order_item_id")),rule);
        if(!Boolean.TRUE.equals(eligibility.get("eligible")) || p.path("quantity").asInt()>(int)eligibility.get("remaining_quantity"))throw new Rejected("RETURN_NOT_ELIGIBLE");
        var reason=db.queryForList("SELECT name FROM oms_order_return_reason WHERE id=? AND status=1",String.class,id(p.path("reason_code")));if(reason.size()!=1)throw new Rejected("RETURN_REASON_INVALID");
        var i=store.first("SELECT * FROM oms_order_item WHERE id=? AND order_id=?",id(p.path("order_item_id")),oid);
        return map("item",i,"amount",((BigDecimal)i.get("real_amount")).multiply(BigDecimal.valueOf(p.path("quantity").asInt())),"reason",reason.get(0),"rule",store.ruleVersion(rule));
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public JsonNode quote(UmsMember member,UUID key,JsonNode input){
        JsonNode p=parameters(input);String hash=operations.hash(member.getId(),p);
        var old=store.first("SELECT * FROM oms_agent_quote WHERE quote_id=?",key.toString());
        if(old!=null){if(!hash.equals(old.get("parameters_hash")) || AgentAfterSaleStore.number(old.get("member_id"))!=member.getId())throw new Rejected("IDEMPOTENCY_CONFLICT");return store.parse(old.get("pricing_snapshot"));}
        var current=preview(member.getId(),p,true);List<Object> lines=new ArrayList<>();BigDecimal total=BigDecimal.ZERO;
        if(p.path("action_type").asText().equals("CREATE_RETURN")){
            @SuppressWarnings("unchecked") var i=(Map<String,Object>)current.get("item");total=(BigDecimal)current.get("amount");
            lines.add(map("product_id",i.get("product_id").toString(),"sku_id",i.get("product_sku_id").toString(),"name",Objects.toString(i.get("product_name"),""),"specification",Objects.toString(i.get("product_attr"),""),"quantity",p.path("quantity").asInt(),"unit_price",AgentAfterSaleStore.money(i.get("real_amount")),"line_payable",AgentAfterSaleStore.money(total)));
        }
        boolean cancelling=p.path("action_type").asText().equals("CANCEL_ORDER");
        if(cancelling)total=new BigDecimal(current.get("pay_amount").toString());
        Instant expiry=Instant.now().plusSeconds(300);String fingerprint=AgentCartSecurity.sha(store.encode(current).getBytes(java.nio.charset.StandardCharsets.UTF_8));
        var quote=map("quote_id",key.toString(),"parameters",p,"parameters_hash",hash,"lines",lines,"pricing",map("subtotal",AgentAfterSaleStore.money(total),"promotion_discount","0.00","coupon_discount","0.00","points_discount","0.00","freight","0.00","payable",AgentAfterSaleStore.money(total),"currency","CNY"),"address_summary",null,"rule_version",cancelling?"CANCEL_PENDING_V2":current.get("rule"),"expires_at",expiry.toString());
        if(cancelling)quote.put("cancellation",map("id",current.get("order_id"),"order_sn",current.get("order_sn"),"pay_amount",current.get("pay_amount"),"status",current.get("status")));
        db.update("INSERT INTO oms_agent_quote(quote_id,member_id,action_type,parameters_hash,parameters_json,pricing_snapshot,total_amount,rule_version,expires_at,checkout_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE quote_id=VALUES(quote_id)",key.toString(),member.getId(),p.path("action_type").asText(),hash,operations.canonical(p),store.encode(quote),total,quote.get("rule_version"),LocalDateTime.ofInstant(expiry,ZoneOffset.UTC),fingerprint);
        var stored=store.first("SELECT * FROM oms_agent_quote WHERE quote_id=? FOR UPDATE",key.toString());if(!hash.equals(stored.get("parameters_hash")) || AgentAfterSaleStore.number(stored.get("member_id"))!=member.getId())throw new Rejected("IDEMPOTENCY_CONFLICT");return store.parse(stored.get("pricing_snapshot"));
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public JsonNode execute(UmsMember member,JsonNode input,JsonNode receipt){
        fields(input,"operation_no","quote_id","parameters_hash","parameters","confirmation_receipt","proofs");JsonNode p=parameters(input.path("parameters"));String op=UUID.fromString(input.path("operation_no").asText()).toString();
        if(!receipt.path("action_type").asText().equals(p.path("action_type").asText()) || !receipt.path("quote_id").asText().equals(input.path("quote_id").asText()) || !operations.hash(member.getId(),p).equals(input.path("parameters_hash").asText()))throw new Rejected("CONFIRMATION_INVALID");
        var record=operations.operation(op,receipt);if(!"IN_TX".equals(record.get("state")))return store.parse(record.get("result_json"));
        var q=store.first("SELECT * FROM oms_agent_quote WHERE quote_id=?",input.path("quote_id").asText());
        if(q==null || AgentAfterSaleStore.number(q.get("member_id"))!=member.getId() || !input.path("parameters_hash").asText().equals(q.get("parameters_hash")))return finish(op,"QUOTE_INVALID",null,null);
        if(p.path("action_type").asText().equals("CANCEL_ORDER") && !"CANCEL_PENDING_V2".equals(store.parse(q.get("pricing_snapshot")).path("rule_version").asText()))return finish(op,"QUOTE_CHANGED",null,null);
        if(!Instant.parse(store.parse(q.get("pricing_snapshot")).path("expires_at").asText()).isAfter(Instant.now()))return finish(op,"QUOTE_EXPIRED",null,null);
        Map<String,Object> current;
        try{current=preview(member.getId(),p,true);String fingerprint=AgentCartSecurity.sha(store.encode(current).getBytes(java.nio.charset.StandardCharsets.UTF_8));if(!fingerprint.equals(q.get("checkout_fingerprint")))return finish(op,"QUOTE_CHANGED",null,null);}
        catch(Rejected e){return finish(op,e.code,null,null);}catch(AgentAfterSaleStore.Rejected e){return finish(op,e.code,null,null);}
        JsonNode proofs=input.path("proofs");if(!proofs.isArray())throw new Rejected("VALIDATION_ERROR");
        if(p.path("action_type").asText().equals("CANCEL_ORDER")){
            if(!proofs.isEmpty())throw new Rejected("VALIDATION_ERROR");
            if(!resources.closePendingOrder(id(p.path("order_id"))))return finish(op,"ORDER_NOT_CANCELLABLE",null,null);
            store.audit("MEMBER",member.getId(),"CANCEL_ORDER",id(p.path("order_id")),receipt.path("confirmation_audit_id").asText(),current,Map.of("status",4),"会员确认取消未付款订单");
            return finish(op,null,store.orderSummary(member.getId(),id(p.path("order_id"))),null);
        }
        if(proofs.size()!=p.path("attachment_ids").size())throw new Rejected("PROOF_INVALID");
        Set<String> supplied=new HashSet<>();for(JsonNode proof:proofs){security.proof(proof,op,member.getId());if(!supplied.add(proof.path("attachment_id").asText()))throw new Rejected("PROOF_INVALID");}
        for(JsonNode aid:p.path("attachment_ids"))if(!supplied.contains(aid.asText()))throw new Rejected("PROOF_INVALID");
        @SuppressWarnings("unchecked") var i=(Map<String,Object>)current.get("item");var o=store.order(member.getId(),id(p.path("order_id")),false);
        var key=new GeneratedKeyHolder();db.update(c->{var s=c.prepareStatement("INSERT INTO oms_order_return_apply(order_id,product_id,order_sn,create_time,member_username,return_name,return_phone,status,product_pic,product_name,product_brand,product_attr,product_count,product_price,product_real_price,reason,description) VALUES(?,?,?,NOW(),?,?,?,0,?,?,?,?,?,?,?,?,?)",Statement.RETURN_GENERATED_KEYS);
            Object[] v={o.get("id"),i.get("product_id"),o.get("order_sn"),o.get("member_username"),o.get("receiver_name"),o.get("receiver_phone"),i.get("product_pic"),i.get("product_name"),i.get("product_brand"),i.get("product_attr"),p.path("quantity").asInt(),i.get("product_price"),i.get("real_amount"),current.get("reason"),p.path("description").asText()};for(int n=0;n<v.length;n++)s.setObject(n+1,v[n]);return s;},key);
        long rid=Objects.requireNonNull(key.getKey()).longValue();
        // Store proof identity and hash only. Short-lived attestations never become durable metadata.
        List<Object> metadata=new ArrayList<>();for(JsonNode proof:proofs)metadata.add(map("attachment_id",proof.path("attachment_id").asText(),"sha256",proof.path("sha256").asText(),"mime_type",proof.path("mime_type").asText(),"byte_size",proof.path("byte_size").asInt()));
        if(!Objects.equals(current.get("rule"),store.ruleVersion(store.activeRule(false))))throw new Rejected("QUOTE_CHANGED");
        db.update("INSERT INTO oms_agent_return(return_id,order_id,order_item_id,quantity,requested_amount,stage,rule_version,proof_metadata,created_at,updated_at) VALUES(?,?,?,?,?,'REQUESTED',?,?,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",rid,o.get("id"),i.get("id"),p.path("quantity").asInt(),current.get("amount"),current.get("rule"),store.encode(metadata));
        var summary=store.returnSummary(store.first("SELECT a.*,x.* FROM oms_order_return_apply a JOIN oms_agent_return x ON x.return_id=a.id WHERE a.id=?",rid));
        store.audit("MEMBER",member.getId(),"CREATE_RETURN",rid,receipt.path("confirmation_audit_id").asText(),null,summary,"会员确认提交演示退货申请");return finish(op,null,null,summary);
    }
    private JsonNode finish(String op,String error,Object order,Object returned){
        boolean success=error==null;Object business=order!=null?((Map<?,?>)order).get("id"):returned!=null?((Map<?,?>)returned).get("id"):null;
        String type=order!=null?"ORDER":returned!=null?"RETURN":null;
        var out=map("operation_no",op,"state",success?"SUCCEEDED":"REJECTED","business_type",type,"business_id",business,"error_code",error,"message",success?(returned!=null?"退货申请已提交，等待员工审核；退款为演示流程":"未付款订单已取消"):"订单或售后条件已变化，请重新读取后准备","order",order,"return_apply",returned,"cart_quantity",null,"completed_at",Instant.now().toString());
        db.update("UPDATE oms_agent_operation SET state=?,result_json=?,error_code=?,business_type=?,business_id=?,completed_at=UTC_TIMESTAMP(6) WHERE operation_no=?",success?"SUCCEEDED":"REJECTED",store.encode(out),error,type,business,op);return store.tree(out);
    }
}
