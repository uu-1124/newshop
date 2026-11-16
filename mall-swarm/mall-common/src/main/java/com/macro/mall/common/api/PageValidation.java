package com.macro.mall.common.api;

import com.macro.mall.common.exception.Asserts;

/** Preserve the distinct SQL (one based) and Elasticsearch (zero based) contracts. */
public final class PageValidation {
    private static final int MAX_PAGE_SIZE = 100;

    private PageValidation() { }

    public static void sql(Integer pageNum, Integer pageSize) {
        validate(pageNum, pageSize, 1, Integer.MAX_VALUE);
    }

    public static void search(Integer pageNum, Integer pageSize) {
        validate(pageNum, pageSize, 0, 10_000);
    }

    private static void validate(Integer pageNum, Integer pageSize, int firstPage, long maxEnd) {
        if (pageNum == null || pageSize == null || pageNum < firstPage || pageSize < 1
                || pageSize > MAX_PAGE_SIZE || ((long) pageNum - firstPage + 1) * pageSize > maxEnd) {
            Asserts.fail(ResultCode.VALIDATE_FAILED);
        }
    }
}
