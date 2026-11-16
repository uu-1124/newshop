package com.macro.mall.portal.service.impl;

import cn.hutool.core.collection.CollUtil;
import com.macro.mall.mapper.OmsCartItemMapper;
import com.macro.mall.mapper.PmsProductMapper;
import com.macro.mall.mapper.PmsSkuStockMapper;
import com.macro.mall.common.exception.Asserts;
import com.macro.mall.model.PmsProduct;
import com.macro.mall.model.PmsSkuStock;
import com.macro.mall.model.OmsCartItem;
import com.macro.mall.model.OmsCartItemExample;
import com.macro.mall.model.UmsMember;
import com.macro.mall.portal.dao.PortalProductDao;
import com.macro.mall.portal.domain.CartProduct;
import com.macro.mall.portal.domain.CartPromotionItem;
import com.macro.mall.portal.service.OmsCartItemService;
import com.macro.mall.portal.service.CartGuard;
import com.macro.mall.portal.service.OmsPromotionService;
import com.macro.mall.portal.service.UmsMemberService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.CollectionUtils;

import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.stream.Collectors;

/**
 * 购物车管理Service实现类
 * Created by macro on 2018/8/2.
 */
@Service
public class OmsCartItemServiceImpl implements OmsCartItemService {
    @Autowired
    private OmsCartItemMapper cartItemMapper;
    @Autowired
    private CartGuard cartGuard;
    @Autowired
    private PortalProductDao productDao;
    @Autowired
    private OmsPromotionService promotionService;
    @Autowired
    private UmsMemberService memberService;
    @Autowired
    private PmsProductMapper productMapper;
    @Autowired
    private PmsSkuStockMapper skuStockMapper;

    @Override
    @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public int add(OmsCartItem cartItem) {
        validateItem(cartItem);
        int count;
        UmsMember currentMember =memberService.getCurrentMember();
        cartGuard.lock(currentMember.getId());
        cartItem.setMemberId(currentMember.getId());
        cartItem.setMemberNickname(currentMember.getNickname());
        cartItem.setDeleteStatus(0);
        OmsCartItem existCartItem = getCartItem(cartItem);
        if (existCartItem == null) {
            cartItem.setId(null);
            cartItem.setCreateDate(new Date());
            count = cartItemMapper.insert(cartItem);
        } else {
            long quantity = (long) existCartItem.getQuantity() + cartItem.getQuantity();
            if (existCartItem.getQuantity() <= 0 || quantity > Integer.MAX_VALUE) {
                Asserts.fail("商品数量无效，请重新选择");
            }
            existCartItem.setModifyDate(new Date());
            existCartItem.setQuantity((int) quantity);
            count = cartItemMapper.updateByPrimaryKey(existCartItem);
        }
        return count;
    }

    private void validateQuantity(Integer quantity) {
        if (quantity == null || quantity <= 0) {
            Asserts.fail("商品数量必须大于0");
        }
    }

    private void validateItem(OmsCartItem item) {
        validateQuantity(item.getQuantity());
        if (item.getProductId() == null || item.getProductSkuId() == null) {
            Asserts.fail("请选择商品及规格");
        }
        PmsProduct product = productMapper.selectByPrimaryKey(item.getProductId());
        PmsSkuStock sku = skuStockMapper.selectByPrimaryKey(item.getProductSkuId());
        if (product == null || !Integer.valueOf(0).equals(product.getDeleteStatus())
                || !Integer.valueOf(1).equals(product.getPublishStatus())
                || sku == null || !item.getProductId().equals(sku.getProductId())) {
            Asserts.fail("商品或规格已失效，请重新选择");
        }
    }

    /**
     * 根据会员id,商品id和规格获取购物车中商品
     */
    private OmsCartItem getCartItem(OmsCartItem cartItem) {
        OmsCartItemExample example = new OmsCartItemExample();
        OmsCartItemExample.Criteria criteria = example.createCriteria().andMemberIdEqualTo(cartItem.getMemberId())
                .andProductIdEqualTo(cartItem.getProductId()).andDeleteStatusEqualTo(0);
        if (cartItem.getProductSkuId()!=null) {
            criteria.andProductSkuIdEqualTo(cartItem.getProductSkuId());
        }
        List<OmsCartItem> cartItemList = cartItemMapper.selectByExample(example);
        if (!CollectionUtils.isEmpty(cartItemList)) {
            return cartItemList.get(0);
        }
        return null;
    }

