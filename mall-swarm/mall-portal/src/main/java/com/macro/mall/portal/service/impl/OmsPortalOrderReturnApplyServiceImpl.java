package com.macro.mall.portal.service.impl;

import com.macro.mall.common.exception.Asserts;
import com.macro.mall.mapper.OmsOrderItemMapper;
import com.macro.mall.mapper.OmsOrderReturnApplyMapper;
import com.macro.mall.model.*;
import com.macro.mall.portal.dao.PortalOrderDao;
import com.macro.mall.portal.domain.OmsOrderReturnApplyParam;
import com.macro.mall.portal.service.OmsPortalOrderReturnApplyService;
import com.macro.mall.portal.service.UmsMemberService;
import org.springframework.beans.BeanUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Date;
import java.util.List;
import java.util.Objects;

/**
 * 订单退货管理Service实现类
 * Created by macro on 2018/10/17.
 */
@Service
public class OmsPortalOrderReturnApplyServiceImpl implements OmsPortalOrderReturnApplyService {
    @Autowired
    private OmsOrderReturnApplyMapper returnApplyMapper;
    @Autowired
    private PortalOrderDao orderDao;
    @Autowired
    private OmsOrderItemMapper orderItemMapper;
    @Autowired
    private UmsMemberService memberService;
    @Override
    @Transactional(rollbackFor = Exception.class)
    public int create(OmsOrderReturnApplyParam returnApply) {
        if (returnApply.getOrderId() == null || returnApply.getProductId() == null
                || returnApply.getProductCount() == null || returnApply.getProductCount() <= 0) {
            Asserts.fail("退货参数不正确");
        }
        // Lock before other database reads so concurrent requests see the previous application.
        OmsOrder order = orderDao.lockOrderForReturn(returnApply.getOrderId());
        UmsMember member = memberService.getCurrentMember();
        if (order == null || !Objects.equals(order.getMemberId(), member.getId())
                || !Objects.equals(order.getDeleteStatus(), 0)) {
            Asserts.fail("订单不存在");
        }
        if (!Objects.equals(order.getStatus(), 2) && !Objects.equals(order.getStatus(), 3)) {
            Asserts.fail("订单状态不允许申请退货");
        }
        OmsOrderItemExample itemExample = new OmsOrderItemExample();
        itemExample.createCriteria().andOrderIdEqualTo(order.getId())
                .andProductIdEqualTo(returnApply.getProductId());
        List<OmsOrderItem> items = orderItemMapper.selectByExample(itemExample);
        // The existing API has no orderItemId; do not guess a SKU or its price.
        if (items.size() != 1) {
            Asserts.fail("无法唯一确定退货商品，请联系商家处理");
        }
        OmsOrderItem item = items.get(0);
        OmsOrderReturnApplyExample applyExample = new OmsOrderReturnApplyExample();
        applyExample.createCriteria().andOrderIdEqualTo(order.getId())
                .andProductIdEqualTo(item.getProductId());
        long appliedCount = 0;
        for (OmsOrderReturnApply previous : returnApplyMapper.selectByExample(applyExample)) {
            if (!Objects.equals(previous.getStatus(), 3)) {
                if (previous.getProductCount() == null || previous.getProductCount() <= 0) {
                    Asserts.fail("历史退货数据异常，请联系商家处理");
                }
                appliedCount += previous.getProductCount();
            }
        }
        if (item.getProductQuantity() == null
                || appliedCount + returnApply.getProductCount() > item.getProductQuantity()
                || item.getProductPrice() == null || item.getRealAmount() == null
                || item.getRealAmount().signum() < 0) {
            Asserts.fail("退货数量或订单金额不正确");
        }
        OmsOrderReturnApply realApply = new OmsOrderReturnApply();
        BeanUtils.copyProperties(returnApply,realApply);
        realApply.setOrderSn(order.getOrderSn());
        realApply.setMemberUsername(order.getMemberUsername());
        realApply.setProductPic(item.getProductPic());
        realApply.setProductName(item.getProductName());
        realApply.setProductBrand(item.getProductBrand());
        realApply.setProductAttr(item.getProductAttr());
        realApply.setProductPrice(item.getProductPrice());
        realApply.setProductRealPrice(item.getRealAmount());
        realApply.setCreateTime(new Date());
        realApply.setStatus(0);
        return returnApplyMapper.insert(realApply);
    }
}
