package com.macro.mall.portal.controller;

import com.macro.mall.common.log.BridgeSignatureObservation;
import com.macro.mall.common.exception.BridgeNonceUnavailableException;
import com.macro.mall.common.log.BridgeSignatureObservation.Guard;
import com.macro.mall.common.log.BridgeSignatureObservation.Phase;
import com.macro.mall.common.log.BridgeSignatureObservation.Outcome;
import com.macro.mall.model.PmsProduct;
import com.macro.mall.model.UmsMember;
import com.macro.mall.portal.service.PmsPortalProductService;
import com.macro.mall.portal.service.UmsMemberService;
import com.macro.mall.portal.service.AgentProductCatalog;
import com.macro.mall.portal.util.StpMemberUtil;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Collections;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/** Narrow, signed, read-only member bridge. The gateway strips /mall-portal before forwarding. */
@RestController
@RequestMapping("/agent-bridge/v1")
public class AgentMemberBridgeController {
    private final UmsMemberService members;
    private final PmsPortalProductService products;
    private final StringRedisTemplate redis;
    private final String secret;
    @org.springframework.beans.factory.annotation.Autowired
    private com.macro.mall.portal.service.AgentCartService cartActions;
    @Value("${MALL_AGENT_ACTIONS_ENABLED:false}")
    private boolean actionsEnabled;
    @org.springframework.beans.factory.annotation.Autowired
    private AgentProductCatalog catalog;

    public AgentMemberBridgeController(UmsMemberService members, PmsPortalProductService products,
                                       StringRedisTemplate redis,
                                       @Value("${MALL_AGENT_BRIDGE_SECRET:}") String secret) {
        this.members = members;
        this.products = products;
        this.redis = redis;
        this.secret = secret;
    }

    @GetMapping("/identity")
    public ResponseEntity<Map<String, Object>> identity(HttpServletRequest request) {
        if (!signed(request)) return reply(request, HttpStatus.FORBIDDEN, "BRIDGE_SIGNATURE_INVALID", null);
        if (!StpMemberUtil.isLogin()) return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        UmsMember member = members.getCurrentMember();
        if (member == null || !Integer.valueOf(1).equals(member.getStatus())) {
            return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        }
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("id", member.getId().toString());
        data.put("kind", "MEMBER");
        String name = member.getNickname() != null ? member.getNickname() : member.getUsername();
        data.put("display_name", name == null ? "会员" : name.substring(0, Math.min(80, name.length())));
        data.put("permissions", Collections.emptyList());
        data.put("token_valid_until", null);
        return reply(request, HttpStatus.OK, "OK", data);
    }

    @GetMapping("/products")
    public ResponseEntity<Map<String, Object>> products(HttpServletRequest request,
            @RequestParam(defaultValue = "5") int limit, @RequestParam(required = false) String query) {
        if (!signed(request)) return reply(request, HttpStatus.FORBIDDEN, "BRIDGE_SIGNATURE_INVALID", null);
        if (!StpMemberUtil.isLogin()) return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        UmsMember member = members.getCurrentMember();
        if (member == null || !Integer.valueOf(1).equals(member.getStatus())) {
            return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        }
        if (request.getParameterMap().values().stream().anyMatch(values -> values.length != 1)) {
            return reply(request, HttpStatus.BAD_REQUEST, "VALIDATION_ERROR", null);
        }
        Map<String, String> filters = new LinkedHashMap<>();
        request.getParameterMap().forEach((name, values) -> filters.put(name, values[0]));
        filters.put("limit", Integer.toString(limit));
        if (query != null) filters.put("query", query);
        try { return reply(request, HttpStatus.OK, "OK", catalog.search(member.getId(), filters)); }
        catch (AgentProductCatalog.Rejected error) {
            return reply(request, HttpStatus.valueOf(error.status), error.code, null);
        }
    }

