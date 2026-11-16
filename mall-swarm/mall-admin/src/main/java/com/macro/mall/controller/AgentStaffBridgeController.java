package com.macro.mall.controller;

import cn.dev33.satoken.stp.StpUtil;
import com.macro.mall.model.UmsAdmin;
import com.macro.mall.service.UmsAdminService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.*;

/** Fresh DB authorization. A login-session permission snapshot is not an authorization source here. */
@RestController
@RequestMapping("/agent-bridge/v1")
public class AgentStaffBridgeController {
    private final UmsAdminService admins;
    private final StringRedisTemplate redis;
    private final String secret;
    @org.springframework.beans.factory.annotation.Autowired
    private com.macro.mall.service.AgentStaffBusiness business;
    @org.springframework.beans.factory.annotation.Autowired
    private com.fasterxml.jackson.databind.ObjectMapper json;

    public AgentStaffBridgeController(UmsAdminService admins, StringRedisTemplate redis,
            @Value("${MALL_AGENT_STAFF_BRIDGE_SECRET:}") String secret) {
        this.admins = admins; this.redis = redis; this.secret = secret;
    }

    @GetMapping("/identity")
    public ResponseEntity<Map<String, Object>> identity(HttpServletRequest request) {
        if (!signed(request)) return reply(request, HttpStatus.FORBIDDEN, "BRIDGE_SIGNATURE_INVALID", null);
        if (!StpUtil.isLogin()) return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        UmsAdmin admin = admins.getItem(StpUtil.getLoginIdAsLong());
        if (admin == null || !Integer.valueOf(1).equals(admin.getStatus()))
            return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        List<String> permissions = admins.getAgentResourceList(admin.getId()).stream()
                .filter(r -> r.getName() != null && r.getName().startsWith("agent:")
                        && r.getUrl() != null && r.getUrl().startsWith("/mall-agent/v1/admin/"))
                .map(r -> r.getName()).distinct().sorted().toList();
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("id", admin.getId().toString()); data.put("kind", "STAFF");
        data.put("display_name", admin.getNickName() == null ? admin.getUsername() : admin.getNickName());
        data.put("permissions", permissions); data.put("token_valid_until", null);
        return reply(request, HttpStatus.OK, "OK", data);
    }

    @GetMapping("/staff/{staffId}/eligibility")
    public ResponseEntity<Map<String, Object>> eligibility(@PathVariable Long staffId, HttpServletRequest request) {
        if (!signed(request)) return reply(request, HttpStatus.FORBIDDEN, "BRIDGE_SIGNATURE_INVALID", null);
        if (!StpUtil.isLogin()) return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        UmsAdmin caller = admins.getItem(StpUtil.getLoginIdAsLong());
        if (caller == null || !Integer.valueOf(1).equals(caller.getStatus()))
            return reply(request, HttpStatus.UNAUTHORIZED, "AUTH_EXPIRED", null);
        if (!agentPermissions(caller.getId()).contains("agent:handoff:transfer"))
            return reply(request, HttpStatus.FORBIDDEN, "FORBIDDEN", null);
        UmsAdmin target = admins.getItem(staffId);
        Set<String> required = Set.of("agent:handoff:claim", "agent:conversation:read",
                "agent:conversation:reply", "agent:presence:write");
        if (target == null || !Integer.valueOf(1).equals(target.getStatus())
                || !agentPermissions(staffId).containsAll(required))
            return reply(request, HttpStatus.FORBIDDEN, "STAFF_NOT_ELIGIBLE", null);
        return reply(request, HttpStatus.OK, "OK", Map.of("id", staffId.toString(), "eligible", true));
    }

    Set<String> agentPermissions(Long id) {
        Set<String> result = new HashSet<>();
        admins.getAgentResourceList(id).stream()
                .filter(r -> r.getName() != null && r.getName().startsWith("agent:")
                        && r.getUrl() != null && r.getUrl().startsWith("/mall-agent/v1/admin/"))
                .forEach(r -> result.add(r.getName()));
        return result;
    }

    @PostMapping("/business-context")
    public ResponseEntity<Map<String,Object>> businessContext(@RequestBody byte[] raw, HttpServletRequest request) {
        if(raw.length>10000 || !signed(request,raw))return reply(request,HttpStatus.FORBIDDEN,"BRIDGE_SIGNATURE_INVALID",null);
        if(!StpUtil.isLogin())return reply(request,HttpStatus.UNAUTHORIZED,"AUTH_EXPIRED",null);
        long id=StpUtil.getLoginIdAsLong();
        UmsAdmin caller=admins.getItem(id);
        if(caller==null || !Integer.valueOf(1).equals(caller.getStatus()))return reply(request,HttpStatus.UNAUTHORIZED,"AUTH_EXPIRED",null);
        if(!agentPermissions(id).containsAll(Set.of("agent:business:read","agent:conversation:read")))return reply(request,HttpStatus.FORBIDDEN,"FORBIDDEN",null);
        try { return reply(request,HttpStatus.OK,"OK",business.read(id,json.readTree(raw))); }
        catch(com.macro.mall.service.AgentStaffBusiness.Rejected e) { return reply(request,HttpStatus.valueOf(e.status),"BUSINESS_CONTEXT_REJECTED",null); }
        catch(java.io.IOException e) { return reply(request,HttpStatus.UNPROCESSABLE_ENTITY,"VALIDATION_ERROR",null); }
    }

    private boolean signed(HttpServletRequest request) {
        return signed(request,new byte[0]);
    }
    boolean signed(HttpServletRequest request, byte[] raw) {
        if (secret.isBlank() || !Set.of("GET","POST","PUT","PATCH").contains(request.getMethod())) return false;
        String timestamp = request.getHeader("X-Agent-Timestamp"), nonce = request.getHeader("X-Agent-Nonce");
        String signature = request.getHeader("X-Agent-Signature");
        if (timestamp == null || nonce == null || signature == null || !signature.startsWith("v1:")) return false;
        try {
            if (!UUID.fromString(nonce).toString().equals(nonce)) return false;
            if (Duration.between(Instant.parse(timestamp), Instant.now()).abs().compareTo(Duration.ofSeconds(60)) > 0) return false;
            String target = "/mall-admin" + request.getRequestURI();
            if (request.getQueryString() != null) target += "?" + request.getQueryString();
            String hash = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(raw));
            String canonical = String.join("\n", request.getMethod(), target, hash, timestamp, nonce);
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            if (!MessageDigest.isEqual(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8)),
                    Base64.getUrlDecoder().decode(signature.substring(3)))) return false;
            return Boolean.TRUE.equals(redis.opsForValue().setIfAbsent("mall-agent:staff:nonce:v1:" + nonce,
                    "1", Duration.ofSeconds(125)));
        } catch (Exception ex) { return false; }
    }

    private ResponseEntity<Map<String, Object>> reply(HttpServletRequest request, HttpStatus status,
            String code, Object data) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", code); body.put("message", code.equals("OK") ? "成功" : "员工鉴权未通过");
        body.put("data", data);
        String id;
        try { id = UUID.fromString(request.getHeader("X-Request-ID")).toString(); }
        catch (RuntimeException ex) { id = UUID.randomUUID().toString(); }
        body.put("request_id", id);
        return ResponseEntity.status(status).body(body);
    }
}
