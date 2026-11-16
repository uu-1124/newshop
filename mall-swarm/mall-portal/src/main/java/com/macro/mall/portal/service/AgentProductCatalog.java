package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.*;

/** Real catalog facts and scoped keyset pagination. No model-supplied SQL or URLs. */
@Service
public class AgentProductCatalog {
    public static class Rejected extends RuntimeException {
        public final String code;
        public final int status;
        public Rejected(String code, int status) { super(code); this.code = code; this.status = status; }
    }

    private final NamedParameterJdbcTemplate db;
    private final ObjectMapper json;
    private final String secret;
    private static final Set<String> FIELDS = Set.of("query", "limit", "cursor", "min_price", "max_price",
            "brand_id", "category_id", "sort", "in_stock", "specification");
    private static final Map<String, String> SORTS = Map.of("newest", "id", "price_asc", "catalog_price",
            "price_desc", "catalog_price", "sales", "catalog_sale");

    public AgentProductCatalog(JdbcTemplate jdbc, ObjectMapper json,
            @Value("${MALL_AGENT_BRIDGE_SECRET:}") String secret) {
        this.db = new NamedParameterJdbcTemplate(jdbc);
        this.json = json;
        this.secret = secret;
    }

    public Map<String, Object> search(long member, Map<String, String> supplied) {
        if (!FIELDS.containsAll(supplied.keySet())) throw new Rejected("VALIDATION_ERROR", 422);
        Map<String, String> filters = new TreeMap<>(supplied);
        String query = filters.getOrDefault("query", "").trim();
        String specification = filters.getOrDefault("specification", "").trim();
        if (query.length() > 200 || specification.length() > 100) throw new Rejected("VALIDATION_ERROR", 422);
        int limit;
        try { limit = Integer.parseInt(filters.getOrDefault("limit", "20")); }
        catch (NumberFormatException e) { throw new Rejected("VALIDATION_ERROR", 422); }
        if (limit < 1 || limit > 100) throw new Rejected("VALIDATION_ERROR", 422);
        String sort = filters.getOrDefault("sort", "newest");
        if (!SORTS.containsKey(sort)) throw new Rejected("VALIDATION_ERROR", 422);
        String stock = filters.getOrDefault("in_stock", "false");
        if (!Set.of("true", "false").contains(stock)) throw new Rejected("VALIDATION_ERROR", 422);
        BigDecimal min = price(filters.get("min_price")), max = price(filters.get("max_price"));
        if (min != null && max != null && min.compareTo(max) > 0) throw new Rejected("VALIDATION_ERROR", 422);
        Long brand = id(filters.get("brand_id")), category = id(filters.get("category_id"));
        Map<String, Object> scope = new TreeMap<>();
        scope.put("member", member); scope.put("query", query); scope.put("specification", specification);
        scope.put("sort", sort); scope.put("in_stock", stock); scope.put("min_price", min); scope.put("max_price", max);
        scope.put("brand_id", brand); scope.put("category_id", category); scope.put("limit", limit);
        String fingerprint = encode(scope);
        MapSqlParameterSource parameters = new MapSqlParameterSource()
                .addValue("query", "%" + escaped(query) + "%")
                .addValue("specification", "%" + escaped(specification) + "%");
        String skuWhere = "s.price>=0 AND s.stock IS NOT NULL";
        if (min != null) { skuWhere += " AND s.price>=:min"; parameters.addValue("min", min); }
        if (max != null) { skuWhere += " AND s.price<=:max"; parameters.addValue("max", max); }
        if (stock.equals("true")) skuWhere += " AND s.stock-COALESCE(s.lock_stock,0)>0";
        if (!specification.isEmpty()) skuWhere += " AND s.sp_data LIKE :specification";
        String where = "p.delete_status=0 AND p.publish_status=1 AND p.name IS NOT NULL AND p.product_category_id IS NOT NULL";
        if (!query.isEmpty()) where += " AND p.name LIKE :query";
        if (brand != null) { where += " AND p.brand_id=:brand"; parameters.addValue("brand", brand); }
        if (category != null) { where += " AND p.product_category_id=:category"; parameters.addValue("category", category); }
        String select = "SELECT p.id,p.name,p.brand_name,p.product_category_id,p.pic,"
                + "s.catalog_price,COALESCE(p.sale,0) catalog_sale FROM pms_product p JOIN "
                + "(SELECT s.product_id,MIN(s.price) catalog_price FROM pms_sku_stock s WHERE " + skuWhere
                + " GROUP BY s.product_id) s ON s.product_id=p.id WHERE " + where;
        String column = SORTS.get(sort);
        boolean ascending = sort.equals("price_asc");
        String comparison = ascending ? ">" : "<";
        String cursor = filters.get("cursor");
        String boundary = "";
        if (cursor != null) {
            JsonNode previous = decodeCursor(cursor, fingerprint);
            if (!previous.path("id").canConvertToLong() || previous.path("id").asLong() <= 0
                    || !previous.path("value").isTextual()) throw new Rejected("INVALID_CURSOR", 400);
            BigDecimal value;
            try { value = new BigDecimal(previous.path("value").asText()); }
            catch (NumberFormatException e) { throw new Rejected("INVALID_CURSOR", 400); }
            parameters.addValue("afterValue", value).addValue("afterId", previous.path("id").asLong());
            boundary = " WHERE (" + column + ",id) " + comparison + " (:afterValue,:afterId)";
        }
        parameters.addValue("take", limit + 1);
        List<Map<String, Object>> found = db.queryForList("SELECT * FROM (" + select + ") catalog" + boundary
                + " ORDER BY " + column + (ascending ? " ASC,id ASC" : " DESC,id DESC") + " LIMIT :take", parameters);
        boolean more = found.size() > limit;
        List<Map<String, Object>> page = found.subList(0, Math.min(limit, found.size()));
        List<Map<String, Object>> items = new ArrayList<>();
        for (Map<String, Object> row : page) {
            Map<String, Object> item = view(row);
            List<Map<String, Object>> options = skus(((Number) row.get("id")).longValue(), specification).stream().filter(s -> {
                BigDecimal amount = new BigDecimal(s.get("price").toString());
                return (min == null || amount.compareTo(min) >= 0) && (max == null || amount.compareTo(max) <= 0)
                        && (!stock.equals("true") || ((Number) s.get("available_quantity")).intValue() > 0);
            }).toList();
            item.put("sku_options", options);
            items.add(item);
        }
        String next = null;
        if (more && !page.isEmpty()) {
            Map<String, Object> last = page.get(page.size() - 1);
            next = cursor(fingerprint, ((Number) last.get("id")).longValue(), last.get(column).toString());
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("items", items); result.put("has_more", more); result.put("next_cursor", next);
        result.put("filters", facets());
        return result;
    }

    public Map<String, Object> detail(long productId) {
        List<Map<String, Object>> records = db.queryForList("SELECT p.id,p.name,p.brand_name,p.product_category_id,p.pic,"
                + "COALESCE((SELECT MIN(s.price) FROM pms_sku_stock s WHERE s.product_id=p.id AND s.price>=0),p.price) catalog_price "
                + "FROM pms_product p WHERE p.id=:id AND p.delete_status=0 AND p.publish_status=1 "
                + "AND p.name IS NOT NULL AND p.product_category_id IS NOT NULL", Map.of("id", productId));
        if (records.isEmpty() || records.get(0).get("catalog_price") == null) throw new Rejected("PRODUCT_UNAVAILABLE", 404);
        Map<String, Object> item = view(records.get(0));
        item.put("sku_options", skus(productId, ""));
        List<Map<String, Object>> facts = new ArrayList<>();
        for (Map<String, Object> row : db.queryForList("SELECT a.name,v.value FROM pms_product_attribute_value v "
                + "JOIN pms_product_attribute a ON a.id=v.product_attribute_id WHERE v.product_id=:id "
                + "AND a.name IS NOT NULL AND v.value IS NOT NULL ORDER BY a.sort,a.id,v.id LIMIT 40", Map.of("id", productId))) {
            facts.add(Map.of("name", bounded(row.get("name"), 80), "value", bounded(row.get("value"), 500)));
        }
        item.put("facts", facts);
        return item;
    }

    private Map<String, Object> facets() {
        List<Map<String, Object>> brands = db.queryForList("SELECT DISTINCT p.brand_id id,COALESCE(b.name,p.brand_name,'') name "
                + "FROM pms_product p LEFT JOIN pms_brand b ON b.id=p.brand_id WHERE p.delete_status=0 AND p.publish_status=1 "
                + "AND p.brand_id IS NOT NULL ORDER BY p.brand_id LIMIT 500", Map.of());
        List<Map<String, Object>> categories = db.queryForList("SELECT DISTINCT p.product_category_id id,COALESCE(c.name,'') name "
                + "FROM pms_product p LEFT JOIN pms_product_category c ON c.id=p.product_category_id WHERE p.delete_status=0 "
                + "AND p.publish_status=1 AND p.product_category_id IS NOT NULL ORDER BY p.product_category_id LIMIT 500", Map.of());
        return Map.of("brands", facetValues(brands), "categories", facetValues(categories));
    }

    private List<Map<String, Object>> facetValues(List<Map<String, Object>> values) {
        return values.stream().map(row -> Map.<String, Object>of("id", row.get("id").toString(), "name", bounded(row.get("name"), 100))).toList();
    }

    private Map<String, Object> view(Map<String, Object> row) {
        Map<String, Object> item = new LinkedHashMap<>();
        String id = row.get("id").toString();
        item.put("id", id); item.put("name", bounded(row.get("name"), 200));
        item.put("brand", bounded(row.get("brand_name"), 100));
        item.put("category_id", row.get("product_category_id").toString());
        item.put("price_from", money((BigDecimal) row.get("catalog_price")));
        item.put("image_url", bounded(row.get("pic"), 2048)); item.put("detail_path", "/pages/product/product?id=" + id);
        item.put("facts", List.of()); item.put("sku_options", List.of()); item.put("checked_at", Instant.now().toString());
        return item;
    }

    private List<Map<String, Object>> skus(long id, String specification) {
        List<Map<String, Object>> output = new ArrayList<>();
        for (Map<String, Object> row : db.queryForList("SELECT id,price,stock,lock_stock,sp_data FROM pms_sku_stock "
                + "WHERE product_id=:id AND price>=0 AND stock IS NOT NULL"
                + (specification.isEmpty() ? "" : " AND sp_data LIKE :specification") + " ORDER BY id LIMIT 100",
                Map.of("id", id, "specification", "%" + escaped(specification) + "%"))) {
            List<Map<String, Object>> attributes = new ArrayList<>();
            try {
                JsonNode values = json.readTree(Objects.toString(row.get("sp_data"), "[]"));
                if (values.isArray()) for (JsonNode attribute : values) {
                    if (attributes.size() < 20 && attribute.path("key").isTextual() && attribute.path("value").isTextual()) {
                        attributes.add(Map.of("name", bounded(attribute.path("key").asText(), 80), "value", bounded(attribute.path("value").asText(), 100)));
                    }
                }
            } catch (Exception ignored) { }
            long available = Math.max(0L, ((Number) row.get("stock")).longValue()
                    - (row.get("lock_stock") == null ? 0L : ((Number) row.get("lock_stock")).longValue()));
            output.add(Map.of("id", row.get("id").toString(), "price", money((BigDecimal) row.get("price")),
                    "available_quantity", Math.min(available, Integer.MAX_VALUE), "attributes", attributes, "visible", true));
        }
        return output;
    }

    private String cursor(String scope, long id, String value) {
        String payload = encode(Map.of("scope", scope, "id", id, "value", value));
        byte[] data = payload.getBytes(StandardCharsets.UTF_8);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(data) + "."
                + Base64.getUrlEncoder().withoutPadding().encodeToString(mac(data));
    }

    private JsonNode decodeCursor(String value, String scope) {
        try {
            if (value.length() > 2048) throw new IllegalArgumentException();
            String[] parts = value.split("\\.", -1);
            if (parts.length != 2) throw new IllegalArgumentException();
            byte[] payload = Base64.getUrlDecoder().decode(parts[0]);
            if (!MessageDigest.isEqual(mac(payload), Base64.getUrlDecoder().decode(parts[1]))) throw new IllegalArgumentException();
            JsonNode data = json.readTree(payload);
            if (!scope.equals(data.path("scope").asText())) throw new IllegalArgumentException();
            return data;
        } catch (Exception e) { throw new Rejected("INVALID_CURSOR", 400); }
    }

    private byte[] mac(byte[] data) {
        try {
            if (secret.length() < 32) throw new IllegalStateException("Catalog cursor secret is not configured");
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(("catalog-cursor-v1:" + secret).getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return mac.doFinal(data);
        } catch (Exception e) { throw new IllegalStateException("Catalog cursor signing unavailable"); }
    }

    private static String escaped(String value) { return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"); }
    private static String bounded(Object value, int length) { String text = Objects.toString(value, ""); return text.substring(0, Math.min(text.length(), length)); }
    private static String money(BigDecimal value) { return value.setScale(2, RoundingMode.UNNECESSARY).toPlainString(); }
    private static BigDecimal price(String value) {
        if (value == null) return null;
        if (!value.matches("(0|[1-9][0-9]{0,15})\\.[0-9]{2}")) throw new Rejected("VALIDATION_ERROR", 422);
        return new BigDecimal(value);
    }
    private static Long id(String value) {
        if (value == null) return null;
        try { if (!value.matches("[1-9][0-9]{0,18}")) throw new NumberFormatException(); return Long.parseLong(value); }
        catch (NumberFormatException e) { throw new Rejected("VALIDATION_ERROR", 422); }
    }
    private String encode(Object value) {
        try { return json.writeValueAsString(value); }
        catch (Exception e) { throw new IllegalStateException("Catalog projection is invalid"); }
    }
}
