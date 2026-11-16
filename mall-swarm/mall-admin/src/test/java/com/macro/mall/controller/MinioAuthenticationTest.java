package com.macro.mall.controller;

import cn.dev33.satoken.stp.StpUtil;
import org.junit.jupiter.api.Test;
import org.mockito.MockedStatic;
import org.springframework.mock.web.MockMultipartFile;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class MinioAuthenticationTest {
    @Test void anonymousUploadIsRejectedBeforeStorageAccess() {
        try (MockedStatic<StpUtil> auth = mockStatic(StpUtil.class)) {
            auth.when(StpUtil::isLogin).thenReturn(false);
            var file = new MockMultipartFile("file", "test.txt", "text/plain", new byte[]{1});
            assertEquals(401, new MinioController().upload(file).getCode());
        }
    }
    @Test void anonymousDeleteIsRejectedBeforeStorageAccess() {
        try (MockedStatic<StpUtil> auth = mockStatic(StpUtil.class)) {
            auth.when(StpUtil::isLogin).thenReturn(false);
            assertEquals(401, new MinioController().delete("test.txt").getCode());
        }
    }
}
