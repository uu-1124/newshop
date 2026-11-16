package com.macro.mall.portal.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macro.mall.order.AgentAfterSaleStore;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

@Service
public class AgentOrderQueries {
    private final JdbcTemplate db; private final ObjectMapper json; private final AgentAfterSaleStore store; private final String secret;
    public AgentOrderQueries(JdbcTemplate db,ObjectMapper json,AgentAfterSaleStore store,@Value("${MALL_AGENT_BRIDGE_SECRET:}") String secret){this.db=db;this.json=json;this.store=store;this.secret=secret;}
    public Map<String,Object> list(long member,Map<String,String> filters){
        if(!Set.of("limit","cursor","order_sn","status").containsAll(filters.keySet()))throw new AgentAfterSaleStore.Rejected("VALIDATION_ERROR",422);
        int limit;Integer status=null;try{limit=Integer.parseInt(filters.getOrDefault("limit","20"));if(filters.containsKey("status"))status=Integer.valueOf(filters.get("status"));}catch(Exception e){throw new AgentAfterSaleStore.Rejected("VALIDATION_ERROR",422);}
        String sn=filters.getOrDefault("order_sn","");if(limit<1 || limit>100 || sn.length()>80 || (status!=null && (status<0 || status>5)))throw new AgentAfterSaleStore.Rejected("VALIDATION_ERROR",422);
        String scope=store.encode(AgentAfterSaleStore.map("member",member,"limit",limit,"sn",sn,"status",status));long after=Long.MAX_VALUE;
        String cursor=filters.get("cursor");
        if(cursor!=null)try{String[] p=cursor.split("\\.",-1);byte[] raw=Base64.getUrlDecoder().decode(p[0]);if(cursor.length()>2048 || p.length!=2 || !MessageDigest.isEqual(mac(raw),Base64.getUrlDecoder().decode(p[1])))throw new IllegalArgumentException();var d=json.readTree(raw);if(!scope.equals(d.path("scope").asText()) || d.path("id").asLong()<1)throw new IllegalArgumentException();after=d.path("id").asLong();}catch(Exception e){throw new AgentAfterSaleStore.Rejected("INVALID_CURSOR",400);}
        var ids=db.queryForList("SELECT id FROM oms_order WHERE member_id=? AND delete_status=0 AND id<? AND (?='' OR order_sn=?) AND (? IS NULL OR status=?) ORDER BY id DESC LIMIT ?",Long.class,member,after,sn,sn,status,status,limit+1);
        boolean more=ids.size()>limit;var page=ids.subList(0,Math.min(limit,ids.size()));String next=null;
        if(more){byte[] payload=store.encode(Map.of("scope",scope,"id",page.get(page.size()-1))).getBytes(StandardCharsets.UTF_8);next=Base64.getUrlEncoder().withoutPadding().encodeToString(payload)+"."+Base64.getUrlEncoder().withoutPadding().encodeToString(mac(payload));}
        return AgentAfterSaleStore.map("items",page.stream().map(id->store.orderSummary(member,id)).toList(),"next_cursor",next,"has_more",more);
    }
    private byte[] mac(byte[] raw){try{if(secret.length()<32)throw new IllegalStateException();Mac m=Mac.getInstance("HmacSHA256");m.init(new SecretKeySpec(("order-cursor-v1:"+secret).getBytes(StandardCharsets.UTF_8),"HmacSHA256"));return m.doFinal(raw);}catch(Exception e){throw new IllegalStateException("Order cursor unavailable",e);}}
}
