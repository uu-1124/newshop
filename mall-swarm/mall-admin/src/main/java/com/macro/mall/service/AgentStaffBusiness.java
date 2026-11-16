package com.macro.mall.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.*;

/** Read-only, purpose-bound access. No raw member identifier is accepted from a browser. */
@Service
public class AgentStaffBusiness {
    public static class Rejected extends RuntimeException {
        public final int status;
        public Rejected(int status) { super("BUSINESS_CONTEXT_REJECTED"); this.status = status; }
    }
    private final JdbcTemplate db;
    private final ObjectMapper json;
    private final String secret;
    public AgentStaffBusiness(JdbcTemplate db, ObjectMapper json,
            @Value("${MALL_AGENT_STAFF_BRIDGE_SECRET:}") String secret) {
        this.db = db; this.json = json; this.secret = secret;
    }
    public JsonNode grant(long staff, JsonNode input) {
        try {
            if (!input.isObject() || input.size()!=2 || !input.hasNonNull("conversation_id")
                    || !input.hasNonNull("access_grant")) throw new Rejected(422);
            String cid = UUID.fromString(input.path("conversation_id").asText()).toString();
            String[] parts = input.path("access_grant").asText().split("\\.", -1);
            if (secret.isBlank() || parts.length!=2 || parts[0].length()>6000) throw new Rejected(403);
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            if (!MessageDigest.isEqual(mac.doFinal(("staff-business.v1."+parts[0]).getBytes(StandardCharsets.UTF_8)),
                    Base64.getUrlDecoder().decode(parts[1]))) throw new Rejected(403);
            JsonNode claims = json.readTree(Base64.getUrlDecoder().decode(parts[0]));
            long expiry=claims.path("expires_at").asLong(), now=Instant.now().getEpochSecond();
            if (!"STAFF_BUSINESS".equals(claims.path("kind").asText())
                    || !Long.toString(staff).equals(claims.path("staff_id").asText())
                    || !cid.equals(claims.path("conversation_id").asText())
                    || expiry<=now || expiry>now+60 || !claims.path("cards").isArray()
                    || claims.path("cards").size()>5 || id(claims.path("member_id").asText())<=0
                    || !claims.path("control_epoch").canConvertToLong() || claims.path("control_epoch").asLong()<1)
                throw new Rejected(403);
            Set<String> seen = new HashSet<>();
            for(JsonNode card:claims.path("cards")) {
                if (card.size()!=2 || !Set.of("PRODUCT","ORDER").contains(card.path("kind").asText())
                        || id(card.path("id").asText())<=0 || !seen.add(card.path("kind").asText()+card.path("id").asText()))
                    throw new Rejected(422);
            }
            return claims;
        } catch(Rejected e) { throw e; }
        catch(Exception e) { throw new Rejected(403); }
    }
    @Transactional(readOnly=true, isolation=org.springframework.transaction.annotation.Isolation.REPEATABLE_READ)
    public Map<String,Object> read(long staff, JsonNode input) {
        JsonNode claims=grant(staff,input);
        long member=id(claims.path("member_id").asText());
        List<Map<String,Object>> accounts=db.queryForList("SELECT nickname,username,phone FROM ums_member WHERE id=? AND status=1", member);
        if(accounts.isEmpty())throw new Rejected(404);
        var account=accounts.get(0);
        List<Map<String,Object>> orders=new ArrayList<>(), products=new ArrayList<>();
        boolean requestedOrders=false;
        for(JsonNode card:claims.path("cards")) {
            long cardId=id(card.path("id").asText());
            if("PRODUCT".equals(card.path("kind").asText()))products.add(product(cardId));
            else {
                requestedOrders=true;
                var rows=db.queryForList("SELECT id,order_sn,status,pay_amount,create_time FROM oms_order WHERE id=? AND member_id=? AND delete_status=0",cardId,member);
                if(rows.isEmpty())throw new Rejected(404);
                orders.add(order(rows.get(0)));
            }
        }
        if(!requestedOrders) for(var row:db.queryForList("SELECT id,order_sn,status,pay_amount,create_time FROM oms_order WHERE member_id=? AND delete_status=0 ORDER BY create_time DESC,id DESC LIMIT 20",member))orders.add(order(row));
        return map("member_display_name",text(account.get("nickname")!=null?account.get("nickname"):account.get("username"),80),
                "phone_masked",mask(account.get("phone")),"orders",orders,"products",products);
    }
    private Map<String,Object> order(Map<String,Object> row) {
        long oid=((Number)row.get("id")).longValue();
        int status=((Number)row.get("status")).intValue();
        var labels=Map.of(0,"待付款",1,"待发货",2,"已发货",3,"已完成",4,"已关闭",5,"无效订单");
        List<Map<String,Object>> lines=new ArrayList<>();
        for(var line:db.queryForList("SELECT product_name,product_attr,product_quantity,product_price FROM oms_order_item WHERE order_id=? ORDER BY id LIMIT 100",oid))
            lines.add(map("name",text(line.get("product_name"),200),"specification",text(line.get("product_attr"),500),
                    "quantity",line.get("product_quantity"),"unit_price",money(line.get("product_price"))));
        return map("id",Long.toString(oid),"order_sn",text(row.get("order_sn"),64),"status",status,
                "status_label",labels.getOrDefault(status,"未知状态"),"pay_amount",money(row.get("pay_amount")),
                "created_at",instant(row.get("create_time")),"lines",lines);
    }
    private Map<String,Object> product(long pid) {
        var rows=db.queryForList("SELECT p.id,p.name,p.brand_name,p.product_category_id,p.pic,COALESCE((SELECT MIN(s.price) FROM pms_sku_stock s WHERE s.product_id=p.id AND s.price>=0),p.price) catalog_price FROM pms_product p WHERE p.id=? AND p.delete_status=0 AND p.publish_status=1 AND p.product_category_id IS NOT NULL",pid);
        if(rows.isEmpty() || rows.get(0).get("catalog_price")==null)throw new Rejected(404);
        var p=rows.get(0);
        List<Map<String,Object>> skus=new ArrayList<>(), facts=new ArrayList<>();
        for(var s:db.queryForList("SELECT id,price,stock,lock_stock,sp_data FROM pms_sku_stock WHERE product_id=? AND price>=0 AND stock IS NOT NULL ORDER BY id LIMIT 100",pid)) {
            List<Map<String,Object>> attributes=new ArrayList<>();
            try {
                JsonNode data=json.readTree(Objects.toString(s.get("sp_data"),"[]"));
                if(data.isArray())for(JsonNode a:data)if(attributes.size()<20 && a.path("key").isTextual() && a.path("value").isTextual())
                    attributes.add(map("name",text(a.path("key").asText(),80),"value",text(a.path("value").asText(),100)));
            } catch(Exception ignored) { }
            long available=Math.max(0L,((Number)s.get("stock")).longValue()-((Number)(s.get("lock_stock")==null?0:s.get("lock_stock"))).longValue());
            skus.add(map("id",s.get("id").toString(),"attributes",attributes,"visible",true,
                    "price",money(s.get("price")),"available_quantity",Math.min(Integer.MAX_VALUE,available)));
        }
        for(var f:db.queryForList("SELECT a.name,v.value FROM pms_product_attribute_value v JOIN pms_product_attribute a ON a.id=v.product_attribute_id WHERE v.product_id=? AND a.name IS NOT NULL AND v.value IS NOT NULL ORDER BY a.sort,a.id,v.id LIMIT 40",pid))
            facts.add(map("name",text(f.get("name"),80),"value",text(f.get("value"),500)));
        String image=text(p.get("pic"),2048);
        if(!image.matches("https?://[^\\s]+"))image="";
        return map("id",Long.toString(pid),"name",text(p.get("name"),200),"brand",text(p.get("brand_name"),100),
                "category_id",p.get("product_category_id").toString(),"price_from",money(p.get("catalog_price")),
                "image_url",image,"detail_path","/pages/product/product?id="+pid,"sku_options",skus,"facts",facts,"checked_at",Instant.now().toString());
    }
    private static long id(String value) {if(!value.matches("[1-9][0-9]{0,18}"))throw new Rejected(422);try{return Long.parseLong(value);}catch(NumberFormatException e){throw new Rejected(422);}}
    private static String text(Object value,int max) {String s=Objects.toString(value,"");return s.substring(0,Math.min(max,s.length()));}
    private static String money(Object value) {return value==null?null:new BigDecimal(value.toString()).setScale(2,RoundingMode.HALF_UP).toPlainString();}
    private static String mask(Object value) {String s=Objects.toString(value,"");return s.length()<7?"未提供":s.substring(0,3)+"****"+s.substring(s.length()-4);}
    private static String instant(Object value) {if(value instanceof java.sql.Timestamp t)return t.toInstant().toString();if(value instanceof java.time.LocalDateTime t)return t.toInstant(java.time.ZoneOffset.UTC).toString();return Objects.toString(value,"");}
    private static Map<String,Object> map(Object... items) {Map<String,Object> value=new LinkedHashMap<>();for(int i=0;i<items.length;i+=2)value.put(items[i].toString(),items[i+1]);return value;}
}
