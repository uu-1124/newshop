package com.macro.mall.controller;

import cn.dev33.satoken.stp.StpUtil;
import com.github.pagehelper.Page;
import com.macro.mall.dto.UmsAdminParam;
import com.macro.mall.model.UmsAdmin;
import com.macro.mall.model.UmsResource;
import com.macro.mall.service.UmsAdminService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.MockedStatic;
import org.springframework.test.util.ReflectionTestUtils;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class AdminResponseSecurityTest {
    private final UmsAdminService service = mock(UmsAdminService.class);
    private final UmsAdminController controller = new UmsAdminController();
    private UmsAdmin admin;
    @BeforeEach void setUp() {
        ReflectionTestUtils.setField(controller,"adminService",service);
        admin = new UmsAdmin();admin.setId(1L);admin.setUsername("test");admin.setPassword("sensitive-hash");
    }
    @Test void detailSanitizesCopyWithoutChangingCachedCredentials() {
        when(service.getItem(1L)).thenReturn(admin);
        var result=controller.getItem(1L).getData();
        assertNull(result.getPassword());assertEquals("test",result.getUsername());
        assertEquals("sensitive-hash",admin.getPassword());assertNotSame(admin,result);
    }
    @Test void listKeepsPaginationAndDoesNotMutateCredentials() {
        Page<UmsAdmin> page = new Page<>(2,5);page.setTotal(12);page.add(admin);
        when(service.list(null,5,2)).thenReturn(page);
        var result=controller.list(null,5,2).getData();
        assertEquals(12L,result.getTotal());assertEquals(2,result.getPageNum());assertEquals(5,result.getPageSize());
        assertNull(result.getList().get(0).getPassword());assertEquals("sensitive-hash",admin.getPassword());
    }
    @Test void anonymousRegistrationDoesNotReachService() {
        try(MockedStatic<StpUtil> auth=mockStatic(StpUtil.class)) {
            auth.when(StpUtil::isLogin).thenReturn(false);
            assertEquals(401,controller.register(new UmsAdminParam()).getCode());verifyNoInteractions(service);
        }
    }
    @Test void rolelessAdministratorCannotCreateAccounts() {
        try(MockedStatic<StpUtil> auth=mockStatic(StpUtil.class)) {
            auth.when(StpUtil::isLogin).thenReturn(true);
            when(service.getCurrentAdmin()).thenReturn(admin);when(service.getResourceList(1L)).thenReturn(List.of());
            assertEquals(403,controller.register(new UmsAdminParam()).getCode());verify(service,never()).register(any());
        }
    }
    @Test void accountManagerCanCreateWithoutHashInResponse() {
        try(MockedStatic<StpUtil> auth=mockStatic(StpUtil.class)) {
            auth.when(StpUtil::isLogin).thenReturn(true);
            when(service.getCurrentAdmin()).thenReturn(admin);
            UmsResource resource=new UmsResource();resource.setUrl("/admin/**");
            when(service.getResourceList(1L)).thenReturn(List.of(resource));
            UmsAdminParam param=new UmsAdminParam();when(service.register(param)).thenReturn(admin);
            var result=controller.register(param);assertEquals(200,result.getCode());assertNull(result.getData().getPassword());
            assertEquals("sensitive-hash",admin.getPassword());
        }
    }
}
