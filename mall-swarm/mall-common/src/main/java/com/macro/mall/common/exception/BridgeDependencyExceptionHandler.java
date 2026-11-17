package com.macro.mall.common.exception;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/** Only nonce dependency failures use this response; invalid signatures remain forbidden. */
@RestControllerAdvice
public class BridgeDependencyExceptionHandler {
    @ExceptionHandler(BridgeNonceUnavailableException.class)
    public ResponseEntity<Map<String, Object>> nonceUnavailable(
            BridgeNonceUnavailableException ignored, HttpServletRequest request) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", "BRIDGE_NONCE_UNAVAILABLE");
        body.put("message", "商城签名校验依赖暂不可用，请稍后核对");
        body.put("data", null);
        body.put("request_id", requestId(request.getHeader("X-Request-ID")));
        return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(body);
    }

    private String requestId(String supplied) {
        if (supplied != null && supplied.length() == 36) {
            try {
                String canonical = UUID.fromString(supplied).toString();
                if (canonical.equalsIgnoreCase(supplied)) return canonical;
            } catch (IllegalArgumentException ignored) {
                // A malformed correlation header is never reflected in the response.
            }
        }
        return UUID.randomUUID().toString();
    }
}
