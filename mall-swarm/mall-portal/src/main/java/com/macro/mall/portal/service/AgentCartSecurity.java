package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.common.log.BridgeSignatureObservation;
import com.macro.mall.common.exception.BridgeNonceUnavailableException;
import com.macro.mall.common.log.BridgeSignatureObservation.Guard;
import com.macro.mall.common.log.BridgeSignatureObservation.Phase;
import com.macro.mall.common.log.BridgeSignatureObservation.Outcome;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Component;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.*;

@Component
public class AgentCartSecurity {
    private final String bridge, confirmation, reconciliation;
    private final StringRedisTemplate redis;
    private final ObjectMapper json;
    public AgentCartSecurity(@Value("${MALL_AGENT_BRIDGE_SECRET:}") String bridge,
            @Value("${MALL_AGENT_CONFIRM_SECRET:}") String confirmation,
            @Value("${MALL_AGENT_RECONCILE_SECRET:}") String reconciliation,
            StringRedisTemplate redis, ObjectMapper json) {
        this.bridge=bridge; this.confirmation=confirmation; this.reconciliation=reconciliation;
        this.redis=redis; this.json=json;
    }
    public boolean signed(HttpServletRequest request, byte[] body) {
        try (var observation = BridgeSignatureObservation.start(request, Guard.CART)) {
            observation.phase(Phase.INPUT);
            try {
                String timestamp=request.getHeader("X-Agent-Timestamp"), nonce=request.getHeader("X-Agent-Nonce");
                String signature=request.getHeader("X-Agent-Signature");
                if (bridge.length()<32 || body.length>32768 || timestamp==null || nonce==null || signature==null
                        || !timestamp.matches("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z")
                        || !signature.matches("v1:[A-Za-z0-9_-]{43}") || UUID.fromString(nonce).version()!=4
                        || !UUID.fromString(nonce).toString().equals(nonce)
                        || Duration.between(Instant.parse(timestamp),Instant.now()).abs().getSeconds()>60) return observation.result(false, Outcome.INPUT_REJECTED);
                observation.phase(Phase.MAC);
                String target="/mall-portal"+request.getRequestURI();
                if(request.getQueryString()!=null) target+="?"+request.getQueryString();
                String canonical=String.join("\n",request.getMethod(),target,sha(body),timestamp,nonce);
                if(!MessageDigest.isEqual(hmac(bridge,canonical.getBytes(StandardCharsets.UTF_8)),Base64.getUrlDecoder().decode(signature.substring(3))))return observation.result(false, Outcome.MAC_MISMATCH);
                observation.phase(Phase.NONCE);
                Boolean nonceResult;
                try {
                    nonceResult = redis.opsForValue().setIfAbsent("mall-agent:bridge:nonce:v1:"+nonce,"1",Duration.ofSeconds(300));
                } catch (DataAccessException ex) {
                    observation.error(ex);
                    throw new BridgeNonceUnavailableException(ex);
                }
                boolean accepted = Boolean.TRUE.equals(nonceResult);
                return observation.result(accepted, accepted ? Outcome.ACCEPTED
                        : Boolean.FALSE.equals(nonceResult) ? Outcome.NONCE_REPLAY : Outcome.NONCE_RESULT_UNKNOWN);
            } catch(BridgeNonceUnavailableException ex) { throw ex; }
            catch(Exception ex) { observation.error(ex); return false; }
        }
    }
    public JsonNode receipt(String encoded, String purpose, String operation, String hash, Long memberId) {
        try {
            String secret=purpose.equals("execute_confirmed")?confirmation:reconciliation;
            if(secret.length()<32 || encoded==null || encoded.length()>4096) throw new IllegalArgumentException();
            String[] parts=encoded.split("\\.",-1);
            if(parts.length!=2 || !MessageDigest.isEqual(hmac(secret,parts[0].getBytes(StandardCharsets.US_ASCII)),Base64.getUrlDecoder().decode(parts[1])))throw new IllegalArgumentException();
            JsonNode data=json.readTree(Base64.getUrlDecoder().decode(parts[0]));
            long now=Instant.now().getEpochSecond();
            if(!purpose.equals(data.path("purpose").asText()) || !audience(data.path("action_type").asText()).equals(data.path("audience").asText())
                    || !operation.equals(data.path("operation_no").asText()) || !operation.equals(data.path("action_id").asText())
                    || !Set.of("ADD_CART","CREATE_ORDER","CANCEL_ORDER","CREATE_RETURN").contains(data.path("action_type").asText()) || !hash.equals(data.path("parameters_hash").asText())
                    || !"v1".equals(data.path("key_id").asText()) || data.path("control_epoch").asLong()<1
                    || !data.path("issued_at").isIntegralNumber() || data.path("issued_at").asLong()>now+10
                    || !data.path("expires_at").isIntegralNumber() || data.path("expires_at").asLong()<=now
                    || data.path("expires_at").asLong()-data.path("issued_at").asLong()>604800
                    || !data.path("member_id").asText().matches("[1-9][0-9]{0,18}")
                    || (memberId!=null && !memberId.toString().equals(data.path("member_id").asText())))throw new IllegalArgumentException();
            Long.parseLong(data.path("member_id").asText());
            UUID.fromString(data.path("confirmation_audit_id").asText());
            if(Instant.parse(data.path("confirmed_at").asText()).isAfter(Instant.now().plusSeconds(10)))throw new IllegalArgumentException();
            return data;
        } catch(Exception ex) { throw new AgentCartService.Rejected("CONFIRMATION_INVALID"); }
    }
    private static String audience(String type){return switch(type){case "ADD_CART"->"mall-portal-agent-cart";case "CREATE_RETURN"->"mall-portal-agent-return";default->"mall-portal-agent-order";};}
    public void proof(JsonNode proof,String operation,long member){
        try{
            String[] parts=proof.path("attestation").asText().split("\\.",-1);
            if(parts.length!=2 || !MessageDigest.isEqual(hmac(confirmation,parts[0].getBytes(StandardCharsets.US_ASCII)),Base64.getUrlDecoder().decode(parts[1])))throw new IllegalArgumentException();
            JsonNode claims=json.readTree(Base64.getUrlDecoder().decode(parts[0]));
            if(!"return_proof".equals(claims.path("purpose").asText()) || !operation.equals(claims.path("operation_no").asText()) || !Long.toString(member).equals(claims.path("member_id").asText())
                || claims.path("expires_at").asLong()<=Instant.now().getEpochSecond() || claims.path("expires_at").asLong()>Instant.now().plusSeconds(150).getEpochSecond())throw new IllegalArgumentException();
            for(String key:List.of("attachment_id","attachment_ref_id","sha256","mime_type","byte_size"))if(!claims.path(key).equals(proof.path(key)))throw new IllegalArgumentException();
            UUID.fromString(proof.path("attachment_id").asText());UUID.fromString(proof.path("attachment_ref_id").asText());
            if(!proof.path("sha256").asText().matches("[0-9a-f]{64}") || !"image/jpeg".equals(proof.path("mime_type").asText()) || proof.path("byte_size").asLong()<1 || proof.path("byte_size").asLong()>5242880)throw new IllegalArgumentException();
        }catch(Exception e){throw new AgentCartService.Rejected("PROOF_INVALID");}
    }
    static byte[] hmac(String secret,byte[] data)throws Exception {
        Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8),"HmacSHA256"));return mac.doFinal(data);
    }
    public static String sha(byte[] data) {
        try{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(data));}
        catch(Exception ex){throw new IllegalStateException("Hash unavailable");}
    }
}
