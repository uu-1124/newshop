package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.*;
import com.macro.mall.common.service.RedisService;
import com.macro.mall.mapper.*;
import com.macro.mall.model.*;
import com.macro.mall.order.OrderResourceService;
import com.macro.mall.order.dao.OrderResourceDao;
import com.macro.mall.portal.component.CancelOrderSender;
import com.macro.mall.portal.dao.*;
import com.macro.mall.portal.service.impl.*;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.context.annotation.*;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.*;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.support.TransactionTemplate;
import javax.sql.DataSource;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@EnabledIfEnvironmentVariable(named="MALL_CART_TEST_JDBC",matches="jdbc:mysql:.*")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AgentOrderDatabaseTest {
    static final long MEMBER=880000002;
    private AnnotationConfigApplicationContext ctx;private JdbcTemplate db;private ObjectMapper json;
    private AgentOrderService service;private AgentCartService ops;private OmsPortalOrderService orders;
    private long sku,product;
    @Configuration @EnableTransactionManagement
    @MapperScan({"com.macro.mall.mapper","com.macro.mall.portal.dao","com.macro.mall.order.dao"})
    static class Config {
        @Bean DataSource dataSource(){String url=System.getenv("MALL_CART_TEST_JDBC");if(!url.matches("jdbc:mysql://127\\.0\\.0\\.1:13306/mall_agent_cart_test\\?.*"))throw new IllegalStateException("Isolated checkout DB required");return new DriverManagerDataSource(url,System.getenv("MALL_CART_TEST_USER"),System.getenv("MALL_CART_TEST_PASSWORD"));}
        @Bean SqlSessionFactory sqlSessionFactory(DataSource ds)throws Exception{var bean=new SqlSessionFactoryBean();bean.setDataSource(ds);var resolver=new PathMatchingResourcePatternResolver();var resources=new ArrayList<org.springframework.core.io.Resource>();resources.addAll(List.of(resolver.getResources("classpath*:com/macro/mall/mapper/*.xml")));resources.addAll(List.of(resolver.getResources("classpath*:dao/*.xml")));bean.setMapperLocations(resources.toArray(org.springframework.core.io.Resource[]::new));return bean.getObject();}
        @Bean DataSourceTransactionManager transactionManager(DataSource ds){return new DataSourceTransactionManager(ds);}
        @Bean JdbcTemplate jdbc(DataSource ds){return new JdbcTemplate(ds);}
        @Bean ObjectMapper json(){return new ObjectMapper();}
        @Bean CartGuard guard(CartGuardDao dao){return new CartGuard(dao);}
        @Bean UmsMemberService members(UmsMemberMapper mapper){var member=mock(UmsMemberService.class);when(member.getCurrentMember()).thenAnswer(inv->mapper.selectByPrimaryKey(MEMBER));return member;}
        @Bean OmsCartItemService carts(){return new OmsCartItemServiceImpl();}
        @Bean OmsPromotionService promotion(){return new OmsPromotionServiceImpl();}
        @Bean UmsMemberReceiveAddressService addresses(){return new UmsMemberReceiveAddressServiceImpl();}
        @Bean UmsMemberCouponService coupons(){return new UmsMemberCouponServiceImpl();}
        @Bean OmsPortalOrderService orders(){return new OmsPortalOrderServiceImpl();}
        @Bean RedisService redis(){var redis=mock(RedisService.class);var sequence=new java.util.concurrent.atomic.AtomicLong();when(redis.incr(anyString(),eq(1L))).thenAnswer(inv->sequence.incrementAndGet());return redis;}
        @Bean org.springframework.amqp.core.AmqpTemplate amqp(){return mock(org.springframework.amqp.core.AmqpTemplate.class);}
        @Bean CancelOrderSender sender(){return new CancelOrderSender();}
        @Bean OrderBusinessOutbox businessOutbox(JdbcTemplate db,org.springframework.transaction.PlatformTransactionManager tx,OrderResourceService resources){return new OrderBusinessOutbox(db,tx,resources);}
        @Bean OrderResourceService resources(OmsOrderMapper orders,OmsOrderItemMapper items,OrderResourceDao dao,RedisService redis){return new OrderResourceService(orders,items,dao,redis,"checkout-test","member");}
        @Bean AgentCartService operations(JdbcTemplate db,ObjectMapper json,CartGuard guard,OmsCartItemService carts){return new AgentCartService(db,json,guard,carts);}
        @Bean AgentOrderService agent(JdbcTemplate db,ObjectMapper json,AgentCartService ops,CartGuard guard,OmsPortalOrderService orders,UmsMemberService members){return new AgentOrderService(db,json,ops,guard,orders,members);}
        @Bean com.macro.mall.order.AgentAfterSaleStore afterSale(JdbcTemplate db,ObjectMapper json){return new com.macro.mall.order.AgentAfterSaleStore(db,json);}
        @Bean AgentCartSecurity security(ObjectMapper json){return new AgentCartSecurity("b".repeat(40),"c".repeat(40),"r".repeat(40),mock(org.springframework.data.redis.core.StringRedisTemplate.class),json);}
        @Bean AgentOrderLifecycle lifecycle(JdbcTemplate db,ObjectMapper json,AgentCartService ops,com.macro.mall.order.AgentAfterSaleStore store,OrderResourceService resources,AgentCartSecurity security){return new AgentOrderLifecycle(db,json,ops,store,resources,security);}
    }
    @BeforeAll void start(){ctx=new AnnotationConfigApplicationContext(Config.class);db=ctx.getBean(JdbcTemplate.class);json=ctx.getBean(ObjectMapper.class);service=ctx.getBean(AgentOrderService.class);ops=ctx.getBean(AgentCartService.class);orders=ctx.getBean(OmsPortalOrderService.class);var row=db.queryForMap("SELECT id,product_id FROM pms_sku_stock ORDER BY id LIMIT 1");sku=((Number)row.get("id")).longValue();product=((Number)row.get("product_id")).longValue();}
    @AfterAll void stop(){if(ctx!=null)ctx.close();}
    @BeforeEach void reset(){
        db.update("DELETE FROM oms_after_sale_rule_version");
        db.update("INSERT INTO oms_after_sale_rule_version(rule_code,version_no,rules_json,effective_at,created_by) VALUES('ITEM_NET_DEMO',1,?, '2000-01-01',0)","{\"return_window_hours\":168,\"allow_shipped\":true,\"amount_basis\":\"PAID_ITEM_NET\",\"is_simulated\":true}");
        db.update("DELETE FROM oms_agent_business_outbox");
        for(String table:List.of("oms_agent_business_audit","oms_agent_staff_command","oms_agent_shipment_event","oms_agent_shipment","oms_agent_return","oms_order_return_apply","oms_order_return_reason","oms_company_address"))db.update("DELETE FROM "+table);
        for(String table:List.of("oms_agent_operation","oms_agent_quote","oms_order_item","oms_order","oms_cart_item","sms_coupon_history","sms_coupon","ums_member_receive_address","ums_member","oms_order_setting","ums_integration_consume_setting"))db.update("DELETE FROM "+table);
        db.update("INSERT INTO ums_member(id,username,nickname,status,integration) VALUES(?, 'checkout-test','checkout-test',1,5000)",MEMBER);
        db.update("INSERT INTO ums_member_receive_address(id,member_id,name,phone_number,default_status,province,city,region,detail_address) VALUES(1,?,'测试收货人','13800000000',1,'测试省','测试市','测试区','测试地址')",MEMBER);
        db.update("UPDATE pms_product SET promotion_type=0,gift_point=0,gift_growth=0,publish_status=1,delete_status=0 WHERE id=?",product);
        db.update("UPDATE pms_sku_stock SET price=100,stock=100,lock_stock=0 WHERE id=?",sku);
        db.update("INSERT INTO oms_cart_item(id,product_id,product_sku_id,member_id,quantity,price,delete_status,product_name,product_attr) VALUES(1,?,?,?,2,100,0,'结算测试商品','默认规格')",product,sku,MEMBER);
        db.update("INSERT INTO oms_order_setting(id,normal_order_overtime,confirm_overtime) VALUES(1,30,7)");
        db.update("INSERT INTO ums_integration_consume_setting(id,use_unit,max_percent_per_order,coupon_status) VALUES(1,100,50,1)");
        db.update("INSERT INTO sms_coupon(id,name,amount,min_point,use_type,start_time,end_time) VALUES(1,'测试优惠券',10,0,0,NOW()-INTERVAL 1 DAY,NOW()+INTERVAL 1 DAY)");
        db.update("INSERT INTO sms_coupon_history(id,coupon_id,member_id,use_status) VALUES(1,1,?,0)",MEMBER);
        db.update("INSERT INTO oms_order_return_reason(id,name,sort,status) VALUES(1,'演示退货原因',1,1)");
        db.update("INSERT INTO oms_company_address(id,address_name,name,phone,province,city,region,detail_address) VALUES(1,'演示退货点','演示收货员','13800000000','省','市','区','演示地址')");
    }
    JsonNode params(){return json.valueToTree(Map.of("action_type","CREATE_ORDER","source","CART","cart_item_ids",List.of("1"),"address_id","1","coupon_id","1","use_points",100));}

    @Test void cartQuoteAndOrderUseCurrentMetadataWithoutRewritingSavedCart() {
        String specification="[{\"key\":\"颜色\",\"value\":\"可信蓝色\"}]";
        db.update("UPDATE pms_product SET name='当前可信商品',product_sn='CURRENT-PRODUCT' WHERE id=?",product);
        db.update("UPDATE pms_sku_stock SET sp_data=?,sku_code='CURRENT-SKU' WHERE id=?",specification,sku);
        db.update("UPDATE oms_cart_item SET product_name=NULL,product_attr='伪造规格',product_sku_code='FAKE' WHERE id=1");
        var q=quote();
        assertEquals("当前可信商品",q.path("lines").get(0).path("name").asText());
        assertEquals(specification,q.path("lines").get(0).path("specification").asText());
        assertNull(db.queryForObject("SELECT product_name FROM oms_cart_item WHERE id=1",String.class));
        assertEquals("伪造规格",db.queryForObject("SELECT product_attr FROM oms_cart_item WHERE id=1",String.class));
        String op=UUID.randomUUID().toString();
        var executed=service.execute(member(),input(op,q),receipt(op,q));
        assertEquals("SUCCEEDED",executed.path("state").asText());
        long order=executed.path("business_id").asLong();
        assertEquals("当前可信商品",db.queryForObject("SELECT product_name FROM oms_order_item WHERE order_id=? LIMIT 1",String.class,order));
        assertEquals(specification,db.queryForObject("SELECT product_attr FROM oms_order_item WHERE order_id=? LIMIT 1",String.class,order));
        assertEquals("CURRENT-SKU",db.queryForObject("SELECT product_sku_code FROM oms_order_item WHERE order_id=? LIMIT 1",String.class,order));
    }
    UmsMember member(){return ctx.getBean(UmsMemberService.class).getCurrentMember();}
    JsonNode quote(){return service.quote(member(),UUID.randomUUID(),params());}
    JsonNode receipt(String op,JsonNode q){return json.valueToTree(Map.of("operation_no",op,"action_id",op,"member_id",Long.toString(MEMBER),"action_type","CREATE_ORDER","parameters_hash",q.path("parameters_hash").asText(),"quote_id",q.path("quote_id").asText(),"control_epoch",1,"confirmed_at",Instant.now().toString(),"confirmation_audit_id",UUID.randomUUID().toString()));}
    JsonNode input(String op,JsonNode q){return json.valueToTree(Map.of("operation_no",op,"quote_id",q.path("quote_id").asText(),"parameters_hash",q.path("parameters_hash").asText(),"parameters",q.path("parameters"),"confirmation_receipt","test","proofs",List.of()));}
    int count(String table){return db.queryForObject("SELECT COUNT(*) FROM "+table,Integer.class);}
    JsonNode direct(){var p=(com.fasterxml.jackson.databind.node.ObjectNode)params().deepCopy();p.remove("cart_item_ids");p.put("source","DIRECT");p.set("items",json.valueToTree(List.of(Map.of("product_id",Long.toString(product),"sku_id",Long.toString(sku),"quantity",2))));return p;}
    JsonNode lifecycleReceipt(String op,JsonNode q){return ((com.fasterxml.jackson.databind.node.ObjectNode)receipt(op,q)).put("action_type",q.path("parameters").path("action_type").asText());}
    @Test void directUsesOriginalPricingAndNeverMutatesCart(){
        var p=direct();var selection=p.deepCopy();((com.fasterxml.jackson.databind.node.ObjectNode)selection).remove(List.of("address_id","coupon_id","use_points"));
        assertFalse(service.options(selection).path("addresses").isEmpty());
        var q=service.quote(member(),UUID.randomUUID(),p);assertEquals("189.00",q.path("pricing").path("payable").asText());
        String op=UUID.randomUUID().toString();var result=service.execute(member(),input(op,q),receipt(op,q));assertEquals("SUCCEEDED",result.path("state").asText());
        assertEquals(1,db.queryForObject("SELECT COUNT(*) FROM oms_cart_item WHERE delete_status=0",Integer.class));
        assertEquals(2,db.queryForObject("SELECT quantity FROM oms_cart_item WHERE id=1",Integer.class));
        assertEquals(1,count("oms_order"));assertEquals(2,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));
    }
    @Test void confirmedCancellationReleasesResourcesOnlyOnceAndRejectsForeignOrder(){
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        var lifecycle=ctx.getBean(AgentOrderLifecycle.class);var p=json.valueToTree(Map.of("action_type","CANCEL_ORDER","order_id",Long.toString(oid)));
        var cancel=lifecycle.quote(member(),UUID.randomUUID(),p);String cop=UUID.randomUUID().toString();var claims=lifecycleReceipt(cop,cancel);
        assertEquals("CANCEL_PENDING_V2",cancel.path("rule_version").asText());
        assertEquals(Long.toString(oid),cancel.path("cancellation").path("id").asText());
        assertEquals(db.queryForObject("SELECT order_sn FROM oms_order WHERE id=?",String.class,oid),cancel.path("cancellation").path("order_sn").asText());
        assertEquals("189.00",cancel.path("cancellation").path("pay_amount").asText());
        assertEquals("189.00",cancel.path("pricing").path("payable").asText());assertEquals(0,cancel.path("cancellation").path("status").asInt());
        assertEquals("SUCCEEDED",lifecycle.execute(member(),input(cop,cancel),claims).path("state").asText());
        assertEquals("SUCCEEDED",lifecycle.execute(member(),input(cop,cancel),claims).path("state").asText());
        assertEquals(0,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));assertEquals(5000,member().getIntegration());
        var stranger=new UmsMember();stranger.setId(MEMBER+1);assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->lifecycle.quote(stranger,UUID.randomUUID(),p));
    }
    long completedOrder(){var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();orders.paySuccess(oid,1);db.update("UPDATE oms_order SET status=3,receive_time=NOW() WHERE id=?",oid);return oid;}
    JsonNode returnParams(long oid,long iid,int quantity){return json.valueToTree(Map.of("action_type","CREATE_RETURN","order_id",Long.toString(oid),"order_item_id",Long.toString(iid),"quantity",quantity,"reason_code","1","description","演示说明","attachment_ids",List.of()));}
    @Test void businessOutboxCommitsWithOrderAndRollsBackWithItsBusinessTransaction(){
        var outbox=ctx.getBean(OrderBusinessOutbox.class);
        var tx=new TransactionTemplate(ctx.getBean(org.springframework.transaction.PlatformTransactionManager.class));
        var rolledQuote=quote();String rolledOp=UUID.randomUUID().toString();
        assertThrows(IllegalStateException.class,()->tx.execute(status->{service.execute(member(),input(rolledOp,rolledQuote),receipt(rolledOp,rolledQuote));throw new IllegalStateException("test rollback");}));
        assertEquals(0,count("oms_order"));assertEquals(0,count("oms_agent_business_outbox"));
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        assertEquals(1,count("oms_order"));assertEquals(1,count("oms_agent_business_outbox"));
        tx.executeWithoutResult(status->outbox.enqueue(oid,0));
        assertEquals(1,count("oms_agent_business_outbox"));assertNull(outbox.claim());
    }
    @Test void businessOutboxPublisherFailureLeaseExpiryAndDuplicateConsumerConvergeWithoutDoubleRelease(){
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        var outbox=ctx.getBean(OrderBusinessOutbox.class);
        db.update("UPDATE oms_agent_business_outbox SET due_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND,next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        var first=outbox.claim();assertNotNull(first);outbox.failed(first);
        assertEquals("PENDING",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        db.update("UPDATE oms_agent_business_outbox SET next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        var second=outbox.claim();assertNotNull(second);assertNull(outbox.claim());
        db.update("UPDATE oms_agent_business_outbox SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        var recovered=outbox.claim();assertTrue(recovered.fence()>second.fence());
        outbox.published(second);assertEquals("PUBLISHING",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        outbox.published(recovered);outbox.consume(oid);outbox.consume(oid);outbox.failed(recovered);
        assertEquals("CONSUMED",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        assertEquals(4,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        assertEquals(0,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));assertEquals(5000,member().getIntegration());
        assertEquals(0,db.queryForObject("SELECT use_status FROM sms_coupon_history WHERE id=1",Integer.class));assertNull(outbox.claim());
    }
    @Test void businessOutboxEarlyEventRejectedAndPaidOrderIsNeverCancelled(){
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        var outbox=ctx.getBean(OrderBusinessOutbox.class);assertThrows(IllegalStateException.class,()->outbox.consume(oid));
        assertEquals(0,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        orders.paySuccess(oid,1);db.update("UPDATE oms_agent_business_outbox SET due_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        outbox.consume(oid);outbox.consume(oid);assertEquals(1,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        assertEquals(98,db.queryForObject("SELECT stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));assertEquals(4900,member().getIntegration());
    }
    @Test void expiredPublisherCannotReportBeforeAnotherPublisherClaimsAndRestartRepublishesLostNotification(){
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        var outbox=ctx.getBean(OrderBusinessOutbox.class);
        db.update("UPDATE oms_agent_business_outbox SET due_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND,next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        var expired=outbox.claim();assertNotNull(expired);
        db.update("UPDATE oms_agent_business_outbox SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        outbox.published(expired);outbox.failed(expired);
        assertEquals("PUBLISHING",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        assertNull(db.queryForObject("SELECT published_at FROM oms_agent_business_outbox WHERE order_id=?",java.sql.Timestamp.class,oid));
        var restarted=new OrderBusinessOutbox(db,ctx.getBean(org.springframework.transaction.PlatformTransactionManager.class),ctx.getBean(OrderResourceService.class));
        var next=restarted.claim();assertNotNull(next);assertTrue(next.fence()>expired.fence());restarted.published(next);
        assertEquals("SENT",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        assertNull(restarted.claim());
        // Simulate a confirmed broker message never reaching the consumer; durable SENT is retried.
        db.update("UPDATE oms_agent_business_outbox SET next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        var retry=restarted.claim();assertNotNull(retry);assertTrue(retry.fence()>next.fence());
        outbox.failed(expired);outbox.published(next);
        assertEquals("PUBLISHING",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        restarted.consume(oid);restarted.published(retry);restarted.failed(retry);restarted.consume(oid);
        assertEquals("CONSUMED",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        assertEquals(4,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        assertEquals(0,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));
        assertEquals(100,db.queryForObject("SELECT stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));
        assertEquals(5000,member().getIntegration());assertEquals(0,db.queryForObject("SELECT use_status FROM sms_coupon_history WHERE id=1",Integer.class));
        assertNull(restarted.claim());
    }
    @Test void consumerCrashAfterResourceMutationRollsBackOrderAndReceiptBeforeRestart(){
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        db.update("UPDATE oms_agent_business_outbox SET due_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND,next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        var resources=ctx.getBean(OrderResourceService.class);var failing=mock(OrderResourceService.class);
        when(failing.closePendingOrder(oid)).thenAnswer(inv->{resources.closePendingOrder(oid);throw new IllegalStateException("synthetic consumer crash before commit");});
        var manager=ctx.getBean(org.springframework.transaction.PlatformTransactionManager.class);
        var consumer=new OrderBusinessOutbox(db,manager,failing);
        assertThrows(IllegalStateException.class,()->consumer.consume(oid));
        assertEquals(0,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        assertEquals(2,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));
        assertEquals(4900,member().getIntegration());assertEquals(1,db.queryForObject("SELECT use_status FROM sms_coupon_history WHERE id=1",Integer.class));
        assertEquals("PENDING",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        assertNull(db.queryForObject("SELECT consumed_at FROM oms_agent_business_outbox WHERE order_id=?",java.sql.Timestamp.class,oid));
        var restarted=new OrderBusinessOutbox(db,manager,resources);restarted.consume(oid);restarted.consume(oid);
        assertEquals(4,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        assertEquals(0,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));
        assertEquals(5000,member().getIntegration());assertEquals(0,db.queryForObject("SELECT use_status FROM sms_coupon_history WHERE id=1",Integer.class));
        assertEquals("CONSUMED",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
    }
    @Test void actualPublisherProcessExitLeavesLeaseForNewProcessAndExpiredOwnerCannotOverwrite()throws Exception{
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        db.update("UPDATE oms_agent_business_outbox SET due_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND,next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        crashProcess("claim",oid);
        var abandoned=db.queryForMap("SELECT state,fencing_token,lease_owner FROM oms_agent_business_outbox WHERE order_id=?",oid);
        assertEquals("PUBLISHING",abandoned.get("state"));assertNotNull(abandoned.get("lease_owner"));
        var original=new OrderBusinessOutbox.Publication("order-timeout:"+oid,oid,abandoned.get("lease_owner").toString(),((Number)abandoned.get("fencing_token")).longValue());
        db.update("UPDATE oms_agent_business_outbox SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        var recovered=ctx.getBean(OrderBusinessOutbox.class);recovered.published(original);recovered.failed(original);
        assertEquals("PUBLISHING",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        var retry=recovered.claim();assertNotNull(retry);assertTrue(retry.fence()>original.fence());
        recovered.consume(oid);recovered.published(retry);recovered.consume(oid);
        assertEquals("CONSUMED",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        assertEquals(0,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));assertEquals(5000,member().getIntegration());
    }
    @Test void actualConsumerProcessExitBeforeCommitRollsBackAndNewConsumerClosesOnce()throws Exception{
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        db.update("UPDATE oms_agent_business_outbox SET due_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND,next_attempt_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE order_id=?",oid);
        crashProcess("consume",oid);
        assertEquals(0,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        assertEquals(2,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));assertEquals(4900,member().getIntegration());
        assertEquals("PENDING",db.queryForObject("SELECT state FROM oms_agent_business_outbox WHERE order_id=?",String.class,oid));
        assertNull(db.queryForObject("SELECT consumed_at FROM oms_agent_business_outbox WHERE order_id=?",java.sql.Timestamp.class,oid));
        var recovered=ctx.getBean(OrderBusinessOutbox.class);recovered.consume(oid);recovered.consume(oid);
        assertEquals(4,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        assertEquals(0,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));assertEquals(5000,member().getIntegration());
        assertEquals(0,db.queryForObject("SELECT use_status FROM sms_coupon_history WHERE id=1",Integer.class));
    }
    private void crashProcess(String mode,long oid)throws Exception{
        var marker=java.nio.file.Files.createTempFile("isolated-outbox-", ".marker");
        var log=java.nio.file.Files.createTempFile("isolated-outbox-", ".log");
        Process process=null;
        try{
            java.nio.file.Files.delete(marker);
            String javaExecutable=java.nio.file.Path.of(System.getProperty("java.home"),"bin","java.exe").toString();
            process=new ProcessBuilder(javaExecutable,"-cp",System.getProperty("java.class.path"),OutboxCrashWorker.class.getName(),mode,Long.toString(oid),marker.toString())
                    .redirectErrorStream(true).redirectOutput(log.toFile()).start();
            assertTrue(process.waitFor(45,TimeUnit.SECONDS),"Isolated worker must reach its explicit crash boundary");
            assertEquals(73,process.exitValue(),"Worker must terminate at the intended transaction boundary");
            assertTrue(java.nio.file.Files.size(marker)>0);
        }finally{
            if(process!=null&&process.isAlive()){process.destroyForcibly();process.waitFor(10,TimeUnit.SECONDS);}
            java.nio.file.Files.deleteIfExists(marker);java.nio.file.Files.deleteIfExists(log);
        }
    }
    @Test void originalLocalOrderDatesProjectToUtcAndRemainEligibleImmediatelyAfterReceipt(){
        long oid=completedOrder();var store=ctx.getBean(com.macro.mall.order.AgentAfterSaleStore.class);
        var summary=store.orderSummary(MEMBER,oid);Instant receipt=Instant.parse(summary.get("receive_time").toString());
        assertTrue(java.time.Duration.between(receipt,Instant.now()).abs().getSeconds()<10);
        long iid=db.queryForObject("SELECT MIN(id) FROM oms_order_item WHERE order_id=?",Long.class,oid);
        assertTrue((Boolean)store.eligibility(MEMBER,oid,iid).get("eligible"));
    }
    JsonNode newRule(long latest,int hours,boolean shipped,Instant effective){return json.valueToTree(Map.of("expected_latest_version",latest,"return_window_hours",hours,"allow_shipped",shipped,"effective_at",effective.toString(),"note","[TEST ONLY] versioned return rule"));}
    @Test void rulesAreImmutableIdempotentAndScheduledRulesDoNotApplyEarly(){
        var store=ctx.getBean(com.macro.mall.order.AgentAfterSaleStore.class);String key=UUID.randomUUID().toString();var body=newRule(1,72,false,Instant.now().plusSeconds(3600));
        var created=json.valueToTree(store.publishRule(3,key,body));assertEquals(2,created.path("version_no").asInt());
        assertEquals(created,json.valueToTree(store.publishRule(3,key,body)));
        assertEquals(com.macro.mall.order.AgentAfterSaleStore.RULE,store.ruleVersion(store.activeRule(false)));
        assertEquals(168,store.parse(db.queryForObject("SELECT rules_json FROM oms_after_sale_rule_version WHERE version_no=1",String.class)).path("return_window_hours").asInt());
        var conflict=assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.publishRule(3,key,newRule(1,48,false,Instant.now())));assertEquals("IDEMPOTENCY_CONFLICT",conflict.code);
        var stale=assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.publishRule(3,UUID.randomUUID().toString(),newRule(1,48,false,Instant.now())));assertEquals("VERSION_CONFLICT",stale.code);
        assertEquals(2,db.queryForObject("SELECT COUNT(*) FROM oms_after_sale_rule_version",Integer.class));
    }
    @Test void ruleSwitchInvalidatesOldQuotesAndExistingReturnsKeepOriginalRule(){
        long oid=completedOrder();long iid=db.queryForObject("SELECT MIN(id) FROM oms_order_item WHERE order_id=?",Long.class,oid);
        var store=ctx.getBean(com.macro.mall.order.AgentAfterSaleStore.class);var lifecycle=ctx.getBean(AgentOrderLifecycle.class);
        var initial=lifecycle.quote(member(),UUID.randomUUID(),returnParams(oid,iid,1));String originalOp=UUID.randomUUID().toString();
        var original=lifecycle.execute(member(),input(originalOp,initial),lifecycleReceipt(originalOp,initial));
        assertEquals("SUCCEEDED",original.path("state").asText());long rid=original.path("business_id").asLong();
        var old=lifecycle.quote(member(),UUID.randomUUID(),returnParams(oid,iid,1));
        store.publishRule(3,UUID.randomUUID().toString(),newRule(1,72,true,Instant.now()));
        String op=UUID.randomUUID().toString();var refused=lifecycle.execute(member(),input(op,old),lifecycleReceipt(op,old));
        assertEquals("REJECTED",refused.path("state").asText());assertEquals("QUOTE_CHANGED",refused.path("error_code").asText());
        assertEquals(1,count("oms_agent_return"));assertEquals(com.macro.mall.order.AgentAfterSaleStore.RULE,store.staffReturn(rid).get("rule_version"));
        var fresh=lifecycle.quote(member(),UUID.randomUUID(),returnParams(oid,iid,1));assertEquals("ITEM_NET_DEMO_V2",fresh.path("rule_version").asText());
        String next=UUID.randomUUID().toString();var accepted=lifecycle.execute(member(),input(next,fresh),lifecycleReceipt(next,fresh));assertEquals("SUCCEEDED",accepted.path("state").asText());
        assertEquals("ITEM_NET_DEMO_V2",accepted.path("return_apply").path("rule_version").asText());
    }
    @Test void changedRuleWindowAndShippedPermissionUseCurrentFacts(){
        long oid=completedOrder();long iid=db.queryForObject("SELECT MIN(id) FROM oms_order_item WHERE order_id=?",Long.class,oid);
        var store=ctx.getBean(com.macro.mall.order.AgentAfterSaleStore.class);
        db.update("UPDATE oms_order SET receive_time=DATE_SUB(NOW(),INTERVAL 100 HOUR) WHERE id=?",oid);
        assertTrue((Boolean)store.eligibility(MEMBER,oid,iid).get("eligible"));
        store.publishRule(3,UUID.randomUUID().toString(),newRule(1,72,false,Instant.now()));
        assertFalse((Boolean)store.eligibility(MEMBER,oid,iid).get("eligible"));
        db.update("UPDATE oms_order SET status=2 WHERE id=?",oid);assertFalse((Boolean)store.eligibility(MEMBER,oid,iid).get("eligible"));
        var invalid=assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.publishRule(3,UUID.randomUUID().toString(),newRule(2,0,true,Instant.now())));assertEquals("VALIDATION_ERROR",invalid.code);
    }
    @Test void itemReturnRequiresSeparateReviewReceiveAndDemoRefundAndReplayIsStable(){
        long oid=completedOrder();long iid=db.queryForObject("SELECT MIN(id) FROM oms_order_item WHERE order_id=?",Long.class,oid);var lifecycle=ctx.getBean(AgentOrderLifecycle.class);var store=ctx.getBean(com.macro.mall.order.AgentAfterSaleStore.class);
        var q=lifecycle.quote(member(),UUID.randomUUID(),returnParams(oid,iid,1));String op=UUID.randomUUID().toString();var receipt=lifecycleReceipt(op,q);var result=lifecycle.execute(member(),input(op,q),receipt);long rid=result.path("business_id").asLong();
        assertEquals("REQUESTED",result.path("return_apply").path("stage").asText());assertEquals(rid,lifecycle.execute(member(),input(op,q),receipt).path("business_id").asLong());
        assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.staffCommand(3,UUID.randomUUID().toString(),"REFUND",rid,json.valueToTree(Map.of("expected_version",1,"note","不能跳过审核收货"))));
        var review=store.staffCommand(3,UUID.randomUUID().toString(),"REVIEW",rid,json.valueToTree(Map.of("expected_version",1,"decision","APPROVE","approved_amount",q.path("pricing").path("payable").asText(),"company_address_id","1","note","批准演示退货")));
        assertEquals("APPROVED",review.path("stage").asText());
        var received=store.staffCommand(3,UUID.randomUUID().toString(),"RECEIVE",rid,json.valueToTree(Map.of("expected_version",2,"note","演示收到货物")));assertEquals("GOODS_RECEIVED",received.path("stage").asText());
        String key=UUID.randomUUID().toString();var refund=json.valueToTree(Map.of("expected_version",3,"note","独立演示退款，无真实资金"));var done=store.staffCommand(3,key,"REFUND",rid,refund);
        assertEquals("SIMULATED_REFUNDED",done.path("stage").asText());assertEquals(done,store.staffCommand(3,key,"REFUND",rid,refund));
        assertEquals(1,db.queryForObject("SELECT COUNT(*) FROM oms_agent_return WHERE simulated_refund_no IS NOT NULL",Integer.class));
        assertEquals(1,((Number)store.eligibility(MEMBER,oid,iid).get("remaining_quantity")).intValue());
        assertEquals(3,db.queryForObject("SELECT COUNT(*) FROM oms_agent_business_audit WHERE actor_kind='STAFF'",Integer.class));
    }
    @Test void concurrentReturnApplicationsCannotExceedPurchasedQuantity()throws Exception{
        long oid=completedOrder(),iid=db.queryForObject("SELECT MIN(id) FROM oms_order_item WHERE order_id=?",Long.class,oid);var life=ctx.getBean(AgentOrderLifecycle.class);
        var a=life.quote(member(),UUID.randomUUID(),returnParams(oid,iid,2));var b=life.quote(member(),UUID.randomUUID(),returnParams(oid,iid,2));
        ExecutorService pool=Executors.newFixedThreadPool(2);try{var jobs=new ArrayList<Future<JsonNode>>();for(JsonNode q:List.of(a,b)){String op=UUID.randomUUID().toString();jobs.add(pool.submit(()->life.execute(member(),input(op,q),lifecycleReceipt(op,q))));}int success=0;for(var job:jobs)if(job.get().path("state").asText().equals("SUCCEEDED"))success++;assertEquals(1,success);}finally{pool.shutdownNow();}
        assertEquals(2,db.queryForObject("SELECT SUM(quantity) FROM oms_agent_return WHERE stage<>'REJECTED'",Integer.class));
    }
    @Test void logisticsEditsAreVersionedAuditedAndNeverChangeOrderState(){
        long oid=completedOrder();var store=ctx.getBean(com.macro.mall.order.AgentAfterSaleStore.class);
        db.update("UPDATE oms_order SET create_time=NOW()-INTERVAL 2 HOUR,delivery_time=NOW()-INTERVAL 1 HOUR WHERE id=?",oid);
        Instant nodeTime=Instant.now().minusSeconds(1800),editTime=Instant.now().minusSeconds(900);
        var s=store.staffCommand(3,UUID.randomUUID().toString(),"SHIPMENT",oid,json.valueToTree(Map.of("expected_version",1,"carrier","演示快递","tracking_no","DEMO-1")));long sid=s.path("shipment_id").asLong();
        var n=store.staffCommand(3,UUID.randomUUID().toString(),"NODE",sid,json.valueToTree(Map.of("expected_shipment_version",2,"occurred_at",nodeTime.toString(),"location","演示分拨中心","description","演示运输")));long nid=n.path("id").asLong();
        var edit=store.staffCommand(3,UUID.randomUUID().toString(),"EDIT_NODE",nid,json.valueToTree(Map.of("expected_version",1,"occurred_at",editTime.toString(),"location","演示收货点","description","演示签收","reason","更正演示时间")));
        assertEquals(2,edit.path("version").asInt());assertEquals(3,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
        assertEquals(3,db.queryForObject("SELECT COUNT(*) FROM oms_agent_business_audit WHERE actor_kind='STAFF'",Integer.class));
    }
    JsonNode nodeInput(int version,Instant time){return json.valueToTree(Map.of("expected_shipment_version",version,"occurred_at",time.toString(),"location","演示地点","description","演示节点"));}
    @Test void invalidLogisticsTimesRollbackCommandsAndEditsPreserveAdjacentOrder(){
        long oid=completedOrder();var store=ctx.getBean(com.macro.mall.order.AgentAfterSaleStore.class);
        db.update("UPDATE oms_order SET create_time=NOW()-INTERVAL 3 HOUR,delivery_time=NOW()-INTERVAL 2 HOUR WHERE id=?",oid);
        var shipment=store.staffCommand(3,UUID.randomUUID().toString(),"SHIPMENT",oid,json.valueToTree(Map.of("expected_version",1,"carrier","演示快递","tracking_no","TEST-TIME")));long sid=shipment.path("shipment_id").asLong();
        for(Instant invalid:List.of(Instant.now().plusSeconds(60),Instant.now().minusSeconds(10860),Instant.now().minusSeconds(7260))){
            var ex=assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.staffCommand(3,UUID.randomUUID().toString(),"NODE",sid,nodeInput(2,invalid)));
            assertEquals("LOGISTICS_TIME_INVALID",ex.code);assertEquals(0,count("oms_agent_shipment_event"));
            assertEquals(1,count("oms_agent_staff_command"));assertEquals(1,count("oms_agent_business_audit"));
        }
        Instant firstTime=Instant.now().minusSeconds(5400),secondTime=Instant.now().minusSeconds(3600);
        var first=store.staffCommand(3,UUID.randomUUID().toString(),"NODE",sid,nodeInput(2,firstTime));
        String repeatKey=UUID.randomUUID().toString();var secondRequest=nodeInput(3,secondTime);
        var second=store.staffCommand(3,repeatKey,"NODE",sid,secondRequest);assertEquals(second,store.staffCommand(3,repeatKey,"NODE",sid,secondRequest));
        assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.staffCommand(3,UUID.randomUUID().toString(),"NODE",sid,nodeInput(4,firstTime.minusSeconds(1))));
        for(long node:List.of(first.path("id").asLong(),second.path("id").asLong())){
            Instant crossed=node==first.path("id").asLong()?secondTime.plusSeconds(1):firstTime.minusSeconds(1);
            assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.staffCommand(3,UUID.randomUUID().toString(),"EDIT_NODE",node,json.valueToTree(Map.of("expected_version",1,"occurred_at",crossed.toString(),"location","演示地点","description","不应保存","reason","时序反例"))));
        }
        // Equal timestamps are valid; IDs keep deterministic order and edits cannot cross either neighbour.
        store.staffCommand(3,UUID.randomUUID().toString(),"EDIT_NODE",first.path("id").asLong(),json.valueToTree(Map.of("expected_version",1,"occurred_at",second.path("occurred_at").asText(),"location","演示地点","description","同一秒节点","reason","合法边界")));
        assertEquals(2,count("oms_agent_shipment_event"));assertEquals(4,count("oms_agent_staff_command"));assertEquals(4,count("oms_agent_business_audit"));
        assertEquals(3,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));
    }
    @Test void oldCancellationCardsWithoutDisplaySummaryCannotExecute(){
        var q=quote();String op=UUID.randomUUID().toString();long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();var lifecycle=ctx.getBean(AgentOrderLifecycle.class);
        var cancel=(com.fasterxml.jackson.databind.node.ObjectNode)lifecycle.quote(member(),UUID.randomUUID(),json.valueToTree(Map.of("action_type","CANCEL_ORDER","order_id",Long.toString(oid))));
        cancel.put("rule_version","CANCEL_PENDING_V1");cancel.remove("cancellation");db.update("UPDATE oms_agent_quote SET pricing_snapshot=? WHERE quote_id=?",cancel.toString(),cancel.path("quote_id").asText());
        String cancellation=UUID.randomUUID().toString();assertEquals("QUOTE_CHANGED",lifecycle.execute(member(),input(cancellation,cancel),lifecycleReceipt(cancellation,cancel)).path("error_code").asText());
        assertEquals(0,db.queryForObject("SELECT status FROM oms_order WHERE id=?",Integer.class,oid));assertEquals(2,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));
    }
    @Test void readonlyQuoteAndDuplicateConcurrentConfirmUseOriginalOrderWriterOnce()throws Exception{
        JsonNode q=quote();assertEquals("189.00",q.path("pricing").path("payable").asText());assertEquals(0,count("oms_order"));assertEquals(1,count("oms_cart_item"));assertEquals(5000,member().getIntegration());
        String op=UUID.randomUUID().toString();JsonNode receipt=receipt(op,q);var pool=Executors.newFixedThreadPool(2);
        try {var jobs=pool.invokeAll(List.<Callable<JsonNode>>of(()->service.execute(member(),input(op,q),receipt),()->service.execute(member(),input(op,q),receipt)));for(var job:jobs){var result=job.get();assertEquals("SUCCEEDED",result.path("state").asText());assertEquals("189.00",result.path("order").path("pay_amount").asText());}}
        finally{pool.shutdownNow();}
        assertEquals(1,count("oms_order"));assertEquals(0,db.queryForObject("SELECT COUNT(*) FROM oms_cart_item WHERE delete_status=0",Integer.class));assertEquals(4900,member().getIntegration());assertEquals(2,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));assertEquals(1,db.queryForObject("SELECT use_status FROM sms_coupon_history WHERE id=1",Integer.class));
        assertEquals("SUCCEEDED",ops.reconcile(op,q.path("parameters_hash").asText(),receipt,true).path("state").asText());assertEquals(1,count("oms_order"));
    }
    @Test void priceChangeRequiresNewBuyerConfirmation(){JsonNode q=quote();db.update("UPDATE pms_sku_stock SET price=101 WHERE id=?",sku);String op=UUID.randomUUID().toString();assertEquals("QUOTE_CHANGED",service.execute(member(),input(op,q),receipt(op,q)).path("error_code").asText());assertEquals(0,count("oms_order"));assertEquals(5000,member().getIntegration());}
    @Test void fullAddressChangeIsDetectedEvenWhenMaskedPhoneIsUnchanged(){JsonNode q=quote();db.update("UPDATE ums_member_receive_address SET phone_number='13811110000' WHERE id=1");String op=UUID.randomUUID().toString();assertEquals("QUOTE_CHANGED",service.execute(member(),input(op,q),receipt(op,q)).path("error_code").asText());assertEquals(0,count("oms_order"));}
    @Test void finalizationPreventsLateOrderExecution(){JsonNode q=quote();String op=UUID.randomUUID().toString();JsonNode receipt=receipt(op,q);assertEquals("REJECTED",ops.reconcile(op,q.path("parameters_hash").asText(),receipt,true).path("state").asText());assertEquals("REJECTED",service.execute(member(),input(op,q),receipt).path("state").asText());assertEquals(0,count("oms_order"));}
    @Test void orderResourcesAndOperationRollbackTogether(){JsonNode q=quote();String op=UUID.randomUUID().toString();JsonNode receipt=receipt(op,q);var tx=new TransactionTemplate(ctx.getBean(DataSourceTransactionManager.class));assertThrows(IllegalStateException.class,()->tx.execute(s->{service.execute(member(),input(op,q),receipt);throw new IllegalStateException("injected failure before commit");}));assertEquals(0,count("oms_order"));assertEquals(0,count("oms_agent_operation"));assertEquals(5000,member().getIntegration());assertEquals(0,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));assertEquals(0,db.queryForObject("SELECT use_status FROM sms_coupon_history WHERE id=1",Integer.class));}
    @Test void foreignAddressCannotBeQuoted(){db.update("UPDATE ums_member_receive_address SET member_id=? WHERE id=1",MEMBER+1);assertThrows(AgentCartService.Rejected.class,this::quote);assertEquals(0,count("oms_order"));}

    @Test void batchedOrderReadsMatchDetailAndPreserveOwnershipAndPerOrderLimit() {
        JsonNode q=quote();String op=UUID.randomUUID().toString();
        long oid=service.execute(member(),input(op,q),receipt(op,q)).path("business_id").asLong();
        var store=ctx.getBean(com.macro.mall.order.AgentAfterSaleStore.class);
        assertEquals(store.orderSummary(MEMBER,oid),store.orderSummaries(MEMBER,List.of(oid)).get(0));
        assertTrue(store.orderSummaries(MEMBER,List.of()).isEmpty());
        assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.orderSummaries(MEMBER+1,List.of(oid)));
        assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.orderSummaries(MEMBER,List.of(oid,oid)));
        long item=db.queryForObject("SELECT MIN(id) FROM oms_order_item WHERE order_id=?",Long.class,oid);
        for(int n=0;n<102;n++)db.update("INSERT INTO oms_order_item(order_id,product_id,product_sku_id,product_name,product_quantity,product_price,real_amount) SELECT order_id,product_id,product_sku_id,product_name,product_quantity,product_price,real_amount FROM oms_order_item WHERE id=?",item);
        assertEquals(100,((List<?>)store.orderSummaries(MEMBER,List.of(oid)).get(0).get("lines")).size());
        assertEquals(store.orderSummary(MEMBER,oid),store.orderSummaries(MEMBER,List.of(oid)).get(0));
        db.update("UPDATE oms_order SET delete_status=1 WHERE id=?",oid);
        assertThrows(com.macro.mall.order.AgentAfterSaleStore.Rejected.class,()->store.orderSummaries(MEMBER,List.of(oid)));
    }

    @Test void residualCentsSurviveRealOrderPersistenceAndResourceRollback() {
        db.update("UPDATE pms_sku_stock SET price=1 WHERE id=?",sku);
        db.update("UPDATE oms_cart_item SET quantity=3 WHERE id=1");
        db.update("UPDATE sms_coupon SET amount=1 WHERE id=1");
        JsonNode q=quote();
        assertEquals("ORDER_CHECKOUT_CENTS_V2",q.path("rule_version").asText());
        assertEquals("1.00",q.path("pricing").path("payable").asText());
        assertEquals(1,q.path("lines").size());
        assertEquals("1.00",q.path("lines").get(0).path("line_payable").asText());
        String op=UUID.randomUUID().toString();
        JsonNode confirmation=receipt(op,q);
        JsonNode result=service.execute(member(),input(op,q),confirmation);
        assertEquals("SUCCEEDED",result.path("state").asText());
        long order=result.path("business_id").asLong();
        var totals=db.queryForMap("SELECT SUM(product_quantity) quantity,SUM(real_amount*product_quantity) paid,"
                +"SUM(coupon_amount*product_quantity) coupon,SUM(integration_amount*product_quantity) points "
                +"FROM oms_order_item WHERE order_id=?",order);
        for(String amount:List.of("paid","coupon","points"))
            assertEquals(0,new java.math.BigDecimal("1.00").compareTo((java.math.BigDecimal)totals.get(amount)));
        assertEquals(3,((Number)totals.get("quantity")).intValue());
        assertEquals(0,db.queryForObject("SELECT COUNT(*) FROM oms_order_item WHERE order_id=? AND real_amount<0",Integer.class,order));
        assertEquals(3,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));
        assertEquals(4900,member().getIntegration());
        assertTrue(ctx.getBean(OrderResourceService.class).closePendingOrder(order));
        assertEquals(5000,member().getIntegration());
        assertEquals(0,db.queryForObject("SELECT lock_stock FROM pms_sku_stock WHERE id=?",Integer.class,sku));
        assertEquals(0,db.queryForObject("SELECT use_status FROM sms_coupon_history WHERE id=1",Integer.class));
        assertEquals("SUCCEEDED",ops.reconcile(op,q.path("parameters_hash").asText(),confirmation,true).path("state").asText());
    }
}
