package com.macro.mall.portal.controller;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.common.exception.BridgeDependencyExceptionHandler;
import com.macro.mall.common.exception.GlobalExceptionHandler;
import com.macro.mall.common.log.BridgeSignatureObservation;
import com.macro.mall.order.AgentAfterSaleStore;
import com.macro.mall.portal.service.AgentCartSecurity;
import com.macro.mall.portal.service.AgentCartService;
import com.macro.mall.portal.service.AgentOrderLifecycle;
import com.macro.mall.portal.service.AgentOrderQueries;
import com.macro.mall.portal.service.AgentOrderService;
import com.macro.mall.portal.service.AgentProductCatalog;
import com.macro.mall.portal.service.PmsPortalProductService;
import com.macro.mall.portal.service.UmsMemberService;
import com.macro.mall.portal.util.StpMemberUtil;
import io.lettuce.core.RedisCommandTimeoutException;
import io.lettuce.core.RedisConnectionException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Base64;
import java.util.HexFormat;
import java.util.UUID;
import java.util.stream.Stream;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.parallel.ResourceLock;
import org.junit.jupiter.api.parallel.Resources;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.dao.QueryTimeoutException;
import org.springframework.data.redis.RedisConnectionFailureException;
import org.springframework.data.redis.RedisSystemException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockStatic;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.verifyNoMoreInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** Standalone HTTP tests: nonce dependencies fail closed with HTTP 503; explicit replay remains forbidden. */
@ResourceLock(Resources.SYSTEM_PROPERTIES)
class AgentNonceFailureHttpTest {
    private static final String SECRET = "characterization-only-bridge-secret-32-bytes";
    private static final String PRIVATE_MARKER = "synthetic-private-redis-timeout-marker";
    private static final String OPERATION = "25af311a-d1af-4ed4-84cf-c69a86810eed";

    private final ObjectMapper json = new ObjectMapper();
    private final StringRedisTemplate redis = mock(StringRedisTemplate.class);
    @SuppressWarnings("unchecked")
    private final ValueOperations<String, String> values = mock(ValueOperations.class);
    private final UmsMemberService members = mock(UmsMemberService.class);
    private final PmsPortalProductService products = mock(PmsPortalProductService.class);
    private final AgentProductCatalog catalog = mock(AgentProductCatalog.class);
    private final AgentCartService carts = mock(AgentCartService.class);
    private final AgentOrderService orderService = mock(AgentOrderService.class);
    private final AgentOrderLifecycle lifecycle = mock(AgentOrderLifecycle.class);
    private final AgentOrderQueries queries = mock(AgentOrderQueries.class);
    private final AgentAfterSaleStore store = mock(AgentAfterSaleStore.class);
    private MockMvc mvc;

    @BeforeEach
    void setup() {
        when(redis.opsForValue()).thenReturn(values);

        var memberController = new AgentMemberBridgeController(members, products, redis, SECRET);
        ReflectionTestUtils.setField(memberController, "catalog", catalog);
        ReflectionTestUtils.setField(memberController, "cartActions", carts);
        var security = new AgentCartSecurity(SECRET,
                "characterization-only-confirm-secret-32-bytes",
                "characterization-only-reconcile-secret-32-bytes", redis, json);
        var cartController = new AgentCartBridgeController(
                orderService, carts, security, members, json, true, true);
        ReflectionTestUtils.setField(cartController, "lifecycle", lifecycle);
        var ordersController = new AgentOrdersBridgeController(security, members, queries, store);

        mvc = MockMvcBuilders.standaloneSetup(memberController, cartController, ordersController)
                .setControllerAdvice(new GlobalExceptionHandler(), new BridgeDependencyExceptionHandler())
                .build();
    }

    private record Route(String method, String path, long ttlSeconds) { }

