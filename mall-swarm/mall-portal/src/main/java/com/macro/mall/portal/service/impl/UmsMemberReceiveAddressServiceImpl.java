package com.macro.mall.portal.service.impl;

import com.macro.mall.common.api.ResultCode;
import com.macro.mall.common.exception.Asserts;
import com.macro.mall.mapper.UmsMemberReceiveAddressMapper;
import com.macro.mall.model.UmsMember;
import com.macro.mall.model.UmsMemberReceiveAddress;
import com.macro.mall.model.UmsMemberReceiveAddressExample;
import com.macro.mall.portal.dao.MemberAddressDao;
import com.macro.mall.portal.service.UmsMemberReceiveAddressService;
import com.macro.mall.portal.service.UmsMemberService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.util.CollectionUtils;

import java.util.List;

/**
 * 用户地址管理Service实现类
 * Created by macro on 2018/8/28.
 */
@Service
public class UmsMemberReceiveAddressServiceImpl implements UmsMemberReceiveAddressService {
    @Autowired
    private UmsMemberService memberService;
    @Autowired
    private UmsMemberReceiveAddressMapper addressMapper;
    @Autowired
    private MemberAddressDao memberAddressDao;

    @Override
    public int add(UmsMemberReceiveAddress address) {
        validateDefaultStatus(address.getDefaultStatus());
        UmsMember currentMember = memberService.getCurrentMember();
        lockMember(currentMember.getId());
        address.setId(null);
        address.setMemberId(currentMember.getId());
        if (address.getDefaultStatus() == null) {
            address.setDefaultStatus(0);
        }
        if (Integer.valueOf(1).equals(address.getDefaultStatus())) {
            clearDefault(currentMember.getId());
        }
        return addressMapper.insert(address);
    }

    @Override
    public int delete(Long id) {
        UmsMember currentMember = memberService.getCurrentMember();
        lockMember(currentMember.getId());
        UmsMemberReceiveAddressExample example = new UmsMemberReceiveAddressExample();
        example.createCriteria().andMemberIdEqualTo(currentMember.getId()).andIdEqualTo(id);
        return addressMapper.deleteByExample(example);
    }

    @Override
    public int update(Long id, UmsMemberReceiveAddress address) {
        validateDefaultStatus(address.getDefaultStatus());
        address.setId(null);
        UmsMember currentMember = memberService.getCurrentMember();
        lockMember(currentMember.getId());
        address.setMemberId(currentMember.getId());
        UmsMemberReceiveAddressExample example = new UmsMemberReceiveAddressExample();
        example.createCriteria().andMemberIdEqualTo(currentMember.getId()).andIdEqualTo(id);
        if (addressMapper.selectByExample(example).isEmpty()) {
            return 0;
        }
        if (Integer.valueOf(1).equals(address.getDefaultStatus())) {
            clearDefault(currentMember.getId());
        }
        return addressMapper.updateByExampleSelective(address,example);
    }

    private void lockMember(Long memberId) {
        if (memberAddressDao.lockMember(memberId) == null) {
            Asserts.fail("会员不存在");
        }
    }

    private void validateDefaultStatus(Integer status) {
        if (status != null && status != 0 && status != 1) {
            Asserts.fail(ResultCode.VALIDATE_FAILED);
        }
    }

    private void clearDefault(Long memberId) {
        UmsMemberReceiveAddress record = new UmsMemberReceiveAddress();
        record.setDefaultStatus(0);
        UmsMemberReceiveAddressExample example = new UmsMemberReceiveAddressExample();
        example.createCriteria().andMemberIdEqualTo(memberId).andDefaultStatusEqualTo(1);
        addressMapper.updateByExampleSelective(record, example);
    }

    @Override
    public List<UmsMemberReceiveAddress> list() {
        UmsMember currentMember = memberService.getCurrentMember();
        UmsMemberReceiveAddressExample example = new UmsMemberReceiveAddressExample();
        example.createCriteria().andMemberIdEqualTo(currentMember.getId());
        return addressMapper.selectByExample(example);
    }

    @Override
    public UmsMemberReceiveAddress getItem(Long id) {
        UmsMember currentMember = memberService.getCurrentMember();
        UmsMemberReceiveAddressExample example = new UmsMemberReceiveAddressExample();
        example.createCriteria().andMemberIdEqualTo(currentMember.getId()).andIdEqualTo(id);
        List<UmsMemberReceiveAddress> addressList = addressMapper.selectByExample(example);
        if(!CollectionUtils.isEmpty(addressList)){
            return addressList.get(0);
        }
        return null;
    }
}
