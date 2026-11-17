package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.mock.web.MockHttpServletRequest;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class AgentCartSecurityTest {
    private final String bridge="test-bridge-secret-0123456789-abcdef",confirm="test-confirm-secret-0123456789-abcdef",reconcile="test-reconcile-secret-0123456789-abcdef";
    private final ObjectMapper json=new ObjectMapper();
    @SuppressWarnings("unchecked")
    @Test void signatureBindsRawBodyAndTargetAndRejectsNonceReplay()throws Exception{
        var redis=mock(StringRedisTemplate.class);ValueOperations<String,String> values=mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(values);when(values.setIfAbsent(anyString(),eq("1"),any(Duration.class))).thenReturn(true,false);
        var verifier=new AgentCartSecurity(bridge,confirm,reconcile,redis,json);byte[] body="{\"商品\":\"中文😀\"}".getBytes(StandardCharsets.UTF_8);
        var request=new MockHttpServletRequest("POST","/agent-bridge/v1/quotes");String timestamp=java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC).format(Instant.now()),nonce=UUID.randomUUID().toString();
        String payload=String.join("\n","POST","/mall-portal/agent-bridge/v1/quotes",AgentCartSecurity.sha(body),timestamp,nonce);
        request.addHeader("X-Agent-Timestamp",timestamp);request.addHeader("X-Agent-Nonce",nonce);request.addHeader("X-Agent-Signature","v1:"+Base64.getUrlEncoder().withoutPadding().encodeToString(AgentCartSecurity.hmac(bridge,payload.getBytes(StandardCharsets.UTF_8))));
        assertFalse(verifier.signed(request,new byte[0]));assertTrue(verifier.signed(request,body));assertFalse(verifier.signed(request,body));
        request.setRequestURI("/agent-bridge/v1/operations");assertFalse(verifier.signed(request,body));
    }
    @SuppressWarnings("unchecked")
    @org.junit.jupiter.api.parallel.ResourceLock(org.junit.jupiter.api.parallel.Resources.SYSTEM_PROPERTIES)
    @Test void nonceReplayNullAndRedisFailureRemainDeniedWithObservationOnOrOff()throws Exception{
        String property="mall.agent.bridge.observability.enabled",previous=System.getProperty(property);
        var redis=mock(StringRedisTemplate.class);ValueOperations<String,String> values=mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(values);
        var verifier=new AgentCartSecurity(bridge,confirm,reconcile,redis,json);
        try {
            for(String enabled:List.of("false","true")) {
                System.setProperty(property,enabled);
                doReturn(false,(Boolean)null).doThrow(new org.springframework.dao.QueryTimeoutException("private diagnostic must not be logged"))
                        .when(values).setIfAbsent(anyString(),eq("1"),any(Duration.class));
                var request=new MockHttpServletRequest("GET","/agent-bridge/v1/orders");
                String timestamp=java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC).format(Instant.now()),nonce=UUID.randomUUID().toString();
                String payload=String.join("\n","GET","/mall-portal/agent-bridge/v1/orders",AgentCartSecurity.sha(new byte[0]),timestamp,nonce);
                request.addHeader("X-Agent-Timestamp",timestamp);request.addHeader("X-Agent-Nonce",nonce);
                request.addHeader("X-Request-ID",UUID.randomUUID().toString());
                request.addHeader("X-Agent-Signature","v1:"+Base64.getUrlEncoder().withoutPadding().encodeToString(AgentCartSecurity.hmac(bridge,payload.getBytes(StandardCharsets.UTF_8))));
                for(int attempt=0;attempt<3;attempt++)assertFalse(verifier.signed(request,new byte[0]));
            }
        } finally {
            if(previous==null)System.clearProperty(property);else System.setProperty(property,previous);
        }
        verify(values,times(6)).setIfAbsent(anyString(),eq("1"),eq(Duration.ofSeconds(300)));
    }
    @Test void executionAndReconciliationReceiptsCannotSubstituteForEachOther()throws Exception{
        var verifier=new AgentCartSecurity(bridge,confirm,reconcile,mock(StringRedisTemplate.class),json);String op=UUID.randomUUID().toString(),hash="a".repeat(64);
        Map<String,Object> claims=new HashMap<>(Map.of("purpose","reconcile_only","audience","mall-portal-agent-cart","operation_no",op,"action_id",op,"action_type","ADD_CART","member_id","1","parameters_hash",hash,"key_id","v1","control_epoch",1));claims.put("confirmed_at",Instant.now().toString());claims.put("confirmation_audit_id",UUID.randomUUID().toString());claims.put("issued_at",Instant.now().getEpochSecond());claims.put("expires_at",Instant.now().plusSeconds(300).getEpochSecond());
        String grant=sign(claims,reconcile);assertEquals("1",verifier.receipt(grant,"reconcile_only",op,hash,null).path("member_id").asText());
        assertThrows(AgentCartService.Rejected.class,()->verifier.receipt(grant,"execute_confirmed",op,hash,1L));
        assertThrows(AgentCartService.Rejected.class,()->verifier.receipt(grant,"reconcile_only",UUID.randomUUID().toString(),hash,null));
        assertThrows(AgentCartService.Rejected.class,()->verifier.receipt(grant,"reconcile_only",op,"b".repeat(64),null));
        claims.put("purpose","execute_confirmed");String attestation=sign(claims,confirm);assertNotNull(verifier.receipt(attestation,"execute_confirmed",op,hash,1L));assertThrows(AgentCartService.Rejected.class,()->verifier.receipt(attestation,"execute_confirmed",op,hash,2L));
        claims.put("expires_at",Instant.now().minusSeconds(1).getEpochSecond());String expired=sign(claims,confirm);assertThrows(AgentCartService.Rejected.class,()->verifier.receipt(expired,"execute_confirmed",op,hash,1L));
    }
    String sign(Map<String,Object> claims,String secret)throws Exception{String payload=Base64.getUrlEncoder().withoutPadding().encodeToString(json.writeValueAsBytes(claims));return payload+"."+Base64.getUrlEncoder().withoutPadding().encodeToString(AgentCartSecurity.hmac(secret,payload.getBytes(StandardCharsets.US_ASCII)));}
    @Test void canonicalHashMatchesPythonVectorWithUnicode()throws Exception{
        var service=new AgentCartService(null,json,null,null);String canonical=service.canonical(json.readTree("{\"b\":null,\"a\":{\"中文\":\"😀\",\"quantity\":2,\"amount\":\"2.00\"}}"));
        assertEquals("{\"a\":{\"amount\":\"2.00\",\"quantity\":2,\"中文\":\"😀\"},\"b\":null}",canonical);
        assertEquals("193337e50e24a71658d2b3c3e835bcc509628497c4749b385c0a08a8e32faf3b",AgentCartSecurity.sha(canonical.getBytes(StandardCharsets.UTF_8)));
    }
    @org.junit.jupiter.api.Test void canonicalNullFieldsSurviveGlobalNonNullConfiguration()throws Exception {
        var mapper=new com.fasterxml.jackson.databind.ObjectMapper();
        mapper.setSerializationInclusion(com.fasterxml.jackson.annotation.JsonInclude.Include.NON_NULL);
        var service=new AgentCartService(null,mapper,null,null);
        var params=mapper.readTree("{\"action_type\":\"CREATE_ORDER\",\"source\":\"CART\",\"cart_item_ids\":[\"1\"],\"address_id\":\"2\",\"coupon_id\":null,\"use_points\":0}");
        assertTrue(service.canonical(params).contains("\"coupon_id\":null"));
        var defaultService=new AgentCartService(null,new ObjectMapper(),null,null);
        assertEquals(defaultService.hash(1,params),service.hash(1,params));
    }
}
