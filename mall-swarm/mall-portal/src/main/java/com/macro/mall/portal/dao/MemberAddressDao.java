package com.macro.mall.portal.dao;

import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/** Serializes address changes even when the member has no address yet. */
public interface MemberAddressDao {
    @Select("SELECT id FROM ums_member WHERE id=#{memberId} FOR UPDATE")
    Long lockMember(@Param("memberId") Long memberId);
}
