package com.macro.mall.portal.service;

import com.macro.mall.portal.dao.CartGuardDao;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionSynchronizationManager;

@Service
public class CartGuard {
    private final CartGuardDao dao;
    public CartGuard(CartGuardDao dao) { this.dao = dao; }
    public void lock(Long memberId) {
        if (!TransactionSynchronizationManager.isActualTransactionActive())
            throw new IllegalStateException("Cart mutex requires an active transaction");
        dao.ensure(memberId);
        if (dao.lock(memberId) == null) throw new IllegalStateException("Cart mutex unavailable");
    }
}
