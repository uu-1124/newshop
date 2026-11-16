package com.macro.mall.portal.service;

import com.macro.mall.portal.domain.MoneyAllocation;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.util.List;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.*;

class MoneyAllocationTest {
    @Test void largestRemainderIsExactAndStable() {
        assertEquals(List.of(new BigDecimal("0.34"), new BigDecimal("0.33"), new BigDecimal("0.33")),
                MoneyAllocation.allocate(new BigDecimal("1.00"), List.of(BigDecimal.ONE, BigDecimal.ONE, BigDecimal.ONE)));
        assertEquals(List.of(new BigDecimal("0.01"), new BigDecimal("0.04")),
                MoneyAllocation.allocate(new BigDecimal("0.05"), List.of(new BigDecimal("0.01"), new BigDecimal("0.05"))));
    }

    @Test void generatedCentCasesConserveAmountsAndRespectEveryCapacity() {
        Random random = new Random(20261112);
        for (int sample = 0; sample < 1500; sample++) {
            List<BigDecimal> capacities = random.ints(1 + random.nextInt(12), 0, 10000)
                    .mapToObj(cents -> BigDecimal.valueOf(cents, 2)).toList();
            int total = capacities.stream().mapToInt(value -> value.movePointRight(2).intValueExact()).sum();
            BigDecimal amount = BigDecimal.valueOf(random.nextInt(total + 1), 2);
            List<BigDecimal> result = MoneyAllocation.allocate(amount, capacities);
            assertEquals(amount, result.stream().reduce(new BigDecimal("0.00"), BigDecimal::add));
            for (int i = 0; i < result.size(); i++) {
                assertTrue(result.get(i).signum() >= 0);
                assertTrue(result.get(i).compareTo(capacities.get(i)) <= 0);
            }
            assertEquals(result, MoneyAllocation.allocate(amount, capacities));
        }
    }

    @Test void invalidDiscountsFailInsteadOfRoundingOrExceedingCapacity() {
        for (String value : List.of("-0.01", "1.001", "1.01"))
            assertThrows(RuntimeException.class, () -> MoneyAllocation.allocate(new BigDecimal(value), List.of(BigDecimal.ONE)));
        assertEquals(List.of(new BigDecimal("0.00")), MoneyAllocation.allocate(BigDecimal.ZERO, List.of(BigDecimal.ZERO)));
    }
}