    @Override
    public List<OmsCartItem> list(Long memberId) {
        OmsCartItemExample example = new OmsCartItemExample();
        example.createCriteria().andDeleteStatusEqualTo(0).andMemberIdEqualTo(memberId);
        return cartItemMapper.selectByExample(example);
    }

    @Override
    public List<CartPromotionItem> listPromotion(Long memberId, List<Long> cartIds) {
        List<OmsCartItem> cartItemList = list(memberId);
        if(CollUtil.isNotEmpty(cartIds)){
            cartItemList = cartItemList.stream().filter(item->cartIds.contains(item.getId())).collect(Collectors.toList());
        }
        List<CartPromotionItem> cartPromotionItemList = new ArrayList<>();
        if(!CollectionUtils.isEmpty(cartItemList)){
            cartPromotionItemList = promotionService.calcCartPromotion(cartItemList);
        }
        return cartPromotionItemList;
    }

    @Override
    @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public int updateQuantity(Long id, Long memberId, Integer quantity) {
        validateQuantity(quantity);
        cartGuard.lock(memberId);
        OmsCartItem cartItem = new OmsCartItem();
        cartItem.setQuantity(quantity);
        OmsCartItemExample example = new OmsCartItemExample();
        example.createCriteria().andDeleteStatusEqualTo(0)
                .andIdEqualTo(id).andMemberIdEqualTo(memberId);
        return cartItemMapper.updateByExampleSelective(cartItem, example);
    }

    @Override
    @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public int delete(Long memberId, List<Long> ids) {
        if (CollectionUtils.isEmpty(ids)) {
            return 0;
        }
        cartGuard.lock(memberId);
        OmsCartItem record = new OmsCartItem();
        record.setDeleteStatus(1);
        OmsCartItemExample example = new OmsCartItemExample();
        example.createCriteria().andIdIn(ids).andMemberIdEqualTo(memberId);
        return cartItemMapper.updateByExampleSelective(record, example);
    }

    @Override
    public CartProduct getCartProduct(Long productId) {
        return productDao.getCartProduct(productId);
    }

    @Override
    @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public int updateAttr(OmsCartItem cartItem) {
        if (cartItem.getId() == null) {
            Asserts.fail("购物车商品不存在");
        }
        Long memberId = memberService.getCurrentMember().getId();
        cartGuard.lock(memberId);
        OmsCartItem original = cartItemMapper.selectByPrimaryKey(cartItem.getId());
        if (original == null || !memberId.equals(original.getMemberId())
                || !Integer.valueOf(0).equals(original.getDeleteStatus())
                || !original.getProductId().equals(cartItem.getProductId())) {
            Asserts.fail("购物车商品不存在");
        }
        validateItem(cartItem);
        //删除原购物车信息
        OmsCartItem updateCart = new OmsCartItem();
        updateCart.setModifyDate(new Date());
        updateCart.setDeleteStatus(1);
        OmsCartItemExample example = new OmsCartItemExample();
        example.createCriteria().andIdEqualTo(cartItem.getId()).andMemberIdEqualTo(memberId)
                .andDeleteStatusEqualTo(0);
        if (cartItemMapper.updateByExampleSelective(updateCart, example) != 1) {
            Asserts.fail("购物车商品已发生变化，请刷新重试");
        }
        cartItem.setId(null);
        if (add(cartItem) <= 0) {
            Asserts.fail("修改规格失败，请重试");
        }
        return 1;
    }

    @Override
    @Transactional(isolation = org.springframework.transaction.annotation.Isolation.READ_COMMITTED)
    public int clear(Long memberId) {
        cartGuard.lock(memberId);
        OmsCartItem record = new OmsCartItem();
        record.setDeleteStatus(1);
        OmsCartItemExample example = new OmsCartItemExample();
        example.createCriteria().andMemberIdEqualTo(memberId);
        return cartItemMapper.updateByExampleSelective(record,example);
    }
}