    @GetMapping("/products/{productId}")
    public ResponseEntity<Map<String, Object>> product(HttpServletRequest request, @PathVariable String productId) {
        if (!signed(request)) return reply(request, HttpStatus.FORBIDDEN, "BRIDGE_SIGNATURE_INVALID", null);
        if (!StpMemberUtil.isLogin()) return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        UmsMember member = members.getCurrentMember();
        if (member == null || !Integer.valueOf(1).equals(member.getStatus())) {
            return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        }
        try {
            if (!productId.matches("[1-9][0-9]{0,18}") || !request.getParameterMap().isEmpty()) {
                return reply(request, HttpStatus.BAD_REQUEST, "VALIDATION_ERROR", null);
            }
            return reply(request, HttpStatus.OK, "OK", catalog.detail(Long.parseLong(productId)));
        } catch (NumberFormatException error) {
            return reply(request, HttpStatus.BAD_REQUEST, "VALIDATION_ERROR", null);
        } catch (AgentProductCatalog.Rejected error) {
            return reply(request, HttpStatus.valueOf(error.status), error.code, null);
        }
    }

    private Map<String, Object> view(PmsProduct p) {
        return Map.of("id", p.getId().toString(), "name", p.getName(), "brand", p.getBrandName() == null ? "" : p.getBrandName(),
                "category_id", p.getProductCategoryId().toString(), "price_from", p.getPrice().setScale(2, RoundingMode.HALF_UP).toPlainString(),
                "image_url", p.getPic() == null ? "" : p.getPic(), "detail_path", "/pages/product/product?id=" + p.getId(),
                "sku_options", actionsEnabled ? cartActions.skus(p.getId()) : Collections.emptyList(), "facts", Collections.emptyList(), "checked_at", Instant.now().toString());
    }

    private boolean signed(HttpServletRequest request) {
        try (var observation = BridgeSignatureObservation.start(request, Guard.MEMBER)) {
            observation.phase(Phase.INPUT);
            String timestamp = request.getHeader("X-Agent-Timestamp");
            String nonce = request.getHeader("X-Agent-Nonce");
            String signature = request.getHeader("X-Agent-Signature");
            if (secret.length() < 32 || timestamp == null || !timestamp.matches("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z")
                    || nonce == null || signature == null || !signature.matches("v1:[A-Za-z0-9_-]{43}")) return observation.result(false, Outcome.INPUT_REJECTED);
            try {
                if (!UUID.fromString(nonce).toString().equals(nonce)) return observation.result(false, Outcome.INPUT_REJECTED);
                if (Duration.between(Instant.parse(timestamp), Instant.now()).abs().compareTo(Duration.ofSeconds(60)) > 0) return observation.result(false, Outcome.INPUT_REJECTED);
                observation.phase(Phase.MAC);
                String target = "/mall-portal" + request.getRequestURI();
                if (request.getQueryString() != null) target += "?" + request.getQueryString();
                String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(new byte[0]));
                String canonical = String.join("\n", request.getMethod(), target, hash, timestamp, nonce);
                Mac mac = Mac.getInstance("HmacSHA256");
                mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
                byte[] actual = Base64.getUrlDecoder().decode(signature.substring(3));
                if (!MessageDigest.isEqual(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8)), actual)) return observation.result(false, Outcome.MAC_MISMATCH);
                observation.phase(Phase.NONCE);
                Boolean nonceResult;
                try {
                    nonceResult = redis.opsForValue().setIfAbsent("mall-agent:bridge:nonce:v1:" + nonce,
                            "1", Duration.ofSeconds(125));
                } catch (DataAccessException ex) {
                    observation.error(ex);
                    throw new BridgeNonceUnavailableException(ex);
                }
                boolean accepted = Boolean.TRUE.equals(nonceResult);
                return observation.result(accepted, accepted ? Outcome.ACCEPTED
                        : Boolean.FALSE.equals(nonceResult) ? Outcome.NONCE_REPLAY : Outcome.NONCE_RESULT_UNKNOWN);
            } catch (BridgeNonceUnavailableException ex) {
                throw ex;
            } catch (Exception ex) {
                observation.error(ex);
                return false;
            }
        }
    }

    private ResponseEntity<Map<String, Object>> reply(HttpServletRequest request, HttpStatus status, String code, Object data) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", code);
        body.put("message", code.equals("OK") ? "成功" : "商城桥接请求未通过校验");
        body.put("data", data);
        String id;
        try { id = UUID.fromString(request.getHeader("X-Request-ID")).toString(); }
        catch (RuntimeException ex) { id = UUID.randomUUID().toString(); }
        body.put("request_id", id);
        return ResponseEntity.status(status).body(body);
    }
}
