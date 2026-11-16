package com.macro.mall.order;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.math.BigDecimal;
import java.sql.*;
import java.time.*;
import java.util.*;

/** Shared projections and demo after-sale state. No payment/refund provider is used here. */
@Service
public class AgentAfterSaleStore {
    public static class Rejected extends RuntimeException {
        public final String code; public final int status;
        public Rejected(String code,int status){super(code);this.code=code;this.status=status;}
    }
    public static final String RULE="ITEM_NET_168H_DEMO_V1";
    private static final String RULE_SELECT="SELECT *,DATE_FORMAT(effective_at,'%Y-%m-%dT%H:%i:%s.%fZ') effective_instant,DATE_FORMAT(created_at,'%Y-%m-%dT%H:%i:%s.%fZ') created_instant FROM oms_after_sale_rule_version";
    public static boolean withinWindow(Instant received,Instant now){return received!=null && !now.isBefore(received) && !now.isAfter(received.plus(Duration.ofHours(168)));}
    public static boolean withinWindow(Instant received,Instant now,long hours){return received!=null && !now.isBefore(received) && !now.isAfter(received.plus(Duration.ofHours(hours)));}
    public Map<String,Object> activeRule(boolean lock){
        if(lock && first("SELECT id FROM oms_after_sale_rule_version WHERE rule_code='ITEM_NET_DEMO' AND version_no=1 FOR SHARE")==null)throw new Rejected("RETURN_RULE_UNAVAILABLE",503);
        var rule=first(RULE_SELECT+" WHERE rule_code='ITEM_NET_DEMO' AND effective_at<=UTC_TIMESTAMP(6) ORDER BY effective_at DESC,version_no DESC LIMIT 1");
        if(rule==null)throw new Rejected("RETURN_RULE_UNAVAILABLE",503);return rule;
    }
    public String ruleVersion(Map<String,Object> rule){return number(rule.get("version_no"))==1?RULE:"ITEM_NET_DEMO_V"+rule.get("version_no");}
    public List<Map<String,Object>> ruleVersions(){return db.queryForList(RULE_SELECT+" WHERE rule_code='ITEM_NET_DEMO' ORDER BY version_no DESC LIMIT 100").stream().map(this::ruleView).toList();}
    public Map<String,Object> ruleView(Map<String,Object> row){var value=parse(row.get("rules_json"));return map("id",row.get("id").toString(),"version_no",row.get("version_no"),"rule_version",ruleVersion(row),"return_window_hours",value.path("return_window_hours").asInt(),"allow_shipped",value.path("allow_shipped").asBoolean(),"amount_basis","PAID_ITEM_NET","is_simulated",true,"effective_at",row.get("effective_instant"),"note",value.path("note").asText(),"created_by",row.get("created_by").toString(),"created_at",time(row.get("created_at")));}
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public Object publishRule(long staff,String key,JsonNode body){
        key=UUID.fromString(key).toString();
        if(!body.isObject() || body.size()!=5 || !body.has("expected_latest_version") || !body.has("return_window_hours") || !body.has("allow_shipped") || !body.has("effective_at") || !body.has("note"))throw new Rejected("VALIDATION_ERROR",422);
        if(!body.path("expected_latest_version").isIntegralNumber() || !body.path("return_window_hours").isIntegralNumber() || !body.path("return_window_hours").canConvertToInt() || body.path("return_window_hours").asInt()<1 || body.path("return_window_hours").asInt()>720 || !body.path("allow_shipped").isBoolean())throw new Rejected("VALIDATION_ERROR",422);
        String note=body.path("note").asText();text(note,500,true);Instant effective;
        try{effective=Instant.parse(body.path("effective_at").asText());}catch(Exception e){throw new Rejected("VALIDATION_ERROR",422);}
        String hash;
        try{hash=java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(("PUBLISH_RETURN_RULE:"+encode(body)).getBytes(java.nio.charset.StandardCharsets.UTF_8)));}catch(Exception e){throw new IllegalStateException(e);}
        db.update("INSERT INTO oms_agent_staff_command(staff_id,command_key,command_hash) VALUES(?,?,?) ON DUPLICATE KEY UPDATE staff_id=staff_id",staff,key,hash);
        var command=first("SELECT * FROM oms_agent_staff_command WHERE staff_id=? AND command_key=? FOR UPDATE",staff,key);
        if(!hash.equals(command.get("command_hash")))throw new Rejected("IDEMPOTENCY_CONFLICT",409);
        if(command.get("result_json")!=null)return parse(command.get("result_json"));
        first("SELECT id FROM oms_after_sale_rule_version WHERE rule_code='ITEM_NET_DEMO' AND version_no=1 FOR UPDATE");
        long latest=db.queryForObject("SELECT MAX(version_no) FROM oms_after_sale_rule_version WHERE rule_code='ITEM_NET_DEMO'",Long.class);
        if(body.path("expected_latest_version").asLong()!=latest)throw new Rejected("VERSION_CONFLICT",409);
        if(effective.isAfter(Instant.now().plus(Duration.ofDays(366))) || effective.isBefore(Instant.now().minus(Duration.ofMinutes(10))))throw new Rejected("RULE_EFFECTIVE_TIME_INVALID",422);
        var current=activeRule(false);if(effective.isBefore(Instant.parse(current.get("effective_instant").toString())))throw new Rejected("RULE_EFFECTIVE_TIME_INVALID",422);
        var rules=map("return_window_hours",body.path("return_window_hours").asInt(),"allow_shipped",body.path("allow_shipped").asBoolean(),"amount_basis","PAID_ITEM_NET","is_simulated",true,"note",note);
        db.update("INSERT INTO oms_after_sale_rule_version(rule_code,version_no,rules_json,effective_at,created_by) VALUES('ITEM_NET_DEMO',?,?,?,?)",latest+1,encode(rules),LocalDateTime.ofInstant(effective,ZoneOffset.UTC),staff);
        var added=first(RULE_SELECT+" WHERE rule_code='ITEM_NET_DEMO' AND version_no=?",latest+1);var result=ruleView(added);
        audit("STAFF",staff,"PUBLISH_RETURN_RULE",number(added.get("id")),key,ruleView(current),result,note);
        db.update("UPDATE oms_agent_staff_command SET result_json=? WHERE staff_id=? AND command_key=?",encode(result),staff,key);return result;
    }
    /** Timeline validation is performed under the shipment lock, before any mutation. */
    public static void validateNodeTime(Instant occurred,Instant now,Instant created,Instant shipped,Instant previous,Instant next){
        if(created==null || (shipped!=null && shipped.isBefore(created)))throw new Rejected("BUSINESS_DATA_INVALID",409);
        Instant start=shipped==null?created:shipped;
        if(occurred==null || occurred.isBefore(start) || occurred.isAfter(now)
            || (previous!=null && occurred.isBefore(previous)) || (next!=null && occurred.isAfter(next)))
            throw new Rejected("LOGISTICS_TIME_INVALID",422);
    }
    private final JdbcTemplate db; private final ObjectMapper json;
    public AgentAfterSaleStore(JdbcTemplate db,ObjectMapper json){this.db=db;this.json=json.copy().setSerializationInclusion(com.fasterxml.jackson.annotation.JsonInclude.Include.ALWAYS);}
    public static Map<String,Object> map(Object... fields){Map<String,Object> m=new LinkedHashMap<>();for(int i=0;i<fields.length;i+=2)m.put((String)fields[i],fields[i+1]);return m;}
    public static long number(Object value){return ((Number)value).longValue();}
    public static String money(Object value){return ((BigDecimal)value).setScale(2,java.math.RoundingMode.UNNECESSARY).toPlainString();}
    public static String time(Object value){if(value==null)return null;if(value instanceof Timestamp t)return t.toInstant().toString();if(value instanceof LocalDateTime d)return d.toInstant(ZoneOffset.UTC).toString();if(value instanceof java.util.Date d)return d.toInstant().toString();throw new Rejected("BUSINESS_DATA_INVALID",409);}
    /** Original OMS DATETIME values are local China time; extension timelines are explicit UTC. */
    public static String legacyTime(Object value){
        if(value==null)return null;
        LocalDateTime local=value instanceof LocalDateTime d?d:value instanceof Timestamp t?t.toLocalDateTime():null;
        if(local!=null)return local.atZone(ZoneId.of("Asia/Shanghai")).toInstant().toString();
        if(value instanceof java.util.Date d)return d.toInstant().toString();
        throw new Rejected("BUSINESS_DATA_INVALID",409);
    }
    public Map<String,Object> first(String sql,Object... args){var list=db.queryForList(sql,args);return list.isEmpty()?null:list.get(0);}
    public String encode(Object value){try{return json.writeValueAsString(value);}catch(Exception e){throw new IllegalStateException("Business projection invalid",e);}}
    public JsonNode tree(Object value){return json.valueToTree(value);}
    public JsonNode parse(Object value){try{return json.readTree(value.toString());}catch(Exception e){throw new IllegalStateException("Business projection invalid",e);}}
    public static void text(String value,int max,boolean required){if(value==null || value.length()>max || (required && value.isBlank()))throw new Rejected("VALIDATION_ERROR",422);}
    public static void version(JsonNode input,long actual){if(!input.path("expected_version").isIntegralNumber() || input.path("expected_version").asLong()!=actual)throw new Rejected("VERSION_CONFLICT",409);}
    public Map<String,Object> order(long member,long order,boolean lock){
        var row=first("SELECT * FROM oms_order WHERE id=? AND member_id=? AND delete_status=0"+(lock?" FOR UPDATE":""),order,member);
        if(row==null)throw new Rejected("NOT_FOUND",404);return row;
    }
    public Map<String,Object> eligibility(long member,long oid,long iid){
        return eligibility(member,oid,iid,activeRule(false));
    }
    public Map<String,Object> eligibility(long member,long oid,long iid,Map<String,Object> rule){
        var rules=parse(rule.get("rules_json"));long hours=rules.path("return_window_hours").asLong();
        var order=order(member,oid,false);var item=first("SELECT * FROM oms_order_item WHERE id=? AND order_id=?",iid,oid);
        if(item==null)throw new Rejected("NOT_FOUND",404);
        int status=((Number)order.get("status")).intValue();String fail=null,deadline=null;
        if(status!=2 && status!=3)fail="订单须已发货或已完成";
        if(status==2 && !rules.path("allow_shipped").asBoolean())fail="当前规则要求收货后再申请退货";
        if(status==3){
            if(order.get("receive_time")==null)fail="无可靠收货时间，请联系人工";
            else {Instant received=Instant.parse(legacyTime(order.get("receive_time")));Instant end=received.plus(Duration.ofHours(hours));deadline=end.toString();if(!withinWindow(received,Instant.now(),hours))fail="收货时间无效或已超过"+hours+"小时退货期限";}
        }
        // Legacy product-only rows cannot safely be attributed to a split order item.
        long legacy=db.queryForObject("SELECT COUNT(*) FROM oms_order_return_apply a LEFT JOIN oms_agent_return x ON x.return_id=a.id WHERE a.order_id=? AND a.product_id=? AND a.status<>3 AND x.return_id IS NULL",Long.class,oid,item.get("product_id"));
        if(legacy>0)fail="LEGACY_AMBIGUOUS：历史退货明细无法安全对应，请联系人工";
        Long used=db.queryForObject("SELECT COALESCE(SUM(quantity),0) FROM oms_agent_return WHERE order_item_id=? AND stage<>'REJECTED'",Long.class,iid);
        int purchased=item.get("product_quantity")==null?0:((Number)item.get("product_quantity")).intValue();
        long remaining=(long)purchased-used;
        if(remaining<0 || !(item.get("real_amount") instanceof BigDecimal amount) || amount.signum()<0)throw new Rejected("BUSINESS_DATA_INVALID",409);
        if(remaining==0 && fail==null)fail="该明细已无可申请数量";
        List<Map<String,Object>> reasons=db.queryForList("SELECT id,name FROM oms_order_return_reason WHERE status=1 ORDER BY sort DESC,id LIMIT 100").stream().map(r->map("code",r.get("id").toString(),"label",Objects.toString(r.get("name"),""))).toList();
        if(reasons.isEmpty() && fail==null)fail="暂无有效退货原因，请联系人工";
        return map("order_id",Long.toString(oid),"order_item_id",Long.toString(iid),"eligible",fail==null,"remaining_quantity",(int)remaining,
            "remaining_amount",money(((BigDecimal)item.get("real_amount")).multiply(BigDecimal.valueOf(remaining))),"deadline",deadline,"reason_codes",reasons,"failure_reason",fail,"rule_version",ruleVersion(rule));
    }
    public Map<String,Object> returnSummary(Map<String,Object> row){
        var summary=map("id",row.get("id").toString(),"order_item_id",row.get("order_item_id")==null?null:row.get("order_item_id").toString(),
            "quantity",row.get("product_count"),"requested_amount",money(row.get("requested_amount")!=null?row.get("requested_amount"):((BigDecimal)row.get("product_real_price")).multiply(BigDecimal.valueOf(number(row.get("product_count"))))),
            "approved_amount",row.get("approved_amount")==null?null:money(row.get("approved_amount")),"stage",Objects.toString(row.get("stage"),"LEGACY"),
            "legacy_status",row.get("status"),"reason",Objects.toString(row.get("reason"),""),"version",row.get("version")==null?1L:row.get("version"),
            "is_simulated",row.get("stage")!=null,"created_at",legacyTime(row.get("create_time")),"updated_at",row.get("updated_at")==null?legacyTime(row.get("create_time")):time(row.get("updated_at")));
        summary.put("rule_version",row.get("rule_version"));
        if(row.get("company_address_snapshot")!=null)summary.put("company_address",parse(row.get("company_address_snapshot")));
        return summary;
    }
    private String returnSql(){return "SELECT a.*,x.order_item_id,x.requested_amount,x.approved_amount,x.stage,x.version,x.updated_at,x.rule_version,x.proof_metadata,x.company_address_snapshot,x.simulated_refund_no FROM oms_order_return_apply a LEFT JOIN oms_agent_return x ON x.return_id=a.id";}
    public List<Map<String,Object>> returns(long member,long oid){order(member,oid,false);return db.queryForList(returnSql()+" WHERE a.order_id=? ORDER BY a.id DESC LIMIT 100",oid).stream().map(this::returnSummary).toList();}
    public Map<String,Object> orderSummary(long member,long oid){
        var o=order(member,oid,false);
        var rules=parse(activeRule(false).get("rules_json"));
        var items=db.queryForList("SELECT i.*,COALESCE((SELECT SUM(x.quantity) FROM oms_agent_return x WHERE x.order_item_id=i.id AND x.stage<>'REJECTED'),0) applied,EXISTS(SELECT 1 FROM oms_order_return_apply a LEFT JOIN oms_agent_return x ON x.return_id=a.id WHERE a.order_id=i.order_id AND a.product_id=i.product_id AND a.status<>3 AND x.return_id IS NULL) ambiguous FROM oms_order_item i WHERE i.order_id=? ORDER BY i.id LIMIT 100",oid);
        return summary(o,rules,items,returns(member,oid));
    }
    /** A page uses four reads, retaining member ownership and per-order limits. */
    @Transactional(readOnly=true)
    public List<Map<String,Object>> orderSummaries(long member,List<Long> ids){
        if(ids.isEmpty())return List.of();
        if(ids.size()>100 || new HashSet<>(ids).size()!=ids.size())throw new Rejected("VALIDATION_ERROR",422);
        String placeholders=String.join(",",Collections.nCopies(ids.size(),"?"));
        List<Object> ownerArgs=new ArrayList<>();ownerArgs.add(member);ownerArgs.addAll(ids);
        var orders=db.queryForList("SELECT * FROM oms_order WHERE member_id=? AND delete_status=0 AND id IN ("+placeholders+")",ownerArgs.toArray());
        if(orders.size()!=ids.size())throw new Rejected("NOT_FOUND",404);
        var rules=parse(activeRule(false).get("rules_json"));
        var items=db.queryForList("SELECT ranked.* FROM (SELECT i.*,COALESCE((SELECT SUM(x.quantity) FROM oms_agent_return x WHERE x.order_item_id=i.id AND x.stage<>'REJECTED'),0) applied,EXISTS(SELECT 1 FROM oms_order_return_apply a LEFT JOIN oms_agent_return x ON x.return_id=a.id WHERE a.order_id=i.order_id AND a.product_id=i.product_id AND a.status<>3 AND x.return_id IS NULL) ambiguous,ROW_NUMBER() OVER(PARTITION BY i.order_id ORDER BY i.id) page_row FROM oms_order_item i WHERE i.order_id IN ("+placeholders+")) ranked WHERE page_row<=100 ORDER BY order_id,id",ids.toArray());
        String returnPage=returnSql().replace(" FROM oms_order_return_apply",",ROW_NUMBER() OVER(PARTITION BY a.order_id ORDER BY a.id DESC) page_row FROM oms_order_return_apply");
        var returned=db.queryForList("SELECT ranked.* FROM ("+returnPage+" WHERE a.order_id IN ("+placeholders+")) ranked WHERE page_row<=100 ORDER BY order_id,id DESC",ids.toArray());
        Map<Long,Map<String,Object>> byId=new HashMap<>();for(var o:orders)byId.put(number(o.get("id")),o);
        return ids.stream().map(id->summary(byId.get(id),rules,items.stream().filter(i->number(i.get("order_id"))==id).toList(),returned.stream().filter(r->number(r.get("order_id"))==id).map(this::returnSummary).toList())).toList();
    }
    private Map<String,Object> summary(Map<String,Object> o,JsonNode rules,List<Map<String,Object>> items,List<Map<String,Object>> returns){
        long oid=number(o.get("id"));
        List<Map<String,Object>> lines=items.stream().map(i->{
            int quantity=((Number)i.get("product_quantity")).intValue();int orderStatus=((Number)o.get("status")).intValue();
            boolean eligible=((orderStatus==2 && rules.path("allow_shipped").asBoolean()) || (orderStatus==3 && o.get("receive_time")!=null && withinWindow(Instant.parse(legacyTime(o.get("receive_time"))),Instant.now(),rules.path("return_window_hours").asLong()))) && ((Number)i.get("ambiguous")).intValue()==0;
            long remaining=Math.max(0L,quantity-number(i.get("applied")));
            return map("order_item_id",i.get("id").toString(),"product_id",i.get("product_id").toString(),"sku_id",i.get("product_sku_id").toString(),"name",Objects.toString(i.get("product_name"),""),
                "specification",Objects.toString(i.get("product_attr"),""),"quantity",quantity,"unit_price",money(i.get("product_price")),
                "paid_line_amount",money(((BigDecimal)i.get("real_amount")).multiply(BigDecimal.valueOf(quantity))),"returnable_quantity",eligible?(int)remaining:0);
        }).toList();
        int status=((Number)o.get("status")).intValue();String[] labels={"待付款","待发货","已发货","已完成","已关闭","无效订单"};
        String phone=Objects.toString(o.get("receiver_phone"),"");if(phone.length()>7)phone=phone.substring(0,3)+"****"+phone.substring(phone.length()-4);
        String address=Objects.toString(o.get("receiver_name"),"")+" "+phone+" "+Objects.toString(o.get("receiver_province"),"")+Objects.toString(o.get("receiver_city"),"")+Objects.toString(o.get("receiver_region"),"");
        return map("id",Long.toString(oid),"order_sn",o.get("order_sn"),"status",status,"status_label",status>=0&&status<labels.length?labels[status]:"未知状态",
            "pay_amount",money(o.get("pay_amount")),"created_at",legacyTime(o.get("create_time")),"receive_time",legacyTime(o.get("receive_time")),"address_summary",address,
            "lines",lines,"returns",returns,"detail_path","/pages/order/orderDetail?orderId="+oid,"payment_path",status==0?"/pages/money/pay?orderId="+oid:null,"is_simulated",true);
    }
    public Map<String,Object> logistics(long member,long oid){
        var o=order(member,oid,false);var s=first("SELECT * FROM oms_agent_shipment WHERE order_id=?",oid);
        List<Map<String,Object>> nodes=s==null?List.of():db.queryForList("SELECT * FROM oms_agent_shipment_event WHERE shipment_id=? ORDER BY occurred_at,id LIMIT 200",s.get("id")).stream().map(this::node).toList();
        return map("order_id",Long.toString(oid),"shipment_id",s==null?null:s.get("id").toString(),"carrier",s==null?o.get("delivery_company"):s.get("carrier"),
            "tracking_no",s==null?o.get("delivery_sn"):s.get("tracking_no"),"nodes",nodes,"version",s==null?1:s.get("version"),"is_simulated",true);
    }
    private Map<String,Object> node(Map<String,Object> n){return map("id",n.get("id").toString(),"occurred_at",time(n.get("occurred_at")),"location",n.get("location"),"description",n.get("description"),"version",n.get("version"),"is_simulated",true);}
    public Map<String,Object> staffReturn(long rid){
        var r=first(returnSql()+" WHERE a.id=?",rid);if(r==null)throw new Rejected("NOT_FOUND",404);
        var result=new LinkedHashMap<>(returnSummary(r));result.put("order_id",r.get("order_id").toString());result.put("description",Objects.toString(r.get("description"),""));
        result.put("company_address",r.get("company_address_snapshot")==null?null:parse(r.get("company_address_snapshot")));
        result.put("proofs",r.get("proof_metadata")==null?List.of():parse(r.get("proof_metadata")));
        result.put("handle_note",r.get("handle_note"));result.put("receive_note",r.get("receive_note"));
        result.put("simulated_refund_no",r.get("simulated_refund_no"));return result;
    }
    public long memberForOrder(long oid){var o=first("SELECT member_id FROM oms_order WHERE id=? AND delete_status=0",oid);if(o==null)throw new Rejected("NOT_FOUND",404);return number(o.get("member_id"));}
    private long insert(String sql,Object... values){var key=new GeneratedKeyHolder();db.update(c->{var p=c.prepareStatement(sql,Statement.RETURN_GENERATED_KEYS);for(int i=0;i<values.length;i++)p.setObject(i+1,values[i]);return p;},key);return Objects.requireNonNull(key.getKey()).longValue();}
    public void audit(String actor,long id,String operation,long resource,String request,Object before,Object after,String note){db.update("INSERT INTO oms_agent_business_audit(actor_kind,actor_id,operation,resource_id,request_id,before_json,after_json,reason) VALUES(?,?,?,?,?,?,?,?)",actor,id,operation,resource,request,before==null?null:encode(before),after==null?null:encode(after),note);}
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public JsonNode staffCommand(long staff,String key,String command,long target,JsonNode input){
        UUID.fromString(key);String fingerprint;
        try {fingerprint=HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(encode(map("command",command,"target",target,"body",input)).getBytes(java.nio.charset.StandardCharsets.UTF_8)));}catch(Exception e){throw new IllegalStateException(e);}
        db.update("INSERT INTO oms_agent_staff_command(staff_id,command_key,command_hash) VALUES(?,?,?) ON DUPLICATE KEY UPDATE command_key=VALUES(command_key)",staff,key,fingerprint);
        var cached=first("SELECT * FROM oms_agent_staff_command WHERE staff_id=? AND command_key=? FOR UPDATE",staff,key);
        if(!fingerprint.equals(cached.get("command_hash")))throw new Rejected("IDEMPOTENCY_CONFLICT",409);
        if(cached.get("result_json")!=null)return parse(cached.get("result_json"));
        Object result;
        if(command.equals("REVIEW") || command.equals("RECEIVE") || command.equals("REFUND"))result=transition(staff,key,command,target,input);
        else result=editLogistics(staff,key,command,target,input);
        db.update("UPDATE oms_agent_staff_command SET result_json=? WHERE staff_id=? AND command_key=?",encode(result),staff,key);return parse(encode(result));
    }
    private Object transition(long staff,String key,String command,long rid,JsonNode input){
        var pre=first("SELECT order_id FROM oms_order_return_apply WHERE id=?",rid);if(pre==null)throw new Rejected("NOT_FOUND",404);
        long oid=number(pre.get("order_id"));order(memberForOrder(oid),oid,true);
        var r=first(returnSql()+" WHERE a.id=? FOR UPDATE",rid);
        if(r.get("stage")==null)throw new Rejected("LEGACY_MANUAL_REQUIRED",409);
        version(input,number(r.get("version")));String note=input.path("note").asText();text(note,1000,true);
        String stage=r.get("stage").toString(),next;Object before=staffReturn(rid);
        if(command.equals("REVIEW")){
            if(!stage.equals("REQUESTED"))throw new Rejected("RETURN_STAGE_CONFLICT",409);
            String decision=input.path("decision").asText();
            if(decision.equals("REJECT")){
                if(input.has("approved_amount") || input.has("company_address_id"))throw new Rejected("VALIDATION_ERROR",422);
                next="REJECTED";db.update("UPDATE oms_order_return_apply SET status=3,handle_note=?,handle_man=?,handle_time=NOW() WHERE id=?",note,Long.toString(staff),rid);
            }else if(decision.equals("APPROVE")){
                String amount=input.path("approved_amount").asText();if(!amount.matches("(0|[1-9][0-9]{0,15})\\.[0-9]{2}"))throw new Rejected("VALIDATION_ERROR",422);
                BigDecimal approved=new BigDecimal(amount);if(approved.compareTo((BigDecimal)r.get("requested_amount"))>0)throw new Rejected("RETURN_AMOUNT_EXCEEDED",409);
                long aid=decimalId(input.path("company_address_id"));var a=first("SELECT * FROM oms_company_address WHERE id=?",aid);if(a==null)throw new Rejected("RETURN_ADDRESS_UNAVAILABLE",409);
                next="APPROVED";db.update("UPDATE oms_agent_return SET approved_amount=?,company_address_snapshot=? WHERE return_id=?",approved,encode(map("id",a.get("id").toString(),"label",String.join(" ",Objects.toString(a.get("address_name"),""),Objects.toString(a.get("name"),""),Objects.toString(a.get("phone"),""),Objects.toString(a.get("province"),""),Objects.toString(a.get("city"),""),Objects.toString(a.get("region"),""),Objects.toString(a.get("detail_address"),"")))),rid);
                db.update("UPDATE oms_order_return_apply SET status=1,return_amount=?,company_address_id=?,handle_note=?,handle_man=?,handle_time=NOW() WHERE id=?",approved,aid,note,Long.toString(staff),rid);
            }else throw new Rejected("VALIDATION_ERROR",422);
        }else if(command.equals("RECEIVE")){
            if(!stage.equals("APPROVED"))throw new Rejected("RETURN_STAGE_CONFLICT",409);next="GOODS_RECEIVED";
            db.update("UPDATE oms_order_return_apply SET receive_note=?,receive_man=?,receive_time=NOW() WHERE id=?",note,Long.toString(staff),rid);
        }else{
            if(!stage.equals("GOODS_RECEIVED"))throw new Rejected("RETURN_STAGE_CONFLICT",409);next="SIMULATED_REFUNDED";
            // Demonstration ledger only: no balance credit, payment callback or external refund request.
            db.update("UPDATE oms_agent_return SET simulated_refund_no=? WHERE return_id=?",UUID.randomUUID().toString(),rid);
            db.update("UPDATE oms_order_return_apply SET status=2 WHERE id=?",rid);
        }
        db.update("UPDATE oms_agent_return SET stage=?,version=version+1,updated_at=UTC_TIMESTAMP(6) WHERE return_id=?",next,rid);
        var result=staffReturn(rid);audit("STAFF",staff,command,rid,key,before,result,note);return result;
    }
    public static long decimalId(JsonNode n){try{if(!n.isTextual() || !n.asText().matches("[1-9][0-9]{0,18}"))throw new IllegalArgumentException();return Long.parseLong(n.asText());}catch(Exception e){throw new Rejected("VALIDATION_ERROR",422);}}
    private Object editLogistics(long staff,String key,String command,long target,JsonNode input){
        if(command.equals("SHIPMENT")){
            long member=memberForOrder(target);var o=order(member,target,true);int status=((Number)o.get("status")).intValue();if(status!=2 && status!=3)throw new Rejected("ORDER_NOT_SHIPPED",409);
            var before=logistics(member,target);var s=first("SELECT * FROM oms_agent_shipment WHERE order_id=? FOR UPDATE",target);version(input,s==null?1:number(s.get("version")));
            String carrier=input.path("carrier").asText(),tracking=input.path("tracking_no").asText();text(carrier,100,true);text(tracking,100,true);
            if(s==null)db.update("INSERT INTO oms_agent_shipment(order_id,carrier,tracking_no,version) VALUES(?,?,?,2)",target,carrier,tracking);
            else db.update("UPDATE oms_agent_shipment SET carrier=?,tracking_no=?,version=version+1,updated_at=UTC_TIMESTAMP(6) WHERE id=?",carrier,tracking,s.get("id"));
            var after=logistics(member,target);audit("STAFF",staff,command,target,key,before,after,"维护演示物流");return after;
        }
        var s=command.equals("NODE")?first("SELECT * FROM oms_agent_shipment WHERE id=? FOR UPDATE",target):first("SELECT s.* FROM oms_agent_shipment s JOIN oms_agent_shipment_event e ON e.shipment_id=s.id WHERE e.id=? FOR UPDATE",target);
        if(s==null)throw new Rejected("NOT_FOUND",404);
        Instant occurred;try{occurred=OffsetDateTime.parse(input.path("occurred_at").asText()).toInstant();}catch(Exception e){throw new Rejected("VALIDATION_ERROR",422);}
        String location=input.path("location").asText(),description=input.path("description").asText();text(location,200,true);text(description,1000,true);
        var timeline=db.queryForList("SELECT id,occurred_at FROM oms_agent_shipment_event WHERE shipment_id=? ORDER BY occurred_at,id",s.get("id"));
        var order=order(memberForOrder(number(s.get("order_id"))),number(s.get("order_id")),false);
        if(!Set.of(2,3).contains(((Number)order.get("status")).intValue()))throw new Rejected("ORDER_NOT_SHIPPED",409);
        Instant previous=null,next=null;
        if(command.equals("NODE")){
            if(!timeline.isEmpty())previous=Instant.parse(time(timeline.get(timeline.size()-1).get("occurred_at")));
        }else if(command.equals("EDIT_NODE")){
            int index=-1;for(int i=0;i<timeline.size();i++)if(number(timeline.get(i).get("id"))==target){index=i;break;}
            if(index<0)throw new Rejected("NOT_FOUND",404);
            if(index>0)previous=Instant.parse(time(timeline.get(index-1).get("occurred_at")));
            if(index+1<timeline.size())next=Instant.parse(time(timeline.get(index+1).get("occurred_at")));
        }else throw new Rejected("VALIDATION_ERROR",422);
        validateNodeTime(occurred,Instant.now(),order.get("create_time")==null?null:Instant.parse(legacyTime(order.get("create_time"))),
            order.get("delivery_time")==null?null:Instant.parse(legacyTime(order.get("delivery_time"))),previous,next);
        Object before=null;long nid;String reason="新增演示物流节点";
        if(command.equals("NODE")){
            if(!input.path("expected_shipment_version").isIntegralNumber() || input.path("expected_shipment_version").asLong()!=number(s.get("version")))throw new Rejected("VERSION_CONFLICT",409);
            if(db.queryForObject("SELECT COUNT(*) FROM oms_agent_shipment_event WHERE shipment_id=?",Long.class,s.get("id"))>=200)throw new Rejected("LOGISTICS_CAPACITY_EXCEEDED",409);
            nid=insert("INSERT INTO oms_agent_shipment_event(shipment_id,occurred_at,location,description) VALUES(?,?,?,?)",s.get("id"),LocalDateTime.ofInstant(occurred,ZoneOffset.UTC),location,description);
        }else if(command.equals("EDIT_NODE")){
            var n=first("SELECT * FROM oms_agent_shipment_event WHERE id=? FOR UPDATE",target);version(input,number(n.get("version")));before=node(n);reason=input.path("reason").asText();text(reason,500,true);nid=target;
            db.update("UPDATE oms_agent_shipment_event SET occurred_at=?,location=?,description=?,version=version+1 WHERE id=?",LocalDateTime.ofInstant(occurred,ZoneOffset.UTC),location,description,nid);
        }else throw new Rejected("VALIDATION_ERROR",422);
        db.update("UPDATE oms_agent_shipment SET version=version+1,updated_at=UTC_TIMESTAMP(6) WHERE id=?",s.get("id"));
        var after=node(first("SELECT * FROM oms_agent_shipment_event WHERE id=?",nid));audit("STAFF",staff,command,nid,key,before,after,reason);return after;
    }
}
