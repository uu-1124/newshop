package com.macro.mall.controller;

import cn.dev33.satoken.stp.StpUtil;
import com.macro.mall.model.UmsAdmin;
import com.macro.mall.model.UmsResource;
import com.macro.mall.service.UmsAdminService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.MockedStatic;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.mock.web.MockHttpServletRequest;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class AgentStaffBridgeTest {
    private final UmsAdminService admins = mock(UmsAdminService.class);
    private final StringRedisTemplate redis = mock(StringRedisTemplate.class);
    @SuppressWarnings("unchecked")
    private final ValueOperations<String, String> values = mock(ValueOperations.class);
    private final String secret = "unit-staff-secret";
    private final AgentStaffBridgeController controller = new AgentStaffBridgeController(admins, redis, secret);

    @BeforeEach void setup() {
        when(redis.opsForValue()).thenReturn(values);
        when(values.setIfAbsent(anyString(), eq("1"), any(Duration.class))).thenReturn(true);
    }

    private MockHttpServletRequest signed(String domain, Instant instant) throws Exception {
        return signed(domain, instant, "/agent-bridge/v1/identity");
    }

    private MockHttpServletRequest signed(String domain, Instant instant, String path) throws Exception {
        var request = new MockHttpServletRequest("GET", path);
        String timestamp = instant.toString(), nonce = UUID.randomUUID().toString();
        String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(new byte[0]));
        String canonical = String.join("\n", "GET", domain + request.getRequestURI(), hash, timestamp, nonce);
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        request.addHeader("X-Agent-Timestamp", timestamp);
        request.addHeader("X-Agent-Nonce", nonce);
        request.addHeader("X-Agent-Signature", "v1:" + Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8))));
        return request;
    }

    @Test void transferTargetRequiresFreshEnabledAccountAndReceptionPermissions() throws Exception {
        String path = "/agent-bridge/v1/staff/8/eligibility";
        try (MockedStatic<StpUtil> auth = mockStatic(StpUtil.class)) {
            auth.when(StpUtil::isLogin).thenReturn(true);
            auth.when(StpUtil::getLoginIdAsLong).thenReturn(7L);
            var caller = new UmsAdmin(); caller.setId(7L); caller.setStatus(1);
            var target = new UmsAdmin(); target.setId(8L); target.setStatus(1);
            when(admins.getItem(7L)).thenReturn(caller);
            when(admins.getItem(8L)).thenReturn(target);
            when(admins.getAgentResourceList(7L)).thenReturn(List.of(permission("agent:handoff:transfer")));
            when(admins.getAgentResourceList(8L)).thenReturn(List.of(permission("agent:handoff:claim"),
                    permission("agent:conversation:read"), permission("agent:conversation:reply"),
                    permission("agent:presence:write")), List.of());
            assertEquals(200, controller.eligibility(8L, signed("/mall-admin", Instant.now(), path)).getStatusCode().value());
            assertEquals(403, controller.eligibility(8L, signed("/mall-admin", Instant.now(), path)).getStatusCode().value());
            target.setStatus(0);
            assertEquals(403, controller.eligibility(8L, signed("/mall-admin", Instant.now(), path)).getStatusCode().value());
            when(admins.getAgentResourceList(7L)).thenReturn(List.of());
            assertEquals(403, controller.eligibility(8L, signed("/mall-admin", Instant.now(), path)).getStatusCode().value());
        }
    }

    private UmsResource permission(String name) {
        var resource = new UmsResource(); resource.setName(name); resource.setUrl("/mall-agent/v1/admin/**");
        return resource;
    }

    @Test void invalidSignatureAndMemberDomainNeverReachIdentity() throws Exception {
        assertEquals(403, controller.identity(new MockHttpServletRequest("GET", "/agent-bridge/v1/identity")).getStatusCode().value());
        assertEquals(403, controller.identity(signed("/mall-portal", Instant.now())).getStatusCode().value());
        assertEquals(403, controller.identity(signed("/mall-admin", Instant.now().minusSeconds(61))).getStatusCode().value());
        verifyNoInteractions(admins, redis);
    }

    @Test void nonceReplayAndChangedTargetAreRejected() throws Exception {
        var request = signed("/mall-admin", Instant.now());
        when(values.setIfAbsent(anyString(), eq("1"), any(Duration.class))).thenReturn(false);
        assertEquals(403, controller.identity(request).getStatusCode().value());
        request.setQueryString("actor=1");
        assertEquals(403, controller.identity(request).getStatusCode().value());
        verifyNoInteractions(admins);
    }

    @org.junit.jupiter.api.parallel.ResourceLock(org.junit.jupiter.api.parallel.Resources.SYSTEM_PROPERTIES)
    @Test void nonceReplayNullAndRedisFailureRemainForbiddenWithObservationOnOrOff() throws Exception {
        String property = "mall.agent.bridge.observability.enabled", previous = System.getProperty(property);
        try {
            for (String enabled : List.of("false", "true")) {
                System.setProperty(property, enabled);
                doReturn(false, (Boolean) null)
                        .doThrow(new org.springframework.dao.QueryTimeoutException("private diagnostic must not be logged"))
                        .when(values).setIfAbsent(anyString(), eq("1"), any(Duration.class));
                var request = signed("/mall-admin", Instant.now());
                request.addHeader("X-Request-ID", UUID.randomUUID().toString());
                for (int attempt = 0; attempt < 3; attempt++) {
                    var response = controller.identity(request);
                    assertEquals(403, response.getStatusCode().value());
                    assertEquals("BRIDGE_SIGNATURE_INVALID", response.getBody().get("code"));
                }
            }
        } finally {
            if (previous == null) System.clearProperty(property); else System.setProperty(property, previous);
        }
        verify(values, times(6)).setIfAbsent(anyString(), eq("1"), eq(Duration.ofSeconds(125)));
        verifyNoInteractions(admins);
    }

    @Test void disabledStaffAndAnonymousCannotAuthenticate() throws Exception {
        try (MockedStatic<StpUtil> auth = mockStatic(StpUtil.class)) {
            auth.when(StpUtil::isLogin).thenReturn(false);
            assertEquals(401, controller.identity(signed("/mall-admin", Instant.now())).getStatusCode().value());
            auth.when(StpUtil::isLogin).thenReturn(true);
            auth.when(StpUtil::getLoginIdAsLong).thenReturn(7L);
            var admin = new UmsAdmin(); admin.setId(7L); admin.setStatus(0);
            when(admins.getItem(7L)).thenReturn(admin);
            assertEquals(401, controller.identity(signed("/mall-admin", Instant.now())).getStatusCode().value());
            verify(admins, never()).getAgentResourceList(anyLong());
        }
    }

    @Test void readsCurrentDbPermissionsOnEveryRequestWithoutImplicitSuperuser() throws Exception {
        try (MockedStatic<StpUtil> auth = mockStatic(StpUtil.class)) {
            auth.when(StpUtil::isLogin).thenReturn(true);
            auth.when(StpUtil::getLoginIdAsLong).thenReturn(7L);
            var admin = new UmsAdmin(); admin.setId(7L); admin.setStatus(1); admin.setUsername("admin");
            admin.setPassword("private-hash");
            when(admins.getItem(7L)).thenReturn(admin);
            var read = new UmsResource(); read.setName("agent:job:read"); read.setUrl("/mall-agent/v1/admin/jobs/**");
            var unrelated = new UmsResource(); unrelated.setName("agent:job:retry"); unrelated.setUrl("/product/**");
            when(admins.getAgentResourceList(7L)).thenReturn(List.of(read, unrelated), List.of());
            var first = controller.identity(signed("/mall-admin", Instant.now()));
            assertEquals(200, first.getStatusCode().value());
            var data = (Map<?, ?>) first.getBody().get("data");
            assertEquals(List.of("agent:job:read"), data.get("permissions"));
            assertFalse(first.getBody().toString().contains("private-hash"));
            var second = controller.identity(signed("/mall-admin", Instant.now()));
            assertEquals(List.of(), ((Map<?, ?>) second.getBody().get("data")).get("permissions"));
            verify(admins, times(2)).getAgentResourceList(7L);
            verify(admins, times(2)).getItem(7L);
        }
    }
}

