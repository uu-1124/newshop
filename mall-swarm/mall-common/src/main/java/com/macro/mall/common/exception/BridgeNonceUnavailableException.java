package com.macro.mall.common.exception;

import org.springframework.dao.DataAccessException;

/** A nonce was not confirmed; the caller must never proceed with the operation. */
public final class BridgeNonceUnavailableException extends RuntimeException {
    public BridgeNonceUnavailableException(DataAccessException cause) {
        super("Bridge nonce store unavailable", cause);
    }
}
