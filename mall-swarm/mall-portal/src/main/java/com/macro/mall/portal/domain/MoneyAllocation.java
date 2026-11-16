package com.macro.mall.portal.domain;

import com.macro.mall.common.exception.Asserts;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/** Allocate integer cents proportionally, with stable input order breaking remainder ties. */
public final class MoneyAllocation {
    private MoneyAllocation() { }

    public static List<BigDecimal> allocate(BigDecimal amount, List<BigDecimal> capacities) {
        BigInteger requested = cents(amount);
        List<BigInteger> weights = capacities.stream().map(MoneyAllocation::cents).toList();
        BigInteger total = weights.stream().reduce(BigInteger.ZERO, BigInteger::add);
        if (requested.compareTo(total) > 0) Asserts.fail("优惠金额超过可抵扣商品金额，请重新选择");
        List<BigInteger> allocated = new ArrayList<>();
        List<BigInteger> remainders = new ArrayList<>();
        BigInteger used = BigInteger.ZERO;
        for (BigInteger weight : weights) {
            BigInteger[] share = total.signum() == 0
                    ? new BigInteger[]{BigInteger.ZERO, BigInteger.ZERO}
                    : requested.multiply(weight).divideAndRemainder(total);
            allocated.add(share[0]);
            remainders.add(share[1]);
            used = used.add(share[0]);
        }
        List<Integer> order = new ArrayList<>();
        for (int i = 0; i < weights.size(); i++) order.add(i);
        order.sort(Comparator.<Integer, BigInteger>comparing(remainders::get).reversed()
                .thenComparingInt(Integer::intValue));
        int remaining = requested.subtract(used).intValueExact();
        for (int i = 0; i < remaining; i++) {
            int index = order.get(i);
            allocated.set(index, allocated.get(index).add(BigInteger.ONE));
        }
        return allocated.stream().map(value -> new BigDecimal(value, 2)).toList();
    }

    private static BigInteger cents(BigDecimal amount) {
        if (amount == null || amount.signum() < 0) Asserts.fail("商品或优惠金额无效，请重新选择");
        try {
            return amount.movePointRight(2).toBigIntegerExact();
        } catch (ArithmeticException ex) {
            Asserts.fail("商品或优惠金额精度无效，请重新选择");
            throw ex;
        }
    }
}
