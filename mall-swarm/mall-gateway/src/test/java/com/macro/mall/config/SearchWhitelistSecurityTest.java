package com.macro.mall.config;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.config.YamlPropertiesFactoryBean;
import org.springframework.core.io.ClassPathResource;
import org.springframework.util.AntPathMatcher;
import java.util.List;
import java.util.Properties;
import static org.junit.jupiter.api.Assertions.*;

class SearchWhitelistSecurityTest {
    private List<String> whitelist() {
        YamlPropertiesFactoryBean yaml = new YamlPropertiesFactoryBean();
        yaml.setResources(new ClassPathResource("application.yml"));
        Properties p = yaml.getObject();
        return p.stringPropertyNames().stream().filter(k -> k.startsWith("secure.ignore.urls[")).map(p::getProperty).toList();
    }
    private boolean publicPath(String path) { return whitelist().stream().anyMatch(p -> new AntPathMatcher().match(p, path)); }
    @Test void allReadEndpointsStayPublic() {
        for (String p : List.of("/esProduct/search", "/esProduct/search/simple", "/esProduct/search/relate", "/esProduct/recommend/1"))
            assertTrue(publicPath("/mall-search" + p), p);
    }
    @Test void mutationsAndFutureEndpointsAreNotPublic() {
        for (String p : List.of("/esProduct/importAll", "/esProduct/create/1", "/esProduct/delete/1", "/esProduct/delete/batch", "/new/admin"))
            assertFalse(publicPath("/mall-search" + p), p);
    }
    @Test void productResourceMatchesMaintenancePermissionPath() {
        assertTrue(new AntPathMatcher().match("/mall-admin/product/**", "/mall-admin/product"));
        assertFalse(new AntPathMatcher().match("/mall-admin/brand/**", "/mall-admin/product"));
    }
    @Test void minioUploadIsNotAnonymous() {
        assertFalse(publicPath("/mall-admin/minio/upload"));
    }
    @Test void adminRegistrationIsNotAnonymous() {
        assertFalse(publicPath("/mall-admin/admin/register"));
    }
}
