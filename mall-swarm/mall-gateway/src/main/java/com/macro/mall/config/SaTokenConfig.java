package com.macro.mall.config;

import cn.dev33.satoken.context.SaHolder;
import cn.dev33.satoken.exception.NotLoginException;
import cn.dev33.satoken.exception.NotPermissionException;
import cn.dev33.satoken.reactor.filter.SaReactorFilter;
import cn.dev33.satoken.router.SaHttpMethod;
import cn.dev33.satoken.router.SaRouter;
import cn.dev33.satoken.stp.StpUtil;
import cn.hutool.core.collection.CollUtil;
import cn.hutool.core.convert.Convert;
import com.macro.mall.common.api.CommonResult;
import com.macro.mall.common.constant.AuthConstant;
import com.macro.mall.util.StpMemberUtil;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.util.AntPathMatcher;
import org.springframework.util.PathMatcher;

import java.util.*;

/**
 * @auther macrozheng
 * @description Sa-Token相关配置
 * @date 2023/11/28
 * @github https://github.com/macrozheng
 */
@Configuration
public class SaTokenConfig {

    @Autowired
    private RedisTemplate<String, Object> redisTemplate;

    /**
     * 注册Sa-Token全局过滤器
     */
    @Bean
    public SaReactorFilter getSaReactorFilter(IgnoreUrlsConfig ignoreUrlsConfig) {
        return new SaReactorFilter()
                // 拦截地址
                .addInclude("/**")
                // 配置白名单路径
                .setExcludeList(ignoreUrlsConfig.getUrls())
                // 鉴权方法：每次访问进入
                .setAuth(obj -> {
                    // 对于OPTIONS预检请求直接放行
                    SaRouter.match(SaHttpMethod.OPTIONS).stop();
                    // Agent member authentication is handled by its signed Java identity bridge.
                    // Internal paths are never routed; staff endpoints require their own login and bridge.
                    SaRouter.match("/mall-agent/v1/member/**").stop();
                    // Staff login is checked here; fresh resource permissions are checked by the signed identity bridge.
                    SaRouter.match("/mall-agent/v1/admin/**", r -> StpUtil.checkLogin()).stop();
                    SaRouter.match("/mall-agent/**", r -> { throw new NotPermissionException("agent-route"); });
                    // Recovery has a separately signed, operation-scoped receipt and cannot execute.
                    SaRouter.match("/mall-portal/agent-bridge/v1/operations/*/reconcile").stop();
                    // 登录认证：商城前台会员认证
                    SaRouter.match("/mall-portal/**", r -> StpMemberUtil.checkLogin()).stop();
                    // 登录认证：管理后台用户认证
                    SaRouter.match("/mall-admin/**", r -> StpUtil.checkLogin());
                    SaRouter.match("/mall-search/**", r -> StpUtil.checkLogin());
                    // 权限认证：管理后台用户权限校验
                    // 获取Redis中缓存的各个接口路径所需权限规则
                    Map<Object, Object> pathResourceMap = redisTemplate.opsForHash().entries(AuthConstant.PATH_RESOURCE_MAP);
                    // 获取到访问当前接口所需权限（一个路径对应多个资源时，拥有任意一个资源都可以访问该路径）
                    List<String> needPermissionList = new ArrayList<>();
                    // 获取当前请求路径
                    String requestPath = SaHolder.getRequest().getRequestPath();
                    boolean searchManagement = requestPath.startsWith("/mall-search/");
                    // Index maintenance uses the existing product-management resource.
                    String permissionPath = searchManagement ? "/mall-admin/product" : requestPath;
                    // 创建路径匹配器
                    PathMatcher pathMatcher = new AntPathMatcher();
                    Set<Map.Entry<Object, Object>> entrySet = pathResourceMap.entrySet();
                    for (Map.Entry<Object, Object> entry : entrySet) {
                        String pattern = (String) entry.getKey();
                        if (pathMatcher.match(pattern, permissionPath)) {
                            needPermissionList.add((String) entry.getValue());
                        }
                    }
                    if (searchManagement && CollUtil.isEmpty(needPermissionList)) {
                        throw new NotPermissionException("search-index-management");
                    }
                    // 接口需要权限时鉴权
                    if(CollUtil.isNotEmpty(needPermissionList)){
                        SaRouter.match(requestPath, r -> StpUtil.checkPermissionOr(Convert.toStrArray(needPermissionList)));
                    }
                })
                // setAuth方法异常处理
                .setError(this::handleException);
    }

    /**
     * 自定义异常处理
     */
    private Object handleException(Throwable e) {
        if (SaHolder.getRequest().getRequestPath().startsWith("/mall-agent/")) {
            int status = e instanceof NotLoginException ? 401 : e instanceof NotPermissionException ? 403 : 503;
            String code = status == 401 ? "AUTH_REQUIRED" : status == 403 ? "FORBIDDEN" : "AUTH_SERVICE_UNAVAILABLE";
            String message = status == 401 ? "请登录对应账号后继续" : status == 403 ? "当前身份没有相关权限" : "权限服务暂不可用，请稍后重试";
            String requestId = SaHolder.getRequest().getHeader("X-Request-ID");
            try {
                if (requestId == null || !UUID.fromString(requestId).toString().equals(requestId)) requestId = UUID.randomUUID().toString();
            } catch (IllegalArgumentException invalid) { requestId = UUID.randomUUID().toString(); }
            SaHolder.getResponse().setStatus(status)
                    .setHeader("Content-Type", "application/json; charset=utf-8")
                    .setHeader("X-Request-ID", requestId).setHeader("Cache-Control", "no-store");
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("code", code); body.put("message", message); body.put("data", null); body.put("request_id", requestId);
            return cn.hutool.json.JSONUtil.toJsonStr(body, cn.hutool.json.JSONConfig.create().setIgnoreNullValue(false));
        }
        //设置错误返回格式为JSON
        SaHolder.getResponse()
                .setHeader("Content-Type", "application/json; charset=utf-8")
                .setHeader("Access-Control-Allow-Origin", "*")
                .setHeader("Cache-Control", "no-cache");
        CommonResult result = null;
        if(e instanceof NotLoginException){
            result = CommonResult.unauthorized(null);
        }else if(e instanceof NotPermissionException){
            result = CommonResult.forbidden(null);
        }else{
            result = CommonResult.failed(e.getMessage());
        }
        return result;
    }
}

