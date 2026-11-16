package com.macro.mall.portal.service;
import com.macro.mall.order.AgentAfterSaleStore;
import org.junit.jupiter.api.Test;
import java.time.*;
import static org.junit.jupiter.api.Assertions.*;
class AgentReturnPolicyTest {
    @Test void logisticsTimelineIncludesBoundariesAndRejectsFutureBeforeShipmentAndNeighbourCrossing(){
        Instant created=Instant.parse("2026-11-12T00:00:00Z"),shipped=created.plusSeconds(3600),now=created.plusSeconds(7200);
        assertDoesNotThrow(()->AgentAfterSaleStore.validateNodeTime(shipped,now,created,shipped,null,null));
        assertDoesNotThrow(()->AgentAfterSaleStore.validateNodeTime(now,now,created,null,now,now));
        for(Instant invalid: new Instant[]{created.minusNanos(1),shipped.minusNanos(1),now.plusNanos(1)})
            assertEquals("LOGISTICS_TIME_INVALID",assertThrows(AgentAfterSaleStore.Rejected.class,()->AgentAfterSaleStore.validateNodeTime(invalid,now,created,shipped,null,null)).code);
        assertThrows(AgentAfterSaleStore.Rejected.class,()->AgentAfterSaleStore.validateNodeTime(shipped,now,created,shipped,shipped.plusSeconds(1),null));
        assertThrows(AgentAfterSaleStore.Rejected.class,()->AgentAfterSaleStore.validateNodeTime(now,now,created,shipped,null,now.minusSeconds(1)));
        assertEquals("BUSINESS_DATA_INVALID",assertThrows(AgentAfterSaleStore.Rejected.class,()->AgentAfterSaleStore.validateNodeTime(now,now,null,null,null,null)).code);
        assertThrows(AgentAfterSaleStore.Rejected.class,()->AgentAfterSaleStore.validateNodeTime(now,now,created,created.minusSeconds(1),null,null));
    }
    @Test void legacyOmsDatetimeAndNewTimelineUseDifferentExplicitZones(){
        LocalDateTime stored=LocalDateTime.of(2026,11,12,21,30);
        assertEquals("2026-11-12T13:30:00Z",AgentAfterSaleStore.legacyTime(stored));
        assertEquals("2026-11-12T21:30:00Z",AgentAfterSaleStore.time(stored));
    }
    @Test void returnWindowUses168HoursIncludesExactDeadlineAndRejectsMissingOrFutureReceipt(){
        Instant received=Instant.parse("2026-11-05T08:00:00Z"),deadline=received.plus(Duration.ofHours(168));
        assertTrue(AgentAfterSaleStore.withinWindow(received,received));
        assertTrue(AgentAfterSaleStore.withinWindow(received,deadline));
        assertFalse(AgentAfterSaleStore.withinWindow(received,deadline.plusNanos(1)));
        assertFalse(AgentAfterSaleStore.withinWindow(received,received.minusSeconds(1)));
        assertFalse(AgentAfterSaleStore.withinWindow(null,deadline));
    }
}
