package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.common.exception.ApiException;
import com.macro.mall.model.*;
import com.macro.mall.portal.domain.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.time.*;
import java.util.*;
import static com.macro.mall.portal.service.AgentCartService.*;

/** An adapter to the original Java order service. No independent pricing or order writer. */
@Service
public class AgentOrderService {
    private static final String RULE_VERSION = "ORDER_CHECKOUT_CENTS_V2";
    private final JdbcTemplate db;
    private final ObjectMapper json;
    private final AgentCartService operations;
    private final CartGuard guard;
    private final OmsPortalOrderService orders;
    private final UmsMemberService members;
    public AgentOrderService(JdbcTemplate db,ObjectMapper json,AgentCartService operations,CartGuard guard,
                             OmsPortalOrderService orders,UmsMemberService members) {
        this.db=db;this.json=json.copy().setSerializationInclusion(com.fasterxml.jackson.annotation.JsonInclude.Include.ALWAYS);this.operations=operations;this.guard=guard;this.orders=orders;this.members=members;
    }
    public List<Long> selection(JsonNode input) {
        if(!"CREATE_ORDER".equals(input.path("action_type").asText()) || !Set.of("CART","DIRECT").contains(input.path("source").asText()))
            throw new Rejected("FEATURE_NOT_ACTIVE");
        if ("DIRECT".equals(input.path("source").asText())) {
            operations.parameters(json.valueToTree(map("action_type","ADD_CART","items",input.path("items"))));
            return List.of();
        }
        JsonNode ids=input.path("cart_item_ids");
        if(!ids.isArray() || ids.isEmpty() || ids.size()>20)throw new Rejected("VALIDATION_ERROR");
        List<Long> selected=new ArrayList<>();for(JsonNode value:ids){long n=id(value);if(selected.contains(n))throw new Rejected("VALIDATION_ERROR");selected.add(n);}
        return selected;
    }
    public JsonNode parameters(JsonNode input) {
        fields(input,"action_type","source","DIRECT".equals(input.path("source").asText())?"items":"cart_item_ids","address_id","coupon_id","use_points");
        selection(input);id(input.path("address_id"));if(!input.path("coupon_id").isNull())id(input.path("coupon_id"));
        if(!input.path("use_points").isIntegralNumber() || !input.path("use_points").canConvertToInt()
            || input.path("use_points").asInt()<0 || input.path("use_points").asInt()>100000000)throw new Rejected("VALIDATION_ERROR");
        return input.deepCopy();
    }
    private OrderParam orderParam(JsonNode input) {
        OrderParam out=new OrderParam();out.setCartIds(selection(input));
        if ("DIRECT".equals(input.path("source").asText())) out.setDirectItems(directItems(input));
        out.setMemberReceiveAddressId(id(input.path("address_id")));
        out.setCouponId(input.path("coupon_id").isNull()?null:id(input.path("coupon_id")));out.setUseIntegration(input.path("use_points").asInt());
        out.setPayType(0);return out;
    }
    public JsonNode options(JsonNode input) {
        fields(input,"action_type","source","DIRECT".equals(input.path("source").asText())?"items":"cart_item_ids");
        selection(input);
        ConfirmOrderResult result;
        try {result="DIRECT".equals(input.path("source").asText())?orders.generateDirectConfirmOrder(directItems(input)):orders.generateConfirmOrder(selection(input));}
        catch(ApiException ex){throw new Rejected("CHECKOUT_SELECTION_INVALID");}
        var addresses=result.getMemberReceiveAddressList().stream().map(a->map("id",a.getId().toString(),
            "label",OrderCheckout.addressLabel(a),"is_default",Integer.valueOf(1).equals(a.getDefaultStatus()))).toList();
        var coupons=result.getCouponHistoryDetailList().stream().map(c->map("id",c.getCoupon().getId().toString(),
            "name",c.getCoupon().getName(),"amount",OrderCheckout.money(c.getCoupon().getAmount()),"available",true,"unavailable_reason",null)).toList();
        var rule=result.getIntegrationConsumeSetting();
        String description=rule==null?"积分规则暂不可用，请选择不使用积分":String.format("每 %d 积分抵扣 1 元，最多抵扣商品金额的 %d%%；%s优惠券同用。实际抵扣以确认卡为准。",
            rule.getUseUnit(),rule.getMaxPercentPerOrder(),Integer.valueOf(1).equals(rule.getCouponStatus())?"可与":"不可与");
        return json.valueToTree(map("addresses",addresses,"coupons",coupons,"available_points",Objects.requireNonNullElse(result.getMemberIntegration(),0),
            "points_rule_description",description,"manage_address_path","/pages/address/address"));
    }
    private List<OmsCartItem> directItems(JsonNode input) {
        List<OmsCartItem> selected = new ArrayList<>();
        for (JsonNode line : input.path("items")) {
            long productId=id(line.path("product_id")), skuId=id(line.path("sku_id"));
            var products=db.query("SELECT * FROM pms_product WHERE id=? AND delete_status=0 AND publish_status=1",
                new org.springframework.jdbc.core.BeanPropertyRowMapper<>(PmsProduct.class),productId);
            var skus=db.query("SELECT * FROM pms_sku_stock WHERE id=? AND product_id=?",
                new org.springframework.jdbc.core.BeanPropertyRowMapper<>(PmsSkuStock.class),skuId,productId);
            if(products.size()!=1 || skus.size()!=1)throw new Rejected("PRODUCT_UNAVAILABLE");
            var p=products.get(0);var s=skus.get(0);int quantity=line.path("quantity").asInt();
            if(s.getPrice()==null || s.getPrice().signum()<0 || s.getStock()==null
                || s.getStock()-Objects.requireNonNullElse(s.getLockStock(),0)<quantity)throw new Rejected("STOCK_INSUFFICIENT");
            OmsCartItem item=new OmsCartItem();
            item.setId(skuId);item.setMemberId(members.getCurrentMember().getId());
            item.setProductId(productId);item.setProductSkuId(skuId);item.setQuantity(quantity);item.setPrice(s.getPrice());
            item.setProductName(p.getName());item.setProductPic(p.getPic());item.setProductBrand(p.getBrandName());
            item.setProductSn(p.getProductSn());item.setProductCategoryId(p.getProductCategoryId());
            item.setProductSkuCode(s.getSkuCode());item.setProductAttr(s.getSpData());item.setDeleteStatus(0);
            selected.add(item);
        }
        return selected;
    }
    private Map<String,Object> first(String sql,Object... args){var rows=db.queryForList(sql,args);return rows.isEmpty()?null:rows.get(0);}
    private JsonNode parse(Object data){try{return json.readTree(data.toString());}catch(Exception ex){throw new IllegalStateException("Order operation projection invalid",ex);}}
    private String encode(Object data){try{return json.writeValueAsString(data);}catch(Exception ex){throw new IllegalStateException("Order projection unavailable",ex);}}
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public JsonNode quote(UmsMember member,UUID quoteId,JsonNode input) {
        JsonNode params=parameters(input);String hash=operations.hash(member.getId(),params);
        var old=first("SELECT * FROM oms_agent_quote WHERE quote_id=?",quoteId.toString());
        if(old!=null){if(!hash.equals(old.get("parameters_hash")) || ((Number)old.get("member_id")).longValue()!=member.getId())throw new Rejected("IDEMPOTENCY_CONFLICT");return parse(old.get("pricing_snapshot"));}
        guard.lock(member.getId());OrderCheckout checkout;
        try {checkout=orders.previewOrder(orderParam(params));}catch(ApiException ex){throw new Rejected("CHECKOUT_SELECTION_INVALID");}
        Instant expiry=Instant.now().plusSeconds(300);
        var quote=map("quote_id",quoteId.toString(),"parameters",params,"parameters_hash",hash,"lines",checkout.lines(),
            "pricing",checkout.pricing(),"address_summary",OrderCheckout.addressLabel(checkout.address()),
            "rule_version",RULE_VERSION,"expires_at",expiry.toString());
        db.update("INSERT INTO oms_agent_quote(quote_id,member_id,action_type,parameters_hash,parameters_json,pricing_snapshot,total_amount,rule_version,expires_at,checkout_fingerprint) VALUES(?,?,'CREATE_ORDER',?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE quote_id=VALUES(quote_id)",
            quoteId.toString(),member.getId(),hash,operations.canonical(params),encode(quote),checkout.order().getPayAmount(),RULE_VERSION,LocalDateTime.ofInstant(expiry,ZoneOffset.UTC),checkout.fingerprint());
        var stored=first("SELECT * FROM oms_agent_quote WHERE quote_id=? FOR UPDATE",quoteId.toString());
        if(!hash.equals(stored.get("parameters_hash")) || ((Number)stored.get("member_id")).longValue()!=member.getId())throw new Rejected("IDEMPOTENCY_CONFLICT");
        return parse(stored.get("pricing_snapshot"));
    }
    @Transactional(isolation=org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public JsonNode execute(UmsMember member,JsonNode input,JsonNode receipt) {
        fields(input,"operation_no","quote_id","parameters_hash","parameters","confirmation_receipt","proofs");
        String operation=UUID.fromString(input.path("operation_no").asText()).toString();
        JsonNode params=parameters(input.path("parameters"));
        if(!"CREATE_ORDER".equals(receipt.path("action_type").asText()) || !input.path("quote_id").asText().equals(receipt.path("quote_id").asText()))throw new Rejected("CONFIRMATION_INVALID");
        if(!input.path("proofs").isArray() || !input.path("proofs").isEmpty() || !operations.hash(member.getId(),params).equals(input.path("parameters_hash").asText()))throw new Rejected("VALIDATION_ERROR");
        var operationRow=operations.operation(operation,receipt);
        if(!"IN_TX".equals(operationRow.get("state")))return parse(operationRow.get("result_json"));
        var quote=first("SELECT * FROM oms_agent_quote WHERE quote_id=?",input.path("quote_id").asText());
        if(quote==null || ((Number)quote.get("member_id")).longValue()!=member.getId() || !"CREATE_ORDER".equals(quote.get("action_type")) || !input.path("parameters_hash").asText().equals(quote.get("parameters_hash")))return finish(operation,"QUOTE_INVALID",null,null);
        JsonNode snapshot=parse(quote.get("pricing_snapshot"));
        if(!Instant.parse(snapshot.path("expires_at").asText()).isAfter(Instant.now()))return finish(operation,"QUOTE_EXPIRED",null,null);
        guard.lock(member.getId());
        // Read-only validation errors can be stored as rejections. Exceptions after calling
        // the transactional order writer must escape and roll back the entire operation.
        try {if(!orders.previewOrder(orderParam(params)).fingerprint().equals(quote.get("checkout_fingerprint")))return finish(operation,"QUOTE_CHANGED",null,null);}
        catch(ApiException ex){return finish(operation,"CHECKOUT_CHANGED",null,null);}
        var created=orders.generateConfirmedOrder(orderParam(params),quote.get("checkout_fingerprint").toString());
        if(Boolean.TRUE.equals(created.get("quote_changed")))return finish(operation,"QUOTE_CHANGED",null,null);
        OmsOrder order=(OmsOrder)created.get("order");
        List<OmsOrderItem> items=db.query("SELECT * FROM oms_order_item WHERE order_id=? ORDER BY id",new org.springframework.jdbc.core.BeanPropertyRowMapper<>(OmsOrderItem.class),order.getId());
        db.update("UPDATE oms_agent_operation SET quote_id=? WHERE operation_no=?",input.path("quote_id").asText(),operation);
        return finish(operation,null,order,items);
    }
    private JsonNode finish(String operation,String error,OmsOrder order,List<OmsOrderItem> items) {
        boolean success=order!=null;String state=success?"SUCCEEDED":"REJECTED";Instant now=Instant.now();
        Object summary=null;
        if(success){
            var lines=items.stream().map(i->map("order_item_id",i.getId().toString(),"product_id",i.getProductId().toString(),"sku_id",i.getProductSkuId().toString(),
                "name",Objects.toString(i.getProductName(),""),"specification",Objects.toString(i.getProductAttr(),""),"quantity",i.getProductQuantity(),
                "unit_price",OrderCheckout.money(i.getProductPrice()),"paid_line_amount",OrderCheckout.money(i.getRealAmount().multiply(java.math.BigDecimal.valueOf(i.getProductQuantity()))),"returnable_quantity",0)).toList();
            var address=new UmsMemberReceiveAddress();address.setName(order.getReceiverName());address.setPhoneNumber(order.getReceiverPhone());address.setProvince(order.getReceiverProvince());address.setCity(order.getReceiverCity());address.setRegion(order.getReceiverRegion());address.setDetailAddress(order.getReceiverDetailAddress());
            summary=map("id",order.getId().toString(),"order_sn",order.getOrderSn(),"status",0,"status_label","待付款","pay_amount",OrderCheckout.money(order.getPayAmount()),
                "created_at",order.getCreateTime().toInstant().toString(),"receive_time",null,"address_summary",OrderCheckout.addressLabel(address),"lines",lines,"returns",List.of(),
                "detail_path","/pages/order/orderDetail?orderId="+order.getId(),"payment_path","/pages/money/pay?orderId="+order.getId(),"is_simulated",true);
        }
        var result=map("operation_no",operation,"state",state,"business_type",success?"ORDER":null,"business_id",success?order.getId().toString():null,"error_code",error,
            "message",success?"订单已创建，等待买家付款":"订单未创建，请重新准备确认","order",summary,"return_apply",null,"cart_quantity",null,"completed_at",now.toString());
        db.update("UPDATE oms_agent_operation SET state=?,result_json=?,error_code=?,business_type=?,business_id=?,completed_at=? WHERE operation_no=?",
            state,encode(result),error,success?"ORDER":null,success?order.getId():null,LocalDateTime.ofInstant(now,ZoneOffset.UTC),operation);
        return json.valueToTree(result);
    }
}
