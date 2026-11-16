package com.macro.mall.config;

import cn.dev33.satoken.SaManager;
import cn.dev33.satoken.context.SaTokenContext;
import cn.dev33.satoken.fun.strategy.SaRouteMatchFunction;
import cn.dev33.satoken.exception.NotLoginException;
import cn.dev33.satoken.exception.NotPermissionException;
import cn.dev33.satoken.reactor.filter.SaReactorFilter;
import cn.dev33.satoken.reactor.filter.SaFirewallCheckFilterForReactor;
import cn.dev33.satoken.reactor.spring.SaTokenContextForSpringReactor;
import cn.dev33.satoken.strategy.SaStrategy;
import cn.hutool.json.JSONUtil;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;
import org.springframework.util.AntPathMatcher;
import reactor.core.publisher.Mono;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.*;

class GatewayErrorEncodingTest {
    private SaTokenContext previousContext;
    private SaRouteMatchFunction previousMatcher;
    private SaReactorFilter filter;

    @BeforeEach void setUp() {
        previousContext = SaManager.getSaTokenContext();
        SaManager.setSaTokenContext(new SaTokenContextForSpringReactor());
        previousMatcher = SaStrategy.instance.routeMatcher;
        SaStrategy.instance.routeMatcher = new AntPathMatcher()::match;
        IgnoreUrlsConfig urls = new IgnoreUrlsConfig();
        urls.setUrls(List.of());
        filter = new SaTokenConfig().getSaReactorFilter(urls);
    }

    @AfterEach void restoreContext() {
        SaManager.setSaTokenContext(previousContext);
        SaStrategy.instance.routeMatcher = previousMatcher;
    }

    private void assertError(Throwable error, int code, String message) {
        filter.setAuth(ignored -> { throw (RuntimeException) error; });
        MockServerWebExchange exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/mall-admin/product/list"));
        filter.filter(exchange, ignored -> { fail("Rejected request reached the backend"); return Mono.empty(); })
                .block(Duration.ofSeconds(5));
        String body = exchange.getResponse().getBodyAsString().block(Duration.ofSeconds(5));
        assertNotNull(body);
        var json = JSONUtil.parseObj(body);
        assertEquals(code, json.getInt("code"));
        assertEquals(message, json.getStr("message"));
        assertEquals("*", exchange.getResponse().getHeaders().getFirst("Access-Control-Allow-Origin"));
        assertEquals("no-cache", exchange.getResponse().getHeaders().getFirst("Cache-Control"));
    }

    @Test void unauthorizedMessageIsValidUtf8() {
        assertError(new NotLoginException("admin", NotLoginException.NOT_TOKEN, "not logged in"),
                401, "暂未登录或token已经过期");
    }

    @Test void forbiddenMessageIsValidUtf8() {
        assertError(new NotPermissionException("resource"), 403, "没有相关权限");
    }

    @Test void otherChineseErrorsKeepTheirMessage() {
        assertError(new IllegalArgumentException("请求参数无效"), 500, "请求参数无效");
    }

    @Test void acceptedRequestsStillReachTheBackend() {
        AtomicBoolean reached = new AtomicBoolean();
        filter.setAuth(ignored -> {});
        var exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/mall-admin/product/list"));
        filter.filter(exchange, ignored -> { reached.set(true); return Mono.empty(); }).block(Duration.ofSeconds(5));
        assertTrue(reached.get());
    }

    @Test void agentLoginRejectionHasHttp401AndStandardEnvelope() {
        assertAgentError(new NotLoginException("admin", NotLoginException.NOT_TOKEN, "PRIVATE_LOGIN_DETAIL"), 401, "AUTH_REQUIRED");
    }

    @Test void agentPermissionRejectionHasHttp403AndNoData() {
        assertAgentError(new NotPermissionException("PRIVATE_RESOURCE"), 403, "FORBIDDEN");
    }

    @Test void agentInfrastructureFailureDoesNotLeakRawException() {
        assertAgentError(new IllegalStateException("PRIVATE_DRIVER_DETAIL"), 503, "AUTH_SERVICE_UNAVAILABLE");
    }

    @Test void validAgentRequestIdIsPreservedAndInvalidIdIsReplaced() {
        filter.setAuth(ignored -> { throw new NotPermissionException("agent-route"); });
        for (String requestId : List.of("d67d1fb7-84c2-4029-94eb-07c0ce93e89b", "not-a-request-id")) {
            var exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/mall-agent/internal/framework").header("X-Request-ID", requestId));
            filter.filter(exchange, ignored -> { fail("Denied request forwarded"); return Mono.empty(); }).block(Duration.ofSeconds(5));
            String returned = JSONUtil.parseObj(exchange.getResponse().getBodyAsString().block()).getStr("request_id");
            assertEquals(returned, exchange.getResponse().getHeaders().getFirst("X-Request-ID"));
            assertDoesNotThrow(() -> java.util.UUID.fromString(returned));
            if (requestId.startsWith("d67d")) assertEquals(requestId, returned);
            else assertNotEquals(requestId, returned);
        }
    }

    private void assertAgentError(RuntimeException error, int status, String code) {
        filter.setAuth(ignored -> { throw error; });
        var exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/mall-agent/v1/admin/model-usage/unknown"));
        filter.filter(exchange, ignored -> { fail("Denied request forwarded"); return Mono.empty(); }).block(Duration.ofSeconds(5));
        assertNotNull(exchange.getResponse().getStatusCode());
        assertEquals(status, exchange.getResponse().getStatusCode().value());
        String body = exchange.getResponse().getBodyAsString().block();
        var json = JSONUtil.parseObj(body);
        assertEquals(code, json.getStr("code"));
        assertTrue(json.containsKey("data"));
        assertTrue(json.isNull("data"));
        assertDoesNotThrow(() -> java.util.UUID.fromString(json.getStr("request_id")));
        assertEquals("no-store", exchange.getResponse().getHeaders().getFirst("Cache-Control"));
        assertFalse(body.contains("PRIVATE_"));
        assertFalse(body.contains("\ufffd"));
    }

    @Test void illegalPathMessageIsValidUtf8AndStillBlocked() {
        var exchange = MockServerWebExchange.from(MockServerHttpRequest.get("/mall-admin/product;bad/list"));
        new SaFirewallCheckFilterForReactor().filter(exchange, ignored -> {
            fail("Illegal path reached the backend");
            return Mono.empty();
        }).block(Duration.ofSeconds(5));
        String body = exchange.getResponse().getBodyAsString().block(Duration.ofSeconds(5));
        assertNotNull(body);
        assertTrue(body.contains("非法"), body);
        assertFalse(body.contains("\ufffd"), body);
    }
}
