package com.macro.mall.config;

import org.mybatis.spring.annotation.MapperScan;
import com.macro.mall.order.OrderResourceService;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.Configuration;
import org.springframework.transaction.annotation.EnableTransactionManagement;

/**
 * MyBatis相关配置
 * Created by macro on 2019/4/8.
 */
@Configuration
@EnableTransactionManagement
@MapperScan({"com.macro.mall.mapper","com.macro.mall.dao","com.macro.mall.order.dao"})
@Import(OrderResourceService.class)
public class MyBatisConfig {
}
