package com.macro.mall.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.transaction.support.TransactionTemplate;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

@EnabledIfEnvironmentVariable(named="MALL_CART_TEST_JDBC", matches="jdbc:mysql:.*")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AgentStaffBusinessDatabaseTest {
    private JdbcTemplate db; private TransactionTemplate tx; private AgentStaffBusiness service;
    private final ObjectMapper json=new ObjectMapper(); private final String secret="staff-isolated-business-secret";
    private static final long MEMBER=892000001L, ORDER=892000001L;
    @BeforeAll void setup() {
        String url=System.getenv("MALL_CART_TEST_JDBC");
        if(!url.matches("jdbc:mysql://127\\.0\\.0\\.1:13306/mall_agent_cart_test\\?.*"))throw new IllegalStateException("Isolated test DB required");
        var ds=new DriverManagerDataSource(url,System.getenv("MALL_CART_TEST_USER"),System.getenv("MALL_CART_TEST_PASSWORD"));
        db=new JdbcTemplate(ds);tx=new TransactionTemplate(new DataSourceTransactionManager(ds));
        service=new AgentStaffBusiness(db,json,secret);
    }
    private com.fasterxml.jackson.databind.JsonNode input(String kind,long id) {
        try {
            String cid=UUID.randomUUID().toString();
            var claims=Map.of("kind","STAFF_BUSINESS","staff_id","7","member_id",Long.toString(MEMBER),"conversation_id",cid,"control_epoch",1,"expires_at",Instant.now().getEpochSecond()+45,"cards",kind.isEmpty()?List.of():List.of(Map.of("kind",kind,"id",Long.toString(id))));
            String payload=Base64.getUrlEncoder().withoutPadding().encodeToString(json.writeValueAsBytes(claims));
            Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8),"HmacSHA256"));
            String sig=Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(("staff-business.v1."+payload).getBytes(StandardCharsets.UTF_8)));
            return json.valueToTree(Map.of("conversation_id",cid,"access_grant",payload+"."+sig));
        } catch(Exception e) {throw new RuntimeException(e);}
    }
    private void fixtures() {
        db.update("INSERT INTO ums_member(id,username,nickname,phone,status) VALUES(?,'STAFF_BUSINESS_ONLY','业务测试','13812345678',1)",MEMBER);
        db.update("INSERT INTO oms_order(id,member_id,order_sn,status,delete_status,pay_amount,create_time,receiver_name,receiver_phone) VALUES(?,?,'STAFF_BUSINESS_OWNED',1,0,12.34,now(),'测试收货人','13800000000')",ORDER,MEMBER);
        db.update("INSERT INTO oms_order(id,member_id,order_sn,status,delete_status,pay_amount,create_time,receiver_name,receiver_phone) VALUES(?,?,'STAFF_BUSINESS_OTHER',1,0,99.00,now(),'测试收货人','13800000000')",ORDER+1,MEMBER+1);
        db.update("INSERT INTO oms_order_item(id,order_id,product_name,product_attr,product_quantity,product_price) VALUES(?,?,'隔离商品','黑色',2,6.17)",ORDER,ORDER);
    }
    @Test void onlyTrustedMembersOrdersAndMaskedIdentityAreReturned() {
        tx.execute(status->{fixtures();
            var result=service.read(7,input("ORDER",ORDER));
            assertEquals("138****5678",result.get("phone_masked"));
            assertTrue(result.toString().contains("STAFF_BUSINESS_OWNED"));
            assertFalse(result.toString().contains("13812345678"));
            assertEquals(404,assertThrows(AgentStaffBusiness.Rejected.class,()->service.read(7,input("ORDER",ORDER+1))).status);
            db.update("UPDATE oms_order SET delete_status=1 WHERE id=?",ORDER);
            assertEquals(404,assertThrows(AgentStaffBusiness.Rejected.class,()->service.read(7,input("ORDER",ORDER))).status);
            status.setRollbackOnly();return null;});
    }
    @Test void currentProductVisibilityAndSkuFactsAreReadFromMysql() {
        tx.execute(status->{fixtures();
            long pid=((Number)db.queryForMap("SELECT id FROM pms_product WHERE publish_status=1 AND delete_status=0 ORDER BY id LIMIT 1").get("id")).longValue();
            var result=service.read(7,input("PRODUCT",pid));
            assertEquals(1,((List<?>)result.get("products")).size());
            var product=(Map<?,?>)((List<?>)result.get("products")).get(0);
            assertNotNull(product.get("sku_options"));assertNotNull(product.get("facts"));
            db.update("UPDATE pms_product SET publish_status=0 WHERE id=?",pid);
            assertEquals(404,assertThrows(AgentStaffBusiness.Rejected.class,()->service.read(7,input("PRODUCT",pid))).status);
            status.setRollbackOnly();return null;});
    }
}
