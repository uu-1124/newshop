package com.macro.mall.portal.controller;

import com.macro.mall.model.UmsMember;
import com.macro.mall.portal.service.UmsMemberService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class MemberInfoResponseSecurityTest {
    private final UmsMemberService service = mock(UmsMemberService.class);
    private final UmsMemberController controller = new UmsMemberController();
    private UmsMember cachedMember;
    private MockMvc mvc;

    @BeforeEach void setUp() {
        ReflectionTestUtils.setField(controller, "memberService", service);
        cachedMember = new UmsMember();
        cachedMember.setId(11L);
        cachedMember.setUsername("member");
        cachedMember.setNickname("Demo member");
        cachedMember.setIntegration(100);
        cachedMember.setPassword("sensitive-password-hash");
        when(service.getCurrentMember()).thenReturn(cachedMember);
        mvc = MockMvcBuilders.standaloneSetup(controller).build();
    }

    @Test void serializedInfoHidesPasswordAndKeepsProfile() throws Exception {
        mvc.perform(get("/sso/info"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data.id").value(11))
                .andExpect(jsonPath("$.data.username").value("member"))
                .andExpect(jsonPath("$.data.nickname").value("Demo member"))
                .andExpect(jsonPath("$.data.integration").value(100))
                .andExpect(jsonPath("$.data.password").doesNotExist());
    }

    @Test void repeatedInfoCallsDoNotMutateCachedCredentials() {
        UmsMember first = (UmsMember) controller.info().getData();
        UmsMember second = (UmsMember) controller.info().getData();
        assertNull(first.getPassword());
        assertNull(second.getPassword());
        assertNotSame(cachedMember, first);
        assertNotSame(first, second);
        assertEquals("sensitive-password-hash", cachedMember.getPassword());
        assertEquals(cachedMember.getUsername(), first.getUsername());
    }
}
