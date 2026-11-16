package com.macro.mall.portal.dao;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface CartGuardDao {
    @Insert("INSERT INTO oms_agent_cart_guard(member_id) VALUES(#{memberId}) ON DUPLICATE KEY UPDATE member_id=VALUES(member_id)")
    int ensure(@Param("memberId") Long memberId);

    @Select("SELECT member_id FROM oms_agent_cart_guard WHERE member_id=#{memberId} FOR UPDATE")
    Long lock(@Param("memberId") Long memberId);
}
