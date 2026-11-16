package com.macro.mall.portal.service;

import com.macro.mall.order.OrderResourceService;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;
import java.util.*;

/** Durable cancellation notification belongs to the original order transaction. */
@Service
public class OrderBusinessOutbox {
    private final JdbcTemplate db;
    private final TransactionTemplate tx;
    private final OrderResourceService resources;
    public record Publication(String eventKey,long orderId,String owner,long fence) {}

    public OrderBusinessOutbox(JdbcTemplate db,PlatformTransactionManager manager,OrderResourceService resources){
        this.db=db;this.tx=new TransactionTemplate(manager);this.resources=resources;
    }
    public void enqueue(long orderId,long delayMillis){
        if(!TransactionSynchronizationManager.isActualTransactionActive())throw new IllegalStateException("Order outbox requires the business transaction");
        if(orderId<=0||delayMillis<0||delayMillis>2_592_000_000L)throw new IllegalArgumentException("Invalid cancellation deadline");
        if(!Boolean.TRUE.equals(db.queryForObject("SELECT EXISTS(SELECT 1 FROM oms_order WHERE id=?)",Boolean.class,orderId)))throw new IllegalStateException("Order missing");
        db.update("INSERT INTO oms_agent_business_outbox(event_key,order_id,event_type,due_at,next_attempt_at) VALUES(?,?,'ORDER_TIMEOUT_CANCEL',TIMESTAMPADD(MICROSECOND,?,UTC_TIMESTAMP(6)),TIMESTAMPADD(MICROSECOND,?,UTC_TIMESTAMP(6))) ON DUPLICATE KEY UPDATE event_key=event_key",
            "order-timeout:"+orderId,orderId,Math.multiplyExact(delayMillis,1000),Math.multiplyExact(delayMillis,1000));
    }
    public Publication claim(){
        return tx.execute(status->{
            var candidates=db.queryForList("SELECT * FROM oms_agent_business_outbox WHERE state IN ('PENDING','PUBLISHING','SENT') AND due_at<=UTC_TIMESTAMP(6) AND next_attempt_at<=UTC_TIMESTAMP(6) AND (lease_until IS NULL OR lease_until<=UTC_TIMESTAMP(6)) ORDER BY due_at,event_key LIMIT 1 FOR UPDATE SKIP LOCKED");
            if(candidates.isEmpty())return null;
            var row=candidates.get(0);String key=row.get("event_key").toString(),owner=UUID.randomUUID().toString();long fence=((Number)row.get("fencing_token")).longValue()+1;
            db.update("UPDATE oms_agent_business_outbox SET state='PUBLISHING',lease_owner=?,lease_until=UTC_TIMESTAMP(6)+INTERVAL 30 SECOND,fencing_token=?,attempts=attempts+1,updated_at=UTC_TIMESTAMP(6) WHERE event_key=?",owner,fence,key);
            return new Publication(key,((Number)row.get("order_id")).longValue(),owner,fence);
        });
    }
    public void published(Publication event){
        // SENT is republished after 60s until a transactional consumer receipt exists.
        db.update("UPDATE oms_agent_business_outbox SET state='SENT',published_at=UTC_TIMESTAMP(6),next_attempt_at=UTC_TIMESTAMP(6)+INTERVAL 60 SECOND,lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=UTC_TIMESTAMP(6) WHERE event_key=? AND state='PUBLISHING' AND lease_owner=? AND fencing_token=? AND lease_until>UTC_TIMESTAMP(6)",event.eventKey(),event.owner(),event.fence());
    }
    public void failed(Publication event){
        db.update("UPDATE oms_agent_business_outbox SET state='PENDING',next_attempt_at=UTC_TIMESTAMP(6)+INTERVAL 5 SECOND,lease_owner=NULL,lease_until=NULL,last_error_code='BROKER_PUBLISH_UNCONFIRMED',updated_at=UTC_TIMESTAMP(6) WHERE event_key=? AND state='PUBLISHING' AND lease_owner=? AND fencing_token=? AND lease_until>UTC_TIMESTAMP(6)",event.eventKey(),event.owner(),event.fence());
    }
    public void consume(long orderId){
        tx.executeWithoutResult(status->{
            var rows=db.queryForList("SELECT state,due_at<=UTC_TIMESTAMP(6) due FROM oms_agent_business_outbox WHERE order_id=? AND event_type='ORDER_TIMEOUT_CANCEL'",orderId);
            if(!rows.isEmpty()){
                if("CONSUMED".equals(rows.get(0).get("state")))return;
                Object due=rows.get(0).get("due");
                if(!Boolean.TRUE.equals(due)&&(!(due instanceof Number)||((Number)due).intValue()!=1))throw new IllegalStateException("Cancellation event is not due");
            }
            // Legacy delay-queue messages still converge on the same guarded transaction.
            resources.closePendingOrder(orderId);
            db.update("UPDATE oms_agent_business_outbox SET state='CONSUMED',consumed_at=UTC_TIMESTAMP(6),lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=UTC_TIMESTAMP(6) WHERE order_id=? AND event_type='ORDER_TIMEOUT_CANCEL'",orderId);
        });
    }
}
