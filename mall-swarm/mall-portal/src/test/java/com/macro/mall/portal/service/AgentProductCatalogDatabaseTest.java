package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.*;

import static org.junit.jupiter.api.Assertions.*;

@EnabledIfEnvironmentVariable(named="MALL_CART_TEST_JDBC", matches="jdbc:mysql:.*")
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class AgentProductCatalogDatabaseTest {
    private JdbcTemplate db;
    private TransactionTemplate transactions;
    private AgentProductCatalog catalog;
    private static final long BASE = 891000001L;

    @BeforeAll void setup() {
        String url = System.getenv("MALL_CART_TEST_JDBC");
        if (!url.matches("jdbc:mysql://127\\.0\\.0\\.1:13306/mall_agent_cart_test\\?.*")) {
            throw new IllegalStateException("Dedicated catalog test database required");
        }
        var source = new DriverManagerDataSource(url, System.getenv("MALL_CART_TEST_USER"), System.getenv("MALL_CART_TEST_PASSWORD"));
        db = new JdbcTemplate(source);
        transactions = new TransactionTemplate(new DataSourceTransactionManager(source));
        catalog = new AgentProductCatalog(db, new ObjectMapper(), "catalog-test-only-key-at-least-thirty-two-characters");
        db.execute("CREATE TABLE IF NOT EXISTS pms_brand(id bigint PRIMARY KEY,name varchar(100))");
        db.execute("CREATE TABLE IF NOT EXISTS pms_product_category(id bigint PRIMARY KEY,name varchar(100))");
        db.execute("CREATE TABLE IF NOT EXISTS pms_product_attribute(id bigint PRIMARY KEY,name varchar(100),sort int)");
        db.execute("CREATE TABLE IF NOT EXISTS pms_product_attribute_value(id bigint PRIMARY KEY,product_id bigint,product_attribute_id bigint,value varchar(500))");
    }

    private void fixture() {
        db.update("INSERT INTO pms_brand(id,name) VALUES (?,?)", BASE, "测试品牌");
        db.update("INSERT INTO pms_product_category(id,name) VALUES (?,?)", BASE, "测试分类");
        db.update("INSERT INTO pms_product_attribute(id,name,sort) VALUES (?,?,1)", BASE, "屏幕尺寸");
        for (int i = 0; i < 5; i++) {
            db.update("INSERT INTO pms_product(id,brand_id,brand_name,product_category_id,name,delete_status,publish_status,price,sale,product_sn) "
                    + "VALUES (?,?,'测试品牌',?,?,?, ?,999.00,?,?)", BASE + i, BASE, BASE,
                    "CATALOG_ONLY_" + i, i == 4 ? 1 : 0, i == 3 ? 0 : 1, i * 10, "CATALOG_ONLY_" + i);
            db.update("INSERT INTO pms_sku_stock(id,product_id,price,stock,lock_stock,sp_data,sku_code) "
                    + "VALUES (?,?,100.00,10,0,?,?)", BASE + i, BASE + i, "[{\"key\":\"颜色\",\"value\":\"黑色\"}]", "CATALOG_ONLY_" + i);
        }
        db.update("INSERT INTO pms_sku_stock(id,product_id,price,stock,lock_stock,sp_data,sku_code) VALUES (?,?,50.00,2,2,?,?)",
                BASE + 10, BASE, "[{\"key\":\"颜色\",\"value\":\"红色\"}]", "CATALOG_ONLY_RED");
        db.update("INSERT INTO pms_product_attribute_value(id,product_id,product_attribute_id,value) VALUES (?,?,?,?)",
                BASE, BASE, BASE, "6.1英寸");
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> items(Map<String, Object> page) { return (List<Map<String, Object>>) page.get("items"); }

    @Test void visibilityExactBudgetStockAndSpecificationUseSameSku() {
        transactions.execute(status -> {
            fixture();
            var all = catalog.search(1, Map.of("query", "CATALOG_ONLY_"));
            assertEquals(3, items(all).size());
            assertEquals("50.00", items(all).stream().filter(item -> item.get("id").equals(Long.toString(BASE))).findFirst().orElseThrow().get("price_from"));
            var noAffordableStock = catalog.search(1, Map.of("query", "CATALOG_ONLY_", "max_price", "60.00", "in_stock", "true"));
            assertTrue(items(noAffordableStock).isEmpty());
            var affordable = catalog.search(1, Map.of("query", "CATALOG_ONLY_", "min_price", "50.00", "max_price", "50.00", "specification", "红色"));
            assertEquals(1, items(affordable).size());
            assertTrue(items(catalog.search(1, Map.of("query", "CATALOG_ONLY_", "max_price", "49.99"))).isEmpty());
            assertTrue(items(catalog.search(1, Map.of("query", "CATALOG_ONLY_", "category_id", Long.toString(BASE + 9)))).isEmpty());
            assertEquals(3, items(catalog.search(1, Map.of("query", "CATALOG_ONLY_", "brand_id", Long.toString(BASE), "category_id", Long.toString(BASE)))).size());
            status.setRollbackOnly(); return null;
        });
    }

    @Test void keysetPaginationHasNoDuplicateWithEqualPricesAndIsScopedAndSigned() {
        transactions.execute(status -> {
            fixture();
            Map<String, String> filter = Map.of("query", "CATALOG_ONLY_", "sort", "price_asc", "limit", "1", "in_stock", "true");
            Set<String> found = new HashSet<>();
            Map<String, Object> page = catalog.search(8, filter);
            String firstCursor = page.get("next_cursor").toString();
            for (int i = 0; i < 3; i++) {
                assertEquals(1, items(page).size());
                assertTrue(found.add(items(page).get(0).get("id").toString()));
                if (Boolean.TRUE.equals(page.get("has_more"))) {
                    Map<String, String> next = new HashMap<>(filter); next.put("cursor", page.get("next_cursor").toString());
                    page = catalog.search(8, next);
                } else assertEquals(2, i);
            }
            Map<String, String> cross = new HashMap<>(filter); cross.put("cursor", firstCursor);
            assertEquals("INVALID_CURSOR", assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.search(9, cross)).code);
            cross.put("query", "different");
            assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.search(8, cross));
            cross.put("query", "CATALOG_ONLY_"); cross.put("cursor", firstCursor.substring(1));
            assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.search(8, cross));
            status.setRollbackOnly(); return null;
        });
    }

    @Test void detailReturnsActualFactsAndSkusAndRejectsDeletedOrUnpublished() {
        transactions.execute(status -> {
            fixture();
            var detail = catalog.detail(BASE);
            assertEquals("50.00", detail.get("price_from"));
            assertEquals(List.of(Map.of("name", "屏幕尺寸", "value", "6.1英寸")), detail.get("facts"));
            assertEquals(2, ((List<?>) detail.get("sku_options")).size());
            assertTrue(((List<?>) catalog.detail(BASE + 1).get("facts")).isEmpty());
            assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.detail(BASE + 3));
            assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.detail(BASE + 4));
            assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.detail(BASE + 100));
            status.setRollbackOnly(); return null;
        });
    }

    @Test void filtersRejectInjectionInvalidRangesAndUnknownFields() {
        assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.search(1, Map.of("sort", "price;DROP TABLE pms_product")));
        assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.search(1, Map.of("min_price", "100.01", "max_price", "100.00")));
        assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.search(1, Map.of("min_price", "1.001")));
        assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.search(1, Map.of("brand_id", "9223372036854775808")));
        assertThrows(AgentProductCatalog.Rejected.class, () -> catalog.search(1, Map.of("member_id", "2")));
        transactions.execute(status -> {
            fixture();
            assertTrue(items(catalog.search(1, Map.of("query", "CATALOG_ONLY_%"))).isEmpty());
            status.setRollbackOnly(); return null;
        });
    }
}
