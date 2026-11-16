package com.macro.mall.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class AgentStaffBusinessTest {
    private final JdbcTemplate db=mock(JdbcTemplate.class);
    private final ObjectMapper json=new ObjectMapper();
    private final String secret="staff-test-only-not-production-secret";
    private final String cid=UUID.randomUUID().toString();
    private final AgentStaffBusiness service=new AgentStaffBusiness(db,json,secret);
    private Map<String,Object> claims() {return new LinkedHashMap<>(Map.of("kind","STAFF_BUSINESS","staff_id","7","member_id","20","conversation_id",cid,"control_epoch",2,"expires_at",Instant.now().getEpochSecond()+45,"cards",List.of()));}
    private JsonNode input(Map<String,Object> claims) throws Exception {
        String payload=Base64.getUrlEncoder().withoutPadding().encodeToString(json.writeValueAsBytes(claims));
        Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8),"HmacSHA256"));
        String signature=Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(("staff-business.v1."+payload).getBytes(StandardCharsets.UTF_8)));
        return json.valueToTree(Map.of("conversation_id",cid,"access_grant",payload+"."+signature));
    }
    @Test void grantBindsStaffConversationPurposeExpiryAndRejectsRawMemberId() throws Exception {
        assertEquals("20",service.grant(7,input(claims())).path("member_id").asText());
        assertThrows(AgentStaffBusiness.Rejected.class,()->service.grant(8,input(claims())));
        for(var override:List.of(Map.of("kind","OTHER"),Map.of("conversation_id",UUID.randomUUID().toString()),Map.of("expires_at",Instant.now().getEpochSecond()-1),Map.of("expires_at",Instant.now().getEpochSecond()+100),Map.of("member_id","9223372036854775808"))) {
            var changed=claims();changed.putAll(override);
            assertThrows(AgentStaffBusiness.Rejected.class,()->service.grant(7,input(changed)));
        }
        var extra=(com.fasterxml.jackson.databind.node.ObjectNode)input(claims());extra.put("member_id","1");
        assertThrows(AgentStaffBusiness.Rejected.class,()->service.grant(7,extra));
        var tampered=(com.fasterxml.jackson.databind.node.ObjectNode)input(claims());tampered.put("access_grant",tampered.path("access_grant").asText()+"x");
        assertThrows(AgentStaffBusiness.Rejected.class,()->service.grant(7,tampered));
        verifyNoInteractions(db);
    }
    @Test void exactOrderLookupIncludesTrustedMemberAndMasksPhone() throws Exception {
        when(db.queryForList(contains("FROM ums_member"),eq(20L))).thenReturn(List.of(Map.of("nickname","测试会员","phone","13812345678")));
        when(db.queryForList(contains("FROM oms_order WHERE id=? AND member_id=?"),eq(42L),eq(20L))).thenReturn(List.of(Map.of("id",42L,"order_sn","ORDER_TEST","status",0,"pay_amount",new java.math.BigDecimal("12.34"),"create_time",java.sql.Timestamp.from(Instant.now()))));
        when(db.queryForList(contains("FROM oms_order_item"),eq(42L))).thenReturn(List.of(Map.of("product_name","测试商品","product_attr","黑色","product_quantity",2,"product_price",new java.math.BigDecimal("6.17"))));
        var claims=claims();claims.put("cards",List.of(Map.of("kind","ORDER","id","42")));
        var result=service.read(7,input(claims));
        assertEquals("138****5678",result.get("phone_masked"));
        assertFalse(result.toString().contains("13812345678"));
        assertEquals(1,((List<?>)result.get("orders")).size());
        verify(db).queryForList(contains("FROM oms_order WHERE id=? AND member_id=?"),eq(42L),eq(20L));
        when(db.queryForList(contains("FROM oms_order WHERE id=? AND member_id=?"),eq(42L),eq(20L))).thenReturn(List.of());
        assertEquals(404,assertThrows(AgentStaffBusiness.Rejected.class,()->service.read(7,input(claims))).status);
    }
    @Test void duplicateAndOversizedCardSelectionsAreRejectedBeforeSql() throws Exception {
        var claims=claims();claims.put("cards",List.of(Map.of("kind","ORDER","id","42"),Map.of("kind","ORDER","id","42")));
        assertThrows(AgentStaffBusiness.Rejected.class,()->service.read(7,input(claims)));
        claims.put("cards",List.of(Map.of("kind","OTHER","id","42")));
        assertThrows(AgentStaffBusiness.Rejected.class,()->service.read(7,input(claims)));
        verifyNoInteractions(db);
    }
}
