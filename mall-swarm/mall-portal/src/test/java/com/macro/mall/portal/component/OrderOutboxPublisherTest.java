package com.macro.mall.portal.component;

import com.macro.mall.portal.service.OrderBusinessOutbox;
import com.rabbitmq.client.AMQP;
import com.rabbitmq.client.Channel;
import com.rabbitmq.client.Return;
import com.rabbitmq.client.ReturnCallback;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.amqp.rabbit.connection.Connection;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class OrderOutboxPublisherTest {
    private OrderBusinessOutbox outbox;
    private ConnectionFactory factory;
    private Connection connection;
    private Channel channel;
    private OrderOutboxPublisher publisher;
    private final OrderBusinessOutbox.Publication event=new OrderBusinessOutbox.Publication("order-timeout:42",42,"synthetic",3);

    @BeforeEach void setup(){
        outbox=mock(OrderBusinessOutbox.class);factory=mock(ConnectionFactory.class);
        connection=mock(Connection.class);channel=mock(Channel.class);
        when(outbox.claim()).thenReturn(event,null);
        when(factory.createConnection()).thenReturn(connection);when(connection.createChannel(false)).thenReturn(channel);
        publisher=new OrderOutboxPublisher(outbox,factory);
    }
    @Test void confirmedPersistentMandatoryMessageUsesOriginalEventIdAndClosesResources()throws Exception{
        publisher.publishDue();
        var properties=ArgumentCaptor.forClass(AMQP.BasicProperties.class);
        verify(channel).confirmSelect();verify(channel).basicPublish(eq("mall.order.direct"),eq("mall.order.cancel"),eq(true),properties.capture(),any(byte[].class));
        assertEquals(2,properties.getValue().getDeliveryMode());assertEquals(event.eventKey(),properties.getValue().getMessageId());
        verify(channel).waitForConfirmsOrDie(5000);verify(outbox).published(event);verify(outbox,never()).failed(any());
        verify(channel).close();verify(connection).close();
    }
    @Test void lostBrokerConfirmKeepsDurableEventRetryable()throws Exception{
        doThrow(new java.io.IOException("synthetic confirm lost")).when(channel).waitForConfirmsOrDie(5000);
        publisher.publishDue();verify(outbox).failed(event);verify(outbox,never()).published(any());verify(connection).close();
    }
    @Test void mandatoryReturnIsFailureEvenWhenBrokerAckArrives()throws Exception{
        var callback=new ReturnCallback[1];doAnswer(inv->{callback[0]=inv.getArgument(0);return null;}).when(channel).addReturnListener(any(ReturnCallback.class));
        doAnswer(inv->{callback[0].handle(new Return(312,"NO_ROUTE","mall.order.direct","mall.order.cancel",new AMQP.BasicProperties(),new byte[0]));return null;}).when(channel).waitForConfirmsOrDie(5000);
        publisher.publishDue();verify(outbox).failed(event);verify(outbox,never()).published(any());verify(connection).close();
    }
    @Test void connectionFailureDoesNotMarkPublished(){
        when(factory.createConnection()).thenThrow(new IllegalStateException("synthetic unavailable"));
        publisher.publishDue();verify(outbox).failed(event);verify(outbox,never()).published(any());
    }
}