    static Stream<Route> routes() {
        return Stream.of(
                new Route("GET", "/agent-bridge/v1/identity", 125L),
                new Route("GET", "/agent-bridge/v1/products", 125L),
                new Route("GET", "/agent-bridge/v1/products/1", 125L),
                new Route("POST", "/agent-bridge/v1/checkout-options", 300L),
                new Route("GET", "/agent-bridge/v1/cart", 300L),
                new Route("POST", "/agent-bridge/v1/quotes", 300L),
                new Route("POST", "/agent-bridge/v1/operations", 300L),
                new Route("GET", "/agent-bridge/v1/operations/" + OPERATION, 300L),
                new Route("POST", "/agent-bridge/v1/operations/" + OPERATION + "/reconcile", 300L),
                new Route("GET", "/agent-bridge/v1/orders", 300L),
                new Route("GET", "/agent-bridge/v1/orders/1", 300L),
                new Route("GET", "/agent-bridge/v1/orders/1/logistics", 300L),
                new Route("GET", "/agent-bridge/v1/orders/1/returns", 300L),
                new Route("GET", "/agent-bridge/v1/orders/1/items/1/return-eligibility", 300L)
        );
    }


    private enum NonceScenario {
        TIMEOUT, CONNECTION, SYSTEM, REPLAY, UNKNOWN;

        boolean unavailable() {
            return this != REPLAY && this != UNKNOWN;
        }
    }

    static Stream<Arguments> cases() {
        return routes().flatMap(route -> Stream.of(NonceScenario.values())
                .flatMap(scenario -> Stream.of(false, true)
                        .map(observed -> Arguments.of(route.method(), route.path(), route.ttlSeconds(),
                                scenario, observed))));
    }

    static Stream<Arguments> requestIds() {
        return Stream.of(
                Arguments.of("88d4c202-4f3e-4cba-a96e-4fccb1d17ff4", true),
                Arguments.of("A1B2C3D4-1111-4222-8333-0123456789AB", true),
                Arguments.of("not-a-uuid", false),
                Arguments.of("1-1-1-1-1", false),
                Arguments.of(null, false)
        );
    }

    @ParameterizedTest(name = "{index}: {0} {1} {3} observation={4}")
    @MethodSource("cases")
    void dependencyFailureIsUnavailableWhileReplayAndUnknownRemainForbidden(
            String method, String path, long ttlSeconds, NonceScenario scenario, boolean observed)
            throws Exception {
        exercise(method, path, ttlSeconds, scenario, observed, UUID.randomUUID().toString(), true);
    }

    @ParameterizedTest(name = "{index}: dependency response request ID={0}")
    @MethodSource("requestIds")
    void unavailableResponsePreservesCanonicalRequestIdOrGeneratesOne(String requestId, boolean valid)
            throws Exception {
        exercise("GET", "/agent-bridge/v1/identity", 125, NonceScenario.TIMEOUT, true, requestId, valid);
    }

    private void exercise(String method, String path, long ttlSeconds, NonceScenario scenario,
                          boolean observed, String requestId, boolean validRequestId) throws Exception {
        configureNonceResult(scenario);
        byte[] body = method.equals("GET") ? new byte[0] : "{}".getBytes(StandardCharsets.UTF_8);
        String nonce = UUID.randomUUID().toString();
        MockHttpServletRequestBuilder signed = signedRequest(method, path, body, body, nonce, requestId);

        try (var observation = new ObservationCapture(observed);
             var login = mockStatic(StpMemberUtil.class)) {
            var result = mvc.perform(signed)
                    .andExpect(status().is(scenario.unavailable() ? 503 : 403)).andReturn();
            String response = result.getResponse().getContentAsString(StandardCharsets.UTF_8);
            JsonNode payload = json.readTree(response);
            assertEquals(scenario.unavailable() ? "BRIDGE_NONCE_UNAVAILABLE" : "BRIDGE_SIGNATURE_INVALID",
                    payload.path("code").asText());
            if (scenario.unavailable()) {
                assertEquals("商城签名校验依赖暂不可用，请稍后核对", payload.path("message").asText());
            }
            assertTrue(payload.has("data"));
            assertTrue(payload.get("data").isNull());
            String responseId = payload.path("request_id").asText();
            assertEquals(UUID.fromString(responseId).toString(), responseId);
            if (scenario.unavailable() && validRequestId) {
                assertEquals(UUID.fromString(requestId).toString(), responseId);
            } else if (scenario.unavailable() && "1-1-1-1-1".equals(requestId)) {
                assertFalse(UUID.fromString(requestId).toString().equals(responseId));
            }
            assertNoPrivateData(response, nonce);
            observation.assertResult(observed, scenario, ttlSeconds == 125 ? "MEMBER" : "CART", nonce);
            login.verifyNoInteractions();
        }

        verify(redis, times(1)).opsForValue();
        verify(values, times(1)).setIfAbsent("mall-agent:bridge:nonce:v1:" + nonce,
                "1", Duration.ofSeconds(ttlSeconds));
        verifyNoMoreInteractions(redis, values);
        assertNoBusinessInteractions();
    }

