package com.macro.mall.portal.service;

import com.macro.mall.common.exception.ApiException;
import com.macro.mall.mapper.UmsMemberReceiveAddressMapper;
import com.macro.mall.model.UmsMember;
import com.macro.mall.model.UmsMemberReceiveAddress;
import com.macro.mall.portal.dao.MemberAddressDao;
import com.macro.mall.portal.service.impl.UmsMemberReceiveAddressServiceImpl;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.context.annotation.*;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.transaction.annotation.EnableTransactionManagement;

import javax.sql.DataSource;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.*;
import java.util.function.IntSupplier;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/** Runs only against a disposable local schema; no business database access. */
@EnabledIfEnvironmentVariable(named = "MALL_FIX_JDBC", matches = "jdbc:mysql:.*")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class MemberAddressDatabaseTest {
    private AnnotationConfigApplicationContext context;
    private JdbcTemplate jdbc;
    private UmsMemberReceiveAddressService addresses;

    @Configuration
    @EnableTransactionManagement
    @MapperScan(basePackageClasses = {UmsMemberReceiveAddressMapper.class, MemberAddressDao.class})
    static class Config {
        @Bean DataSource dataSource() {
            String url = System.getenv("MALL_FIX_JDBC");
            if (!url.matches("jdbc:mysql://127\\.0\\.0\\.1:13306/mall_fix_test_[a-z0-9_]+\\?.*")) {
                throw new IllegalStateException("Only disposable test schemas are allowed");
            }
            return new DriverManagerDataSource(url, System.getenv("MALL_FIX_DB_USER"),
                    System.getenv("MALL_FIX_DB_PASSWORD"));
        }
        @Bean SqlSessionFactory sqlSessionFactory(DataSource source) throws Exception {
            var factory = new SqlSessionFactoryBean();
            factory.setDataSource(source);
            factory.setMapperLocations(new PathMatchingResourcePatternResolver()
                    .getResources("classpath*:com/macro/mall/mapper/*.xml"));
            return factory.getObject();
        }
        @Bean DataSourceTransactionManager transactionManager(DataSource source) {
            return new DataSourceTransactionManager(source);
        }
        @Bean UmsMemberService memberService() {
            UmsMemberService service = mock(UmsMemberService.class);
            UmsMember member = new UmsMember();
            member.setId(1L);
            when(service.getCurrentMember()).thenReturn(member);
            return service;
        }
        @Bean UmsMemberReceiveAddressService addressService() {
            return new UmsMemberReceiveAddressServiceImpl();
        }
    }

    @BeforeAll void start() {
        context = new AnnotationConfigApplicationContext(Config.class);
        jdbc = new JdbcTemplate(context.getBean(DataSource.class));
        addresses = context.getBean(UmsMemberReceiveAddressService.class);
    }
    @AfterAll void stop() { if (context != null) context.close(); }

    @BeforeEach void reset() {
        jdbc.update("DELETE FROM ums_member_receive_address");
        jdbc.update("DELETE FROM ums_member");
        jdbc.update("INSERT INTO ums_member(id) VALUES(1),(2)");
        seed(10, 1, 1);
        seed(11, 1, 0);
        seed(20, 2, 1);
    }
    private void seed(long id, long member, int status) {
        jdbc.update("INSERT INTO ums_member_receive_address(id,member_id,name,default_status) VALUES(?,?,?,?)",
                id, member, "address-" + id, status);
    }
    private UmsMemberReceiveAddress address(Integer status) {
        var address = new UmsMemberReceiveAddress();
        address.setName("test");
        address.setDefaultStatus(status);
        return address;
    }
    private int defaults(long member) {
        return jdbc.queryForObject("SELECT COUNT(*) FROM ums_member_receive_address WHERE member_id=? AND default_status=1",
                Integer.class, member);
    }
    private int status(long id) {
        return jdbc.queryForObject("SELECT default_status FROM ums_member_receive_address WHERE id=?", Integer.class, id);
    }
    private void race(List<IntSupplier> calls) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(calls.size());
        CountDownLatch ready = new CountDownLatch(calls.size()), go = new CountDownLatch(1);
        try {
            List<Future<Integer>> results = new ArrayList<>();
            for (IntSupplier call : calls) results.add(pool.submit(() -> {
                ready.countDown();
                if (!go.await(10, TimeUnit.SECONDS)) throw new IllegalStateException("start timeout");
                return call.getAsInt();
            }));
            assertTrue(ready.await(10, TimeUnit.SECONDS));
            go.countDown();
            for (Future<Integer> result : results) assertEquals(1, result.get(30, TimeUnit.SECONDS));
        } finally { go.countDown(); pool.shutdownNow(); }
    }

    @Test void addingDefaultReplacesOldDefaultOnlyForCurrentMember() {
        var added = address(1);
        assertEquals(1, addresses.add(added));
        assertEquals(0, status(10));
        assertEquals(1, status(added.getId()));
        assertEquals(1, defaults(1));
        assertEquals(1, defaults(2));
    }
    @Test void addingWithoutDefaultStatusKeepsExistingDefaultAndOverridesIdentity() {
        var added = address(null);
        added.setId(20L);
        added.setMemberId(2L);
        assertEquals(1, addresses.add(added));
        assertNotEquals(20L, added.getId());
        assertEquals(1L, added.getMemberId());
        assertEquals(0, status(added.getId()));
        assertEquals(1, status(10));
        assertEquals(1, status(20));
    }
    @Test void switchingDefaultAlsoRepairsExistingDuplicates() {
        seed(12, 1, 1);
        assertEquals(1, addresses.update(11L, address(1)));
        assertEquals(1, status(11));
        assertEquals(0, status(10));
        assertEquals(0, status(12));
        assertEquals(1, defaults(1));
        assertEquals(1, defaults(2));
    }
    @Test void omittedStatusUpdatesOtherFieldsAndPreservesDefault() {
        var patch = address(null);
        patch.setMemberId(2L);
        patch.setId(20L);
        assertEquals(1, addresses.update(10L, patch));
        assertEquals("test", addresses.getItem(10L).getName());
        assertEquals(1L, addresses.getItem(10L).getMemberId());
        assertEquals(1, status(10));
        assertEquals(1, status(20));
    }
    @Test void foreignAndMissingTargetsDoNotClearDefaultsOrTransferOwnership() {
        assertEquals(0, addresses.update(20L, address(1)));
        assertEquals(0, addresses.update(999L, address(1)));
        assertEquals(1, status(10));
        assertEquals(1, status(20));
        assertNull(addresses.getItem(20L));
        assertEquals(2, addresses.list().size());
    }
    @Test void invalidDefaultStatusesAreRejectedBeforeWriting() {
        for (int status : List.of(-1, 2)) {
            assertThrows(ApiException.class, () -> addresses.add(address(status)));
            assertThrows(ApiException.class, () -> addresses.update(11L, address(status)));
        }
        assertEquals(2, addresses.list().size());
        assertEquals(1, defaults(1));
    }
    @Test void failedInsertRollsBackClearingTheOldDefault() {
        var invalid = address(1);
        invalid.setName("x".repeat(1000));
        assertThrows(RuntimeException.class, () -> addresses.add(invalid));
        assertEquals(1, status(10));
        assertEquals(2, addresses.list().size());
    }
    @Test void failedUpdateRollsBackClearingTheOldDefault() {
        var invalid = address(1);
        invalid.setName("x".repeat(1000));
        assertThrows(RuntimeException.class, () -> addresses.update(11L, invalid));
        assertEquals(1, status(10));
        assertEquals(0, status(11));
    }
    @Test void deletionAndUnsettingDefaultRespectOwnership() {
        assertEquals(0, addresses.delete(20L));
        assertEquals(1, status(20));
        assertEquals(1, addresses.update(10L, address(0)));
        assertEquals(0, defaults(1));
        assertEquals(1, addresses.delete(10L));
        assertNull(addresses.getItem(10L));
    }
    @Test void concurrentFirstDefaultsRemainUnique() throws Exception {
        jdbc.update("DELETE FROM ums_member_receive_address WHERE member_id=1");
        List<IntSupplier> calls = new ArrayList<>();
        for (int i = 0; i < 8; i++) calls.add(() -> addresses.add(address(1)));
        race(calls);
        assertEquals(8, addresses.list().size());
        assertEquals(1, defaults(1));
    }
    @Test void concurrentSwitchesRemainUnique() throws Exception {
        List<IntSupplier> calls = new ArrayList<>();
        for (long id : List.of(10L, 11L, 10L, 11L, 10L, 11L, 10L, 11L)) {
            calls.add(() -> addresses.update(id, address(1)));
        }
        race(calls);
        assertEquals(1, defaults(1));
    }
    @Test void concurrentCreateAndSwitchRemainUnique() throws Exception {
        race(List.of(() -> addresses.add(address(1)), () -> addresses.update(11L, address(1)),
                () -> addresses.add(address(1)), () -> addresses.update(10L, address(1))));
        assertEquals(1, defaults(1));
        assertEquals(1, defaults(2));
    }
}
