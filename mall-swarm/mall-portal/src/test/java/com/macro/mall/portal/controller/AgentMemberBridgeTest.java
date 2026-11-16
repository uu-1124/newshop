package com.macro.mall.portal.controller;

import com.macro.mall.portal.service.PmsPortalProductService;
import com.macro.mall.portal.service.UmsMemberService;
import com.macro.mall.portal.service.AgentProductCatalog;
import com.macro.mall.portal.util.StpMemberUtil;
import com.macro.mall.model.PmsProduct;
import com.macro.mall.model.UmsMember;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.mock.web.MockHttpServletRequest;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Base64;
import java.util.HexFormat;
import java.util.UUID;
import java.util.List;
import java.util.Map;
import java.math.BigDecimal;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.*;

class AgentMemberBridgeTest {
    private final UmsMemberService members = mock(UmsMemberService.class);
    private final PmsPortalProductService products = mock(PmsPortalProductService.class);
    private final StringRedisTemplate redis = mock(StringRedisTemplate.class);
    private final String secret = "test-only-bridge-key-at-least-32-bytes";
    private final AgentMemberBridgeController controller = new AgentMemberBridgeController(members, products, redis, secret);

    @Test void unsignedAndTamperedRequestsNeverReadMemberData() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/agent-bridge/v1/identity");
        assertEquals(403, controller.identity(request).getStatusCode().value());
        sign(request, Instant.now().truncatedTo(ChronoUnit.MILLIS).toString());
        request.removeHeader("X-Agent-Nonce");
        request.addHeader("X-Agent-Nonce", UUID.randomUUID().toString());
        assertEquals(403, controller.identity(request).getStatusCode().value());
        verifyNoInteractions(members, products, redis);
    }

    @Test void expiredSignedRequestNeverClaimsNonceOrReadsData() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/agent-bridge/v1/identity");
        sign(request, Instant.now().minusSeconds(120).truncatedTo(ChronoUnit.MILLIS).toString());
        assertEquals(403, controller.identity(request).getStatusCode().value());
        verifyNoInteractions(members, products, redis);
    }

    @Test void validMemberSearchUsesTrustedCatalogAndCanonicalFilter() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/agent-bridge/v1/products");
        sign(request, Instant.now().toString());
        ValueOperations<String, String> values = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(values);
        when(values.setIfAbsent(anyString(), eq("1"), any(java.time.Duration.class))).thenReturn(true);
        UmsMember member = new UmsMember(); member.setId(11L); member.setStatus(1);
        when(members.getCurrentMember()).thenReturn(member);
        AgentProductCatalog catalog = mock(AgentProductCatalog.class);
        org.springframework.test.util.ReflectionTestUtils.setField(controller, "catalog", catalog);
        when(catalog.search(11L, Map.of("limit", "5"))).thenReturn(Map.of("items", List.of(Map.of("id", "1"))));
        try (var stp = mockStatic(StpMemberUtil.class)) {
            stp.when(StpMemberUtil::isLogin).thenReturn(true);
            var response = controller.products(request, 5, null);
            assertEquals(200, response.getStatusCode().value());
            Map<String,Object> page = (Map<String,Object>)response.getBody().get("data");
            List<Map<String,Object>> items = (List<Map<String,Object>>)page.get("items");
            assertEquals(1, items.size());
            assertEquals("1", items.get(0).get("id"));
            verify(catalog).search(11L, Map.of("limit", "5"));
        }
    }

    private PmsProduct product(Long id, int deleted, int published) {
        PmsProduct p = new PmsProduct(); p.setId(id); p.setName("test"); p.setDeleteStatus(deleted);
        p.setPublishStatus(published); p.setPrice(new BigDecimal("1.00")); p.setProductCategoryId(1L);
        return p;
    }

    private void sign(MockHttpServletRequest request, String timestamp) throws Exception {
        timestamp = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC).format(Instant.parse(timestamp));
        String nonce = UUID.randomUUID().toString();
        String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(new byte[0]));
        String canonical = String.join("\n", "GET", "/mall-portal" + request.getRequestURI(), hash, timestamp, nonce);
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        request.addHeader("X-Agent-Timestamp", timestamp);
        request.addHeader("X-Agent-Nonce", nonce);
        request.addHeader("X-Agent-Signature", "v1:" + Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8))));
    }
}