    @org.junit.jupiter.api.Test
    void tamperedBodyIsRejectedBeforeNonceOrBusiness() throws Exception {
        byte[] signedBody = "{}".getBytes(StandardCharsets.UTF_8);
        byte[] actualBody = "{\"tampered\":true}".getBytes(StandardCharsets.UTF_8);
        String nonce = UUID.randomUUID().toString();
        try (var observation = new ObservationCapture(false);
             var login = mockStatic(StpMemberUtil.class)) {
            var result = mvc.perform(signedRequest("POST", "/agent-bridge/v1/quotes", signedBody,
                    actualBody, nonce, UUID.randomUUID().toString()))
                    .andExpect(status().isForbidden()).andReturn();
            JsonNode payload = json.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8));
            assertEquals("BRIDGE_SIGNATURE_INVALID", payload.path("code").asText());
            assertTrue(payload.has("data") && payload.get("data").isNull());
            login.verifyNoInteractions();
        }
        verifyNoInteractions(redis, values);
        assertNoBusinessInteractions();
    }


    static Stream<Arguments> malformedNonceRoutes() {
        return Stream.of(Arguments.of("GET", "/agent-bridge/v1/identity"),
                Arguments.of("POST", "/agent-bridge/v1/quotes"));
    }

    @ParameterizedTest(name = "{index}: malformed nonce {0} {1}")
    @MethodSource("malformedNonceRoutes")
    void localValidationFailureRemainsForbiddenWithoutClaimingNonce(String method, String path)
            throws Exception {
        byte[] body = method.equals("GET") ? new byte[0] : "{}".getBytes(StandardCharsets.UTF_8);
        try (var observation = new ObservationCapture(true);
             var login = mockStatic(StpMemberUtil.class)) {
            var result = mvc.perform(signedRequest(method, path, body, body,
                    "not-a-uuid", UUID.randomUUID().toString()))
                    .andExpect(status().isForbidden()).andReturn();
            JsonNode payload = json.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8));
            assertEquals("BRIDGE_SIGNATURE_INVALID", payload.path("code").asText());
            assertTrue(payload.has("data") && payload.get("data").isNull());
            login.verifyNoInteractions();
        }
        verifyNoInteractions(redis, values);
        assertNoBusinessInteractions();
    }

    private void configureNonceResult(NonceScenario scenario) {
        if (!scenario.unavailable()) {
            when(values.setIfAbsent(anyString(), eq("1"), any(Duration.class)))
                    .thenReturn(scenario == NonceScenario.REPLAY ? Boolean.FALSE : null);
            return;
        }
        DataAccessException failure = switch (scenario) {
            case TIMEOUT -> new QueryTimeoutException(PRIVATE_MARKER,
                    new RedisCommandTimeoutException(PRIVATE_MARKER));
            case CONNECTION -> new RedisConnectionFailureException(PRIVATE_MARKER,
                    new RedisConnectionException(PRIVATE_MARKER));
            case SYSTEM -> new RedisSystemException(PRIVATE_MARKER,
                    new IllegalArgumentException(PRIVATE_MARKER));
            default -> throw new IllegalArgumentException("Only dependency failures reach this branch");
        };
        when(values.setIfAbsent(anyString(), eq("1"), any(Duration.class))).thenThrow(failure);
    }

    private MockHttpServletRequestBuilder signedRequest(String method, String path, byte[] signedBody,
                                                        byte[] actualBody, String nonce, String requestId)
            throws Exception {
        String timestamp = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'")
                .withZone(ZoneOffset.UTC).format(Instant.now());
        MockHttpServletRequestBuilder builder = request(HttpMethod.valueOf(method), path)
                .contentType(MediaType.APPLICATION_JSON)
                .content(actualBody)
                .header("Authorization", "Bearer characterization-private-token")
                .header("X-Agent-Timestamp", timestamp)
                .header("X-Agent-Nonce", nonce)
                .header("X-Agent-Signature", sign(method, "/mall-portal" + path,
                        signedBody, timestamp, nonce));
        if (requestId != null) builder.header("X-Request-ID", requestId);
        return builder;
    }

    private void assertNoBusinessInteractions() {
        verifyNoInteractions(members, products, catalog, carts, orderService, lifecycle, queries, store);
    }

    private static void assertNoPrivateData(String response, String nonce) {
        assertFalse(response.contains(PRIVATE_MARKER));
        assertFalse(response.contains(SECRET));
        assertFalse(response.contains("characterization-private-token"));
        assertFalse(response.contains(nonce));
    }

    private static String sign(String method, String target, byte[] body,
                               String timestamp, String nonce) throws Exception {
        String bodyHash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(body));
        String canonical = String.join("\n", method, target, bodyHash, timestamp, nonce);
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(SECRET.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        return "v1:" + Base64.getUrlEncoder().withoutPadding()
                .encodeToString(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8)));
    }

    private static final class ObservationCapture implements AutoCloseable {
        private final String previous = System.getProperty(BridgeSignatureObservation.ENABLED_PROPERTY);
        private final Logger logger = (Logger) LoggerFactory.getLogger(BridgeSignatureObservation.class);
        private final Level level = logger.getLevel();
        private final boolean additive = logger.isAdditive();
        private final ListAppender<ILoggingEvent> events = new ListAppender<>();

        ObservationCapture(boolean enabled) {
            System.setProperty(BridgeSignatureObservation.ENABLED_PROPERTY, Boolean.toString(enabled));
            events.start();
            logger.addAppender(events);
            logger.setLevel(Level.INFO);
            logger.setAdditive(false);
        }

        void assertResult(boolean observed, NonceScenario scenario, String guard, String nonce) {
            assertEquals(observed ? 1 : 0, events.list.size());
            if (!observed) return;
            ILoggingEvent event = events.list.get(0);
            String log = event.getFormattedMessage();
            String outcome = scenario.unavailable() ? "NONCE_STORE_ERROR"
                    : scenario == NonceScenario.REPLAY ? "NONCE_REPLAY" : "NONCE_RESULT_UNKNOWN";
            assertTrue(log.contains("guard=" + guard + " "));
            assertTrue(log.contains("phase=NONCE outcome=" + outcome + " accepted=false"));
            String errorTypes = switch (scenario) {
                case TIMEOUT -> "error_type=QUERY_TIMEOUT root_type=REDIS_COMMAND_TIMEOUT";
                case CONNECTION -> "error_type=REDIS_CONNECTION_FAILURE root_type=REDIS_CONNECTION";
                case SYSTEM -> "error_type=REDIS_SYSTEM root_type=ILLEGAL_ARGUMENT";
                default -> "error_type=NONE root_type=NONE";
            };
            assertTrue(log.contains(errorTypes));
            assertNoPrivateData(log, nonce);
            assertFalse(log.contains("BridgeNonceUnavailableException"));
            assertTrue(event.getThrowableProxy() == null);
            if (event.getArgumentArray() != null) {
                for (Object argument : event.getArgumentArray()) assertFalse(argument instanceof Throwable);
            }
        }

        @Override
        public void close() {
            if (previous == null) System.clearProperty(BridgeSignatureObservation.ENABLED_PROPERTY);
            else System.setProperty(BridgeSignatureObservation.ENABLED_PROPERTY, previous);
            logger.detachAppender(events);
            events.stop();
            logger.setLevel(level);
            logger.setAdditive(additive);
        }
    }
}
