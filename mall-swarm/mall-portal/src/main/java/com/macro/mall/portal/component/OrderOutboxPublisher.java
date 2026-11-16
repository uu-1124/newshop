package com.macro.mall.portal.component;

import com.macro.mall.portal.domain.QueueEnum;
import com.macro.mall.portal.service.OrderBusinessOutbox;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.support.converter.SimpleMessageConverter;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import com.rabbitmq.client.AMQP;
import java.util.concurrent.atomic.AtomicBoolean;

/** Publish with broker confirms and mandatory routing, outside SQL transactions. */
@Component
@ConditionalOnProperty(name="mall.order.business-outbox.enabled",havingValue="true",matchIfMissing=true)
public class OrderOutboxPublisher {
    private final OrderBusinessOutbox outbox;
    private final ConnectionFactory connections;
    public OrderOutboxPublisher(OrderBusinessOutbox outbox,ConnectionFactory connections){this.outbox=outbox;this.connections=connections;}
    @Scheduled(fixedDelay=1000,initialDelay=5000)
    public void publishDue(){
        for(int n=0;n<10;n++){
            var event=outbox.claim();if(event==null)return;
            try{
                var connection=connections.createConnection();
                try(var channel=connection.createChannel(false)){
                    channel.confirmSelect();
                    var returned=new AtomicBoolean(false);
                    channel.addReturnListener(value->returned.set(true));
                    var properties=new MessageProperties();properties.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
                    var message=new SimpleMessageConverter().toMessage(event.orderId(),properties);
                    var basic=new AMQP.BasicProperties.Builder().contentType(message.getMessageProperties().getContentType())
                        .deliveryMode(2).messageId(event.eventKey()).build();
                    channel.basicPublish(QueueEnum.QUEUE_ORDER_CANCEL.getExchange(),QueueEnum.QUEUE_ORDER_CANCEL.getRouteKey(),true,basic,message.getBody());
                    channel.waitForConfirmsOrDie(5000);
                    if(returned.get())throw new IllegalStateException("Cancellation route unavailable");
                }finally{connection.close();}
                outbox.published(event);
            }catch(Exception ex){outbox.failed(event);return;}
        }
    }
}
