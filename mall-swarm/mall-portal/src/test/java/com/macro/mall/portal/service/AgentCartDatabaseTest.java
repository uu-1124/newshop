package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.mapper.OmsCartItemMapper;
import com.macro.mall.model.*;
import com.macro.mall.portal.dao.*;
import com.macro.mall.portal.service.impl.OmsCartItemServiceImpl;
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
import java.math.BigDecimal;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@EnabledIfEnvironmentVariable(named="MALL_CART_TEST_JDBC",matches="jdbc:mysql:.*")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AgentCartDatabaseTest {
    static final long MEMBER=880000001;
    private AnnotationConfigApplicationContext context;
    private JdbcTemplate jdbc;private AgentCartService service;private OmsCartItemService legacy;private ObjectMapper json;
    private long sku,product;private BigDecimal price;
    @Configuration @EnableTransactionManagement
    @MapperScan({"com.macro.mall.mapper","com.macro.mall.portal.dao"})
    static class Config {
        @Bean DataSource dataSource(){
            String url=System.getenv("MALL_CART_TEST_JDBC");
            if(!url.matches("jdbc:mysql://127\\.0\\.0\\.1:13306/mall_agent_cart_test\\?.*"))throw new IllegalStateException("Dedicated cart test DB required");
            return new DriverManagerDataSource(url,System.getenv("MALL_CART_TEST_USER"),System.getenv("MALL_CART_TEST_PASSWORD"));
        }
        @Bean SqlSessionFactory sqlSessionFactory(DataSource source)throws Exception{
            var bean=new SqlSessionFactoryBean();bean.setDataSource(source);bean.setMapperLocations(new PathMatchingResourcePatternResolver().getResources("classpath*:com/macro/mall/mapper/*.xml"));return bean.getObject();
        }
        @Bean DataSourceTransactionManager transactionManager(DataSource source){return new DataSourceTransactionManager(source);}
        @Bean JdbcTemplate jdbcTemplate(DataSource source){return new JdbcTemplate(source);}
        @Bean ObjectMapper objectMapper(){return new ObjectMapper();}
        @Bean CartGuard cartGuard(CartGuardDao dao){return new CartGuard(dao);}
        @Bean UmsMemberService memberService(){var mock=mock(UmsMemberService.class);var member=new UmsMember();member.setId(MEMBER);member.setNickname("cart-test");member.setStatus(1);when(mock.getCurrentMember()).thenReturn(member);return mock;}
        @Bean OmsPromotionService promotion(){return mock(OmsPromotionService.class);}
        @Bean OmsCartItemService cartItemService(){return new OmsCartItemServiceImpl();}
        @Bean AgentCartService agent(JdbcTemplate db,ObjectMapper json,CartGuard guard,OmsCartItemService carts){return new AgentCartService(db,json,guard,carts);}
    }
    @BeforeAll void start(){context=new AnnotationConfigApplicationContext(Config.class);jdbc=context.getBean(JdbcTemplate.class);service=context.getBean(AgentCartService.class);legacy=context.getBean(OmsCartItemService.class);json=context.getBean(ObjectMapper.class);var row=jdbc.queryForMap("SELECT id,product_id,price FROM pms_sku_stock ORDER BY id LIMIT 1");sku=((Number)row.get("id")).longValue();product=((Number)row.get("product_id")).longValue();price=(BigDecimal)row.get("price");}
    @AfterAll void stop(){if(context!=null)context.close();}
    @BeforeEach void reset(){jdbc.update("DELETE FROM oms_agent_operation");jdbc.update("DELETE FROM oms_agent_quote");jdbc.update("DELETE FROM oms_cart_item");jdbc.update("UPDATE pms_sku_stock SET stock=100,lock_stock=0,price=? WHERE id=?",price,sku);jdbc.update("UPDATE pms_product SET publish_status=1,delete_status=0 WHERE id=?",product);}
    JsonNode parameters(int qty){return json.valueToTree(Map.of("action_type","ADD_CART","items",List.of(Map.of("product_id",Long.toString(product),"sku_id",Long.toString(sku),"quantity",qty))));}
    JsonNode receipt(String op,JsonNode quote){return json.valueToTree(Map.of("operation_no",op,"action_id",op,"member_id",Long.toString(MEMBER),"action_type","ADD_CART","parameters_hash",quote.path("parameters_hash").asText(),"quote_id",quote.path("quote_id").asText(),"control_epoch",1,"confirmed_at",Instant.now().toString(),"confirmation_audit_id",UUID.randomUUID().toString()));}
    JsonNode input(String op,JsonNode quote,JsonNode receipt){return json.valueToTree(Map.of("operation_no",op,"quote_id",quote.path("quote_id").asText(),"parameters_hash",quote.path("parameters_hash").asText(),"parameters",quote.path("parameters"),"confirmation_receipt","unit-attestation","proofs",List.of()));}
    UmsMember member(){return context.getBean(UmsMemberService.class).getCurrentMember();}
    long total(){return jdbc.queryForObject("SELECT COALESCE(SUM(quantity),0) FROM oms_cart_item WHERE member_id=? AND delete_status=0",Long.class,MEMBER);}
    @Test void cartSnapshotIsMemberScopedPagedCurrentAndReadonly(){
        var item=new OmsCartItem();item.setProductId(product);item.setProductSkuId(sku);item.setQuantity(2);item.setPrice(price);legacy.add(item);
        jdbc.update("INSERT INTO oms_cart_item(member_id,product_id,product_sku_id,quantity,delete_status,product_name) VALUES(?,?,?,?,0,?)",MEMBER,product,sku,1,"second line");
        jdbc.update("INSERT INTO oms_cart_item(member_id,product_id,product_sku_id,quantity,delete_status,product_name) VALUES(?,?,?,?,0,?)",MEMBER+1,product,sku,1,"OTHER BUYER PRIVATE");
        long before=total();
        var first=service.snapshot(MEMBER,1,null);assertTrue(first.path("has_more").asBoolean());assertEquals(1,first.path("items").size());
        assertEquals(2,first.path("items").get(0).path("quantity").asInt());assertTrue(first.path("items").get(0).path("available").asBoolean());
        String cursor=first.path("next_cursor").asText();var second=service.snapshot(MEMBER,1,cursor);
        assertEquals(1,second.path("items").size());assertFalse(second.path("has_more").asBoolean());assertTrue(second.path("next_cursor").isNull());
        assertFalse(first.toString().contains("member_id"));assertFalse(second.toString().contains("OTHER BUYER PRIVATE"));
        jdbc.update("UPDATE pms_sku_stock SET lock_stock=stock,price=price+1 WHERE id=?",sku);
        var stale=service.snapshot(MEMBER,20,null);assertFalse(stale.path("items").get(0).path("available").asBoolean());
        assertEquals(0,stale.path("items").get(0).path("available_quantity").asInt());
        assertEquals(price.add(BigDecimal.ONE).setScale(2).toPlainString(),stale.path("items").get(0).path("unit_price").asText());
        assertEquals(before,total());assertEquals(0,jdbc.queryForObject("SELECT count(*) FROM oms_agent_quote",Integer.class));
        assertThrows(AgentCartService.Rejected.class,()->service.snapshot(MEMBER,21,null));
        assertThrows(AgentCartService.Rejected.class,()->service.snapshot(MEMBER,20,"9223372036854775808"));
    }
    @Test void prepareIsReadonlyThenConcurrentSameOperationMutatesOnce()throws Exception{
        JsonNode quote=service.quote(MEMBER,UUID.randomUUID(),parameters(2));assertEquals(0,total());String op=UUID.randomUUID().toString();JsonNode receipt=receipt(op,quote),input=input(op,quote,receipt);
        var pool=Executors.newFixedThreadPool(2);try{var tasks=pool.invokeAll(List.<Callable<JsonNode>>of(()->service.execute(member(),input,receipt),()->service.execute(member(),input,receipt)));for(var task:tasks)assertEquals("SUCCEEDED",task.get().path("state").asText());}finally{pool.shutdownNow();}
        assertEquals(2,total());assertEquals(1,jdbc.queryForObject("SELECT count(*) FROM oms_agent_operation",Integer.class));
        assertEquals(2,service.reconcile(op,quote.path("parameters_hash").asText(),receipt,true).path("cart_quantity").asInt());
        assertEquals(2,total());
    }
    @Test void finalizeAbsentPreventsLateExecute(){JsonNode quote=service.quote(MEMBER,UUID.randomUUID(),parameters(1));String op=UUID.randomUUID().toString();JsonNode receipt=receipt(op,quote);
        assertEquals("NOT_COMMITTED",service.reconcile(op,quote.path("parameters_hash").asText(),receipt,false).path("state").asText());
        assertEquals("RECOVERY_NOT_EXECUTED",service.reconcile(op,quote.path("parameters_hash").asText(),receipt,true).path("error_code").asText());
        assertEquals("REJECTED",service.execute(member(),input(op,quote,receipt),receipt).path("state").asText());assertEquals(0,total());}
    @Test void priceChangeRejectsWithoutCartMutation(){JsonNode quote=service.quote(MEMBER,UUID.randomUUID(),parameters(1));jdbc.update("UPDATE pms_sku_stock SET price=price+1 WHERE id=?",sku);String op=UUID.randomUUID().toString();JsonNode receipt=receipt(op,quote);assertEquals("QUOTE_CHANGED",service.execute(member(),input(op,quote,receipt),receipt).path("error_code").asText());assertEquals(0,total());}
    @Test void legacyAndAgentConcurrentAddsShareMutexWithoutLostUpdates()throws Exception{
        JsonNode quote=service.quote(MEMBER,UUID.randomUUID(),parameters(1));String op=UUID.randomUUID().toString();JsonNode receipt=receipt(op,quote);
        var pool=Executors.newFixedThreadPool(2);try{var jobs=pool.invokeAll(List.<Callable<Object>>of(()->service.execute(member(),input(op,quote,receipt),receipt),()->{var c=new OmsCartItem();c.setProductId(product);c.setProductSkuId(sku);c.setQuantity(1);c.setPrice(price);return legacy.add(c);}));for(var job:jobs)job.get();}finally{pool.shutdownNow();}
        assertEquals(2,total());assertEquals(1,jdbc.queryForObject("SELECT count(*) FROM oms_cart_item WHERE member_id=? AND delete_status=0",Integer.class,MEMBER));}
    @Test void cartAndOperationRollbackTogether(){JsonNode quote=service.quote(MEMBER,UUID.randomUUID(),parameters(1));String op=UUID.randomUUID().toString();JsonNode receipt=receipt(op,quote);var tx=new TransactionTemplate(context.getBean(DataSourceTransactionManager.class));assertThrows(IllegalStateException.class,()->tx.execute(s->{service.execute(member(),input(op,quote,receipt),receipt);throw new IllegalStateException("simulated lost process before commit");}));assertEquals(0,total());assertEquals(0,jdbc.queryForObject("SELECT count(*) FROM oms_agent_operation",Integer.class));assertEquals("RECOVERY_NOT_EXECUTED",service.reconcile(op,quote.path("parameters_hash").asText(),receipt,true).path("error_code").asText());}
    @Test void crossBuyerQuoteAndMismatchedSkuCannotExecute(){JsonNode quote=service.quote(MEMBER,UUID.randomUUID(),parameters(1));assertThrows(AgentCartService.Rejected.class,()->service.quote(MEMBER+1,UUID.fromString(quote.path("quote_id").asText()),parameters(1)));var invalid=json.valueToTree(Map.of("action_type","ADD_CART","items",List.of(Map.of("product_id",Long.toString(product+999999),"sku_id",Long.toString(sku),"quantity",1))));assertThrows(AgentCartService.Rejected.class,()->service.quote(MEMBER,UUID.randomUUID(),invalid));assertEquals(0,total());}
}
