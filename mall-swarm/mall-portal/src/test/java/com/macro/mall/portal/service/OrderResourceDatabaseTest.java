package com.macro.mall.portal.service;

import com.macro.mall.common.service.RedisService;
import com.macro.mall.mapper.*;
import com.macro.mall.model.OmsOrder;
import com.macro.mall.model.OmsOrderItem;
import com.macro.mall.order.OrderResourceService;
import com.macro.mall.order.dao.OrderResourceDao;
import com.macro.mall.portal.dao.PortalOrderDao;
import com.macro.mall.portal.service.impl.OmsPortalOrderServiceImpl;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.context.annotation.*;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.transaction.annotation.EnableTransactionManagement;
import org.springframework.transaction.support.TransactionTemplate;
import javax.sql.DataSource;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.Supplier;
import java.util.stream.Stream;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Opt-in tests against a disposable schema, never the business database. */
@EnabledIfEnvironmentVariable(named = "MALL_FIX_JDBC", matches = "jdbc:mysql:.*")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OrderResourceDatabaseTest {
    private AnnotationConfigApplicationContext context;
    private JdbcTemplate jdbc;
    private OrderResourceService resources;
    private TransactionTemplate transaction;
    private OmsPortalOrderServiceImpl portal;

    @Configuration
    @EnableTransactionManagement
    @MapperScan({"com.macro.mall.mapper", "com.macro.mall.order.dao", "com.macro.mall.portal.dao"})
    static class Config {
        @Bean DataSource dataSource() {
            String url = System.getenv("MALL_FIX_JDBC");
            if (!url.matches("jdbc:mysql://127\\.0\\.0\\.1:13306/mall_fix_test_[a-z0-9_]+\\?.*")) {
                throw new IllegalStateException("Only disposable test schemas are allowed");
            }
            return new DriverManagerDataSource(url, System.getenv("MALL_FIX_DB_USER"), System.getenv("MALL_FIX_DB_PASSWORD"));
        }
        @Bean SqlSessionFactory sqlSessionFactory(DataSource source) throws Exception {
            var factory = new SqlSessionFactoryBean();
            factory.setDataSource(source);
            var resolver = new PathMatchingResourcePatternResolver();
            factory.setMapperLocations(Stream.concat(
                    Arrays.stream(resolver.getResources("classpath*:com/macro/mall/mapper/*.xml")),
                    Arrays.stream(resolver.getResources("classpath:dao/PortalOrderDao.xml"))).toArray(Resource[]::new));
            return factory.getObject();
        }
        @Bean DataSourceTransactionManager transactionManager(DataSource source) {
            return new DataSourceTransactionManager(source);
        }
        @Bean RedisService redisService() { return mock(RedisService.class); }
        @Bean OrderResourceService orderResourceService(OmsOrderMapper orders, OmsOrderItemMapper items,
                OrderResourceDao dao, RedisService redis) {
            return new OrderResourceService(orders, items, dao, redis, "test", "ums:member");
        }
    }

    @BeforeAll void start() {
        context = new AnnotationConfigApplicationContext(Config.class);
        jdbc = new JdbcTemplate(context.getBean(DataSource.class));
        resources = context.getBean(OrderResourceService.class);
        transaction = new TransactionTemplate(context.getBean(DataSourceTransactionManager.class));
        portal = new OmsPortalOrderServiceImpl();
        ReflectionTestUtils.setField(portal, "orderMapper", context.getBean(OmsOrderMapper.class));
        ReflectionTestUtils.setField(portal, "portalOrderDao", context.getBean(PortalOrderDao.class));
        ReflectionTestUtils.setField(portal, "orderResources", resources);
    }
    @AfterAll void stop() { if (context != null) context.close(); }

    @BeforeEach void reset() {
        for (String table : List.of("oms_order_item", "oms_order", "pms_sku_stock", "sms_coupon_history", "ums_member")) {
            jdbc.update("DELETE FROM " + table);
        }
        jdbc.update("INSERT INTO pms_sku_stock(id,sku_code,stock,lock_stock) VALUES(100,'test',10,0)");
        jdbc.update("INSERT INTO ums_member(id,integration) VALUES(1,100)");
        clearInvocations(context.getBean(RedisService.class));
    }

    private void order(long id, int status, int points, boolean coupon) {
        jdbc.update("INSERT INTO oms_order(id,member_id,order_sn,status,delete_status,receiver_name,receiver_phone,use_integration,coupon_id) "
                + "VALUES(?,1,?,?,0,'test','test',?,?)", id, "test-" + id, status, points, coupon ? 9L : null);
        jdbc.update("INSERT INTO oms_order_item(order_id,product_sku_id,product_quantity) VALUES(?,100,2)", id);
        jdbc.update("UPDATE pms_sku_stock SET lock_stock=lock_stock+2 WHERE id=100");
        if (coupon) jdbc.update("INSERT INTO sms_coupon_history(id,coupon_id,member_id,use_status,order_id,order_sn) VALUES(?,9,1,1,?,?)", id, id, "test-" + id);
    }
    private int value(String table, String column, long id) {
        return jdbc.queryForObject("SELECT " + column + " FROM " + table + " WHERE id=?", Integer.class, id);
    }
    private OmsOrderItem item(long sku, int quantity) {
        var item = new OmsOrderItem(); item.setProductSkuId(sku); item.setProductQuantity(quantity); return item;
    }
    private List<Boolean> race(List<Supplier<Boolean>> calls) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(calls.size());
        CountDownLatch ready = new CountDownLatch(calls.size()), go = new CountDownLatch(1);
        try {
            List<Future<Boolean>> results = new ArrayList<>();
            for (Supplier<Boolean> call : calls) results.add(pool.submit(() -> {
                ready.countDown(); go.await(); return call.get();
            }));
            assertTrue(ready.await(10, TimeUnit.SECONDS)); go.countDown();
            List<Boolean> values = new ArrayList<>();
            for (Future<Boolean> result : results) values.add(result.get(30, TimeUnit.SECONDS));
            return values;
        } finally { go.countDown(); pool.shutdownNow(); }
    }

    @Test void cancellationReleasesResourcesExactlyOnce() {
        order(1, 0, 50, true);
        assertTrue(resources.closePendingOrder(1L));
        assertFalse(resources.closePendingOrder(1L));
        assertEquals(4, value("oms_order", "status", 1));
        assertEquals(0, value("pms_sku_stock", "lock_stock", 100));
        assertEquals(10, value("pms_sku_stock", "stock", 100));
        assertEquals(150, value("ums_member", "integration", 1));
        assertEquals(0, value("sms_coupon_history", "use_status", 1));
        assertNull(jdbc.queryForObject("SELECT order_id FROM sms_coupon_history WHERE id=1", Long.class));
        verify(context.getBean(RedisService.class), times(2)).del("test:ums:member:1");
    }

    @Test void concurrentCancellationHasOneWinner() throws Exception {
        order(1, 0, 50, true);
        var calls = new ArrayList<Supplier<Boolean>>();
        for (int i = 0; i < 8; i++) calls.add(() -> resources.closePendingOrder(1L));
        assertEquals(1, race(calls).stream().filter(Boolean::booleanValue).count());
        assertEquals(0, value("pms_sku_stock", "lock_stock", 100));
        assertEquals(150, value("ums_member", "integration", 1));
    }

    @Test void paymentAndCancellationCannotBothWin() throws Exception {
        for (long id = 1; id <= 10; id++) {
            reset();
            order(id, 0, 0, false);
            final long current = id;
            race(List.of(() -> resources.closePendingOrder(current), () -> {
                try { return transaction.execute(s -> portal.paySuccess(current, 1)) > 0; }
                catch (com.macro.mall.common.exception.ApiException expected) { return false; }
            }));
            int status = value("oms_order", "status", current);
            assertTrue(status == 1 || status == 4);
            assertEquals(0, value("pms_sku_stock", "lock_stock", 100));
            assertEquals(status == 1 ? 8 : 10, value("pms_sku_stock", "stock", 100));
        }
    }

    @Test void concurrentReservationsNeverOversell() throws Exception {
        var calls = new ArrayList<Supplier<Boolean>>();
        for (int i = 0; i < 12; i++) calls.add(() -> {
            try { resources.reserveStock(List.of(item(100, 1))); return true; }
            catch (com.macro.mall.common.exception.ApiException expected) { return false; }
        });
        assertEquals(10, race(calls).stream().filter(Boolean::booleanValue).count());
        assertEquals(10, value("pms_sku_stock", "lock_stock", 100));
    }

    @Test void duplicateSkuLinesAreSummed() {
        resources.reserveStock(List.of(item(100, 2), item(100, 3)));
        assertEquals(5, value("pms_sku_stock", "lock_stock", 100));
        assertEquals(1, resources.deductStock(List.of(item(100, 2), item(100, 3))));
        assertEquals(5, value("pms_sku_stock", "stock", 100));
        assertEquals(0, value("pms_sku_stock", "lock_stock", 100));
    }

    @Test void missingSkuRollsBackEarlierStockReleaseAndStatus() {
        order(1, 0, 50, true);
        jdbc.update("INSERT INTO oms_order_item(order_id,product_sku_id,product_quantity) VALUES(1,101,1)");
        assertThrows(RuntimeException.class, () -> resources.closePendingOrder(1L));
        assertEquals(0, value("oms_order", "status", 1));
        assertEquals(2, value("pms_sku_stock", "lock_stock", 100));
        assertEquals(100, value("ums_member", "integration", 1));
        assertEquals(1, value("sms_coupon_history", "use_status", 1));
    }

    @Test void unboundHistoricalCouponIsPreservedAndClosureRolledBack() {
        order(1, 0, 0, true);
        jdbc.update("UPDATE sms_coupon_history SET order_id=NULL,order_sn=NULL WHERE id=1");
        assertThrows(RuntimeException.class, () -> resources.closePendingOrder(1L));
        assertEquals(0, value("oms_order", "status", 1));
        assertEquals(2, value("pms_sku_stock", "lock_stock", 100));
        assertEquals(1, value("sms_coupon_history", "use_status", 1));
    }

    @Test void insufficientLockedStockRollsBackPayment() {
        order(1, 0, 0, false);
        jdbc.update("UPDATE pms_sku_stock SET lock_stock=1 WHERE id=100");
        assertThrows(RuntimeException.class, () -> transaction.execute(s -> portal.paySuccess(1L, 1)));
        assertEquals(0, value("oms_order", "status", 1));
        assertEquals(10, value("pms_sku_stock", "stock", 100));
        assertEquals(1, value("pms_sku_stock", "lock_stock", 100));
    }

    @Test void paidShippedCompletedDeletedAndMissingOrdersAreUntouched() {
        for (int status : List.of(1, 2, 3, 4)) {
            order(status, status, 50, true);
            assertFalse(resources.closePendingOrder((long) status));
            assertEquals(status, value("oms_order", "status", status));
        }
        order(5, 0, 50, true);
        jdbc.update("UPDATE oms_order SET delete_status=1 WHERE id=5");
        assertFalse(resources.closePendingOrder(5L));
        assertFalse(resources.closePendingOrder(999L));
        assertEquals(100, value("ums_member", "integration", 1));
    }

    @Test void pointsConsumptionIsAtomicAndRollsBackOnOuterFailure() throws Exception {
        assertEquals(1, race(List.of(() -> consume(80), () -> consume(80))).stream().filter(Boolean::booleanValue).count());
        assertEquals(20, value("ums_member", "integration", 1));
        assertThrows(RuntimeException.class, () -> transaction.execute(s -> {
            resources.consumePoints(1L, 10); throw new IllegalStateException("later failure");
        }));
        assertEquals(20, value("ums_member", "integration", 1));
    }
    private boolean consume(int points) {
        try { resources.consumePoints(1L, points); return true; }
        catch (com.macro.mall.common.exception.ApiException expected) { return false; }
    }

    @Test void sameCouponCannotBeBoundToTwoOrders() throws Exception {
        jdbc.update("INSERT INTO sms_coupon_history(id,coupon_id,member_id,use_status) VALUES(1,9,1,0)");
        List<Supplier<Boolean>> calls = new ArrayList<>();
        for (long id : List.of(1L, 2L)) calls.add(() -> {
            OmsOrder order = new OmsOrder(); order.setId(id); order.setOrderSn("test-" + id);
            order.setMemberId(1L); order.setCouponId(9L);
            try { resources.consumeCoupon(order); return true; }
            catch (com.macro.mall.common.exception.ApiException expected) { return false; }
        });
        assertEquals(1, race(calls).stream().filter(Boolean::booleanValue).count());
        assertEquals(1, value("sms_coupon_history", "use_status", 1));
    }
}
