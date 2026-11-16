package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.model.OmsCartItem;
import com.macro.mall.model.UmsMember;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.*;

/** ADD_CART authority lives here. A quote never reserves stock or charges money. */
@Service
public class AgentCartService {
    public static class Rejected extends RuntimeException {
        public final String code;
        public Rejected(String code){super(code);this.code=code;}
    }
    private final JdbcTemplate db;
    private final ObjectMapper json;
    private final CartGuard guard;
    private final OmsCartItemService carts;
    public AgentCartService(JdbcTemplate db,ObjectMapper json,CartGuard guard,OmsCartItemService carts){
        this.db=db;this.json=json.copy().setSerializationInclusion(com.fasterxml.jackson.annotation.JsonInclude.Include.ALWAYS);this.guard=guard;this.carts=carts;
    }
    static Map<String,Object> map(Object... fields){
        Map<String,Object> result=new LinkedHashMap<>();for(int i=0;i<fields.length;i+=2)result.put((String)fields[i],fields[i+1]);return result;
    }
    static void fields(JsonNode node,String... names){
        if(!node.isObject() || !new HashSet<>(Arrays.asList(names)).equals(fieldNames(node)))throw new Rejected("VALIDATION_ERROR");
    }
    private static Set<String> fieldNames(JsonNode node){Set<String> set=new HashSet<>();node.fieldNames().forEachRemaining(set::add);return set;}
    static long id(JsonNode n){
        if(!n.isTextual() || !n.asText().matches("[1-9][0-9]{0,18}"))throw new Rejected("VALIDATION_ERROR");
        try{return Long.parseLong(n.asText());}catch(NumberFormatException e){throw new Rejected("VALIDATION_ERROR");}
    }
    public JsonNode parameters(JsonNode input){
        fields(input,"action_type","items");
        if(!"ADD_CART".equals(input.path("action_type").asText()))throw new Rejected("FEATURE_NOT_ACTIVE");
        JsonNode items=input.path("items");
        if(!items.isArray() || items.isEmpty() || items.size()>20)throw new Rejected("VALIDATION_ERROR");
        Set<Long> seen=new HashSet<>();
        for(JsonNode line:items){
            fields(line,"product_id","sku_id","quantity");id(line.path("product_id"));long sku=id(line.path("sku_id"));
            if(!seen.add(sku) || !line.path("quantity").isIntegralNumber() || !line.path("quantity").canConvertToInt()
                    || line.path("quantity").asInt()<1 || line.path("quantity").asInt()>999)throw new Rejected("VALIDATION_ERROR");
        }
        return input.deepCopy();
    }
    public String canonical(JsonNode node){
        try{return new ObjectMapper().writeValueAsString(sorted(node));}catch(Exception e){throw new Rejected("VALIDATION_ERROR");}
    }
    private Object sorted(JsonNode node){
        if(node.isObject()){Map<String,Object> result=new TreeMap<>();node.fields().forEachRemaining(e->result.put(e.getKey(),sorted(e.getValue())));return result;}
        if(node.isArray()){List<Object> result=new ArrayList<>();node.forEach(n->result.add(sorted(n)));return result;}
        if(node.isTextual())return node.asText();if(node.isIntegralNumber())return node.longValue();
        if(node.isBoolean())return node.booleanValue();if(node.isNull())return null;throw new Rejected("VALIDATION_ERROR");
    }
    public String hash(long member,JsonNode parameters){
        return AgentCartSecurity.sha(canonical(json.valueToTree(map("schema_version","agent-action-v1","action_type",parameters.path("action_type").asText(),"member_id",Long.toString(member),"parameters",parameters))).getBytes(StandardCharsets.UTF_8));
    }
    private JsonNode parse(String encoded){
        try{return json.readTree(encoded);}catch(Exception ex){throw new IllegalStateException("Persisted cart operation is invalid");}
    }
    private String encode(Object object){try{return json.writeValueAsString(object);}catch(Exception ex){throw new IllegalStateException("Cart projection is invalid");}}
    private Map<String,Object> first(String sql,Object... args){var rows=db.queryForList(sql,args);return rows.isEmpty()?null:rows.get(0);}
    private String money(BigDecimal value){return value.setScale(2,RoundingMode.UNNECESSARY).toPlainString();}
    @Transactional(readOnly=true,isolation=org.springframework.transaction.annotation.Isolation.REPEATABLE_READ)
    public JsonNode snapshot(long member,int limit,String after){
        if(member<1 || limit<1 || limit>20)throw new Rejected("VALIDATION_ERROR");
        long last=0;
        if(after!=null){
            if(!after.matches("[1-9][0-9]{0,18}"))throw new Rejected("VALIDATION_ERROR");
            try{last=Long.parseLong(after);}catch(NumberFormatException ex){throw new Rejected("VALIDATION_ERROR");}
        }
        var rows=db.queryForList("SELECT c.id,c.product_id,c.product_sku_id,c.quantity,c.product_name,c.product_attr,"
            +"p.name current_name,p.publish_status,p.delete_status product_deleted,s.price,s.stock,s.lock_stock,s.sp_data "
            +"FROM oms_cart_item c LEFT JOIN pms_product p ON p.id=c.product_id "
            +"LEFT JOIN pms_sku_stock s ON s.id=c.product_sku_id AND s.product_id=c.product_id "
            +"WHERE c.member_id=? AND c.delete_status=0 AND c.id>? ORDER BY c.id LIMIT ?",member,last,limit+1);
        boolean more=rows.size()>limit;List<Map<String,Object>> items=new ArrayList<>();
        for(var row:rows.subList(0,Math.min(limit,rows.size()))){
            int quantity=row.get("quantity")==null?0:Math.max(0,((Number)row.get("quantity")).intValue());
            long available=row.get("stock")==null?0:Math.max(0,((Number)row.get("stock")).longValue()-(row.get("lock_stock")==null?0:((Number)row.get("lock_stock")).longValue()));
            BigDecimal price=row.get("price") instanceof BigDecimal p && p.signum()>=0?p:null;
            boolean valid=quantity>0 && quantity<=999 && Integer.valueOf(1).equals(row.get("publish_status"))
                && Integer.valueOf(0).equals(row.get("product_deleted")) && price!=null && available>=quantity;
            String name=Objects.toString(row.get("current_name"),Objects.toString(row.get("product_name"),""));
            String spec=Objects.toString(row.get("sp_data"),Objects.toString(row.get("product_attr"),""));
            items.add(map("id",row.get("id").toString(),"product_id",row.get("product_id")==null?null:row.get("product_id").toString(),
                "sku_id",row.get("product_sku_id")==null?null:row.get("product_sku_id").toString(),
                "name",name.substring(0,Math.min(500,name.length())),"specification",spec.substring(0,Math.min(300,spec.length())),
                "quantity",quantity,"available",valid,"available_quantity",(int)Math.min(Integer.MAX_VALUE,available),"unit_price",price==null?null:money(price)));
        }
        return json.valueToTree(map("items",items,"next_cursor",more?items.get(items.size()-1).get("id"):null,"has_more",more,"checked_at",Instant.now().toString()));
    }
    private List<Map<String,Object>> lines(JsonNode parameters,boolean lock){
        // All product then all SKU locks are sorted independently of display/input order.
        Map<Long,Map<String,Object>> products=new TreeMap<>(),skus=new TreeMap<>();
        parameters.path("items").forEach(line->{products.put(id(line.path("product_id")),null);skus.put(id(line.path("sku_id")),null);});
        for(Long pid:new ArrayList<>(products.keySet()))products.put(pid,first("SELECT * FROM pms_product WHERE id=?"+(lock?" FOR SHARE":""),pid));
        for(Long sid:new ArrayList<>(skus.keySet()))skus.put(sid,first("SELECT * FROM pms_sku_stock WHERE id=?"+(lock?" FOR UPDATE":""),sid));
        List<Map<String,Object>> output=new ArrayList<>();
        for(JsonNode item:parameters.path("items")){
            long pid=id(item.path("product_id")),sid=id(item.path("sku_id"));int quantity=item.path("quantity").asInt();
            var product=products.get(pid);var sku=skus.get(sid);
            if(product==null || sku==null || ((Number)sku.get("product_id")).longValue()!=pid
                    || !Integer.valueOf(0).equals(product.get("delete_status")) || !Integer.valueOf(1).equals(product.get("publish_status")))throw new Rejected("PRODUCT_UNAVAILABLE");
            if(!(sku.get("price") instanceof BigDecimal price) || price.signum()<0 || sku.get("stock")==null)throw new Rejected("PRODUCT_UNAVAILABLE");
            int available=((Number)sku.get("stock")).intValue()-(sku.get("lock_stock")==null?0:((Number)sku.get("lock_stock")).intValue());
            if(available<quantity)throw new Rejected("STOCK_INSUFFICIENT");
            String spec=Objects.toString(sku.get("sp_data"),"");
            if(spec.length()>300)throw new Rejected("PRODUCT_UNAVAILABLE");
            output.add(map("product_id",Long.toString(pid),"sku_id",Long.toString(sid),"name",Objects.toString(product.get("name"),""),"specification",spec,"quantity",quantity,"unit_price",money(price),"line_payable",money(price.multiply(BigDecimal.valueOf(quantity)))));
        }
        return output;
    }
    @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public JsonNode quote(long member,UUID quoteId,JsonNode input){
        JsonNode parameters=parameters(input);String hash=hash(member,parameters);
        var old=first("SELECT * FROM oms_agent_quote WHERE quote_id=?",quoteId.toString());
        if(old!=null){if(((Number)old.get("member_id")).longValue()!=member || !hash.equals(old.get("parameters_hash")))throw new Rejected("IDEMPOTENCY_CONFLICT");return parse(old.get("pricing_snapshot").toString());}
        var lines=lines(parameters,false);BigDecimal total=lines.stream().map(l->new BigDecimal(l.get("line_payable").toString())).reduce(BigDecimal.ZERO,BigDecimal::add);
        Instant expiry=Instant.now().plusSeconds(300);
        var pricing=map("subtotal",money(total),"promotion_discount","0.00","coupon_discount","0.00","points_discount","0.00","freight","0.00","payable",money(total),"currency","CNY");
        var quote=map("quote_id",quoteId.toString(),"parameters",parameters,"parameters_hash",hash,"lines",lines,"pricing",pricing,"address_summary",null,"rule_version","CART_SKU_PRICE_V1","expires_at",expiry.toString());
        db.update("INSERT INTO oms_agent_quote(quote_id,member_id,action_type,parameters_hash,parameters_json,pricing_snapshot,total_amount,rule_version,expires_at) VALUES(?,?,'ADD_CART',?,?,?,?,?,?) ON DUPLICATE KEY UPDATE quote_id=VALUES(quote_id)",quoteId.toString(),member,hash,canonical(parameters),encode(quote),total,"CART_SKU_PRICE_V1",LocalDateTime.ofInstant(expiry,ZoneOffset.UTC));
        var row=first("SELECT * FROM oms_agent_quote WHERE quote_id=? FOR UPDATE",quoteId.toString());
        if(((Number)row.get("member_id")).longValue()!=member || !hash.equals(row.get("parameters_hash")))throw new Rejected("IDEMPOTENCY_CONFLICT");
        return parse(row.get("pricing_snapshot").toString());
    }
    Map<String,Object> operation(String operation,JsonNode receipt){
        db.update("INSERT INTO oms_agent_operation(operation_no,member_id,action_type,parameters_hash,state,confirmed_at,confirmation_audit_id,control_epoch) VALUES(?,?,?,?,'IN_TX',?,?,?) ON DUPLICATE KEY UPDATE operation_no=VALUES(operation_no)",operation,id(receipt.path("member_id")),receipt.path("action_type").asText(),receipt.path("parameters_hash").asText(),LocalDateTime.ofInstant(Instant.parse(receipt.path("confirmed_at").asText()),ZoneOffset.UTC),receipt.path("confirmation_audit_id").asText(),receipt.path("control_epoch").asLong());
        var row=first("SELECT * FROM oms_agent_operation WHERE operation_no=? FOR UPDATE",operation);
        if(((Number)row.get("member_id")).longValue()!=id(receipt.path("member_id")) || !receipt.path("parameters_hash").asText().equals(row.get("parameters_hash")) || !receipt.path("action_type").asText().equals(row.get("action_type")) || !receipt.path("confirmation_audit_id").asText().equals(row.get("confirmation_audit_id")))throw new Rejected("IDEMPOTENCY_CONFLICT");
        return row;
    }
    private JsonNode result(String operation,String state,String error,Integer quantity){
        return json.valueToTree(map("operation_no",operation,"state",state,"business_type",state.equals("SUCCEEDED")?"CART":null,"business_id",null,"error_code",error,"message",state.equals("SUCCEEDED")?"商品已加入购物车":state.equals("NOT_COMMITTED")?"操作结果核对中":"操作未执行，请重新准备确认","order",null,"return_apply",null,"cart_quantity",quantity,"completed_at",state.equals("NOT_COMMITTED")?null:Instant.now().toString()));
    }
    private JsonNode finish(String operation,String state,String error,Integer quantity){
        JsonNode result=result(operation,state,error,quantity);
        db.update("UPDATE oms_agent_operation SET state=?,result_json=?,error_code=?,business_type=?,completed_at=? WHERE operation_no=?",state,encode(result),error,state.equals("SUCCEEDED")?"CART":null,LocalDateTime.ofInstant(Instant.now(),ZoneOffset.UTC),operation);
        return result;
    }
    @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public JsonNode execute(UmsMember member,JsonNode input,JsonNode receipt){
        fields(input,"operation_no","quote_id","parameters_hash","parameters","confirmation_receipt","proofs");
        String operation=input.path("operation_no").asText();UUID.fromString(operation);UUID.fromString(input.path("quote_id").asText());
        if(!input.path("quote_id").asText().equals(receipt.path("quote_id").asText()))throw new Rejected("CONFIRMATION_INVALID");
        JsonNode parameters=parameters(input.path("parameters"));
        if(!input.path("proofs").isArray() || !input.path("proofs").isEmpty() || !hash(member.getId(),parameters).equals(input.path("parameters_hash").asText()))throw new Rejected("VALIDATION_ERROR");
        if(!"ADD_CART".equals(receipt.path("action_type").asText()))throw new Rejected("CONFIRMATION_INVALID");
        var row=operation(operation,receipt);
        if(!row.get("state").equals("IN_TX"))return parse(row.get("result_json").toString());
        var quote=first("SELECT * FROM oms_agent_quote WHERE quote_id=?",input.path("quote_id").asText());
        if(quote==null || ((Number)quote.get("member_id")).longValue()!=member.getId() || !input.path("parameters_hash").asText().equals(quote.get("parameters_hash")))return finish(operation,"REJECTED","QUOTE_INVALID",null);
        JsonNode snapshot=parse(quote.get("pricing_snapshot").toString());
        if(Instant.parse(snapshot.path("expires_at").asText()).isBefore(Instant.now()))return finish(operation,"REJECTED","QUOTE_EXPIRED",null);
        guard.lock(member.getId());
        List<Map<String,Object>> current;
        try {
            current=lines(parameters,true);
            if(!json.valueToTree(current).equals(snapshot.path("lines")))return finish(operation,"REJECTED","QUOTE_CHANGED",null);
            for(JsonNode item:parameters.path("items")){
                Number existing=db.queryForObject("SELECT COALESCE(SUM(quantity),0) FROM oms_cart_item WHERE member_id=? AND product_sku_id=? AND delete_status=0",Number.class,member.getId(),id(item.path("sku_id")));
                if(existing==null || existing.longValue()<0 || existing.longValue()+item.path("quantity").asInt()>999)throw new Rejected("CART_QUANTITY_LIMIT");
            }
        }catch(Rejected ex){return finish(operation,"REJECTED",ex.code,null);}
        // Validate every line before the first cart mutation. SQL errors roll back both
        // mutations and operation result; they are never converted into a fake failure.
        for(Map<String,Object> line:current){
            long pid=Long.parseLong(line.get("product_id").toString()),sid=Long.parseLong(line.get("sku_id").toString());
            var product=first("SELECT * FROM pms_product WHERE id=?",pid);var sku=first("SELECT * FROM pms_sku_stock WHERE id=?",sid);
            OmsCartItem cart=new OmsCartItem();cart.setProductId(pid);cart.setProductSkuId(sid);cart.setQuantity((Integer)line.get("quantity"));cart.setPrice(new BigDecimal(line.get("unit_price").toString()));
            cart.setProductName(line.get("name").toString());cart.setProductAttr(line.get("specification").toString());cart.setProductPic(Objects.toString(sku.get("pic"),Objects.toString(product.get("pic"),"")));cart.setProductBrand(Objects.toString(product.get("brand_name"),""));cart.setProductSn(Objects.toString(product.get("product_sn"),""));cart.setProductSkuCode(Objects.toString(sku.get("sku_code"),""));cart.setProductCategoryId(((Number)product.get("product_category_id")).longValue());
            if(carts.add(cart)!=1)throw new IllegalStateException("Cart mutation failed");
        }
        db.update("UPDATE oms_agent_operation SET quote_id=? WHERE operation_no=?",input.path("quote_id").asText(),operation);
        Long quantity=db.queryForObject("SELECT COALESCE(SUM(quantity),0) FROM oms_cart_item WHERE member_id=? AND delete_status=0",Long.class,member.getId());
        if(quantity==null || quantity<0 || quantity>Integer.MAX_VALUE)throw new IllegalStateException("Cart total exceeds supported range");
        return finish(operation,"SUCCEEDED",null,quantity.intValue());
    }
    @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public JsonNode reconcile(String operation,String hash,JsonNode receipt,boolean finalize){
        if(!hash.equals(receipt.path("parameters_hash").asText()))throw new Rejected("CONFIRMATION_INVALID");
        var row=finalize?operation(operation,receipt):first("SELECT * FROM oms_agent_operation WHERE operation_no=?",operation);
        if(row==null)return result(operation,"NOT_COMMITTED",null,null);
        if(((Number)row.get("member_id")).longValue()!=id(receipt.path("member_id")) || !hash.equals(row.get("parameters_hash")))throw new Rejected("IDEMPOTENCY_CONFLICT");
        if(row.get("state").equals("IN_TX"))return finalize?finish(operation,"REJECTED","RECOVERY_NOT_EXECUTED",null):result(operation,"NOT_COMMITTED",null,null);
        return parse(row.get("result_json").toString());
    }
    public JsonNode get(long member,String operation){
        var row=first("SELECT * FROM oms_agent_operation WHERE operation_no=? AND member_id=?",operation,member);
        if(row==null)return result(operation,"NOT_COMMITTED",null,null);
        return parse(row.get("result_json").toString());
    }
    public List<Map<String,Object>> skus(long product){
        List<Map<String,Object>> result=new ArrayList<>();
        for(var row:db.queryForList("SELECT id,price,stock,lock_stock,sp_data FROM pms_sku_stock WHERE product_id=? AND price>=0 ORDER BY id LIMIT 100",product)){
            List<Map<String,Object>> attrs=new ArrayList<>();
            try { JsonNode data=json.readTree(Objects.toString(row.get("sp_data"),"[]"));
                if(data.isArray())for(JsonNode attr:data){String name=attr.path("key").asText(),value=attr.path("value").asText();if(attrs.size()<20 && name.length()<=80 && value.length()<=100)attrs.add(map("name",name,"value",value));}
            } catch(Exception ignored) { }
            int available=row.get("stock")==null?0:Math.max(0,((Number)row.get("stock")).intValue()-(row.get("lock_stock")==null?0:((Number)row.get("lock_stock")).intValue()));
            result.add(map("id",row.get("id").toString(),"attributes",attrs,"price",money((BigDecimal)row.get("price")),"available_quantity",available,"visible",true));
        }
        return result;
    }
}
