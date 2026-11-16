package com.macro.mall.portal.service;

import com.macro.mall.mapper.*;
import com.macro.mall.model.*;
import com.macro.mall.portal.service.impl.OmsCartItemServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import java.util.List;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class CartValidationSecurityTest {
    private final OmsCartItemServiceImpl service = new OmsCartItemServiceImpl();
    private final OmsCartItemMapper carts = mock(OmsCartItemMapper.class);
    private final PmsProductMapper products = mock(PmsProductMapper.class);
    private final PmsSkuStockMapper skus = mock(PmsSkuStockMapper.class);
    private OmsCartItem item;
    private PmsProduct product;
    private PmsSkuStock sku;
    @BeforeEach void setUp() {
        ReflectionTestUtils.setField(service,"cartGuard",org.mockito.Mockito.mock(CartGuard.class));
        ReflectionTestUtils.setField(service,"cartItemMapper",carts);
        ReflectionTestUtils.setField(service,"productMapper",products);
        ReflectionTestUtils.setField(service,"skuStockMapper",skus);
        UmsMemberService members = mock(UmsMemberService.class);
        ReflectionTestUtils.setField(service,"memberService",members);
        UmsMember member = new UmsMember(); member.setId(1L);
        when(members.getCurrentMember()).thenReturn(member);
        product = new PmsProduct(); product.setId(10L); product.setDeleteStatus(0); product.setPublishStatus(1);
        sku = new PmsSkuStock(); sku.setId(20L); sku.setProductId(10L);
        when(products.selectByPrimaryKey(10L)).thenReturn(product);
        when(skus.selectByPrimaryKey(20L)).thenReturn(sku);
        when(carts.selectByExample(any())).thenReturn(List.of());
        when(carts.insert(any())).thenReturn(1);
        item = new OmsCartItem(); item.setProductId(10L); item.setProductSkuId(20L); item.setQuantity(1);
    }
    @Test void normalAddIgnoresClientIdentity() {
        item.setId(999L); item.setMemberId(2L);
        assertEquals(1,service.add(item)); assertNull(item.getId()); assertEquals(1L,item.getMemberId());
    }
    @Test void nonPositiveAndMissingQuantitiesNeverWrite() {
        for (Integer n : new Integer[]{null,0,-1,Integer.MIN_VALUE}) {
            item.setQuantity(n); assertThrows(RuntimeException.class,()->service.add(item));
        }
        verify(carts,never()).insert(any());
    }
    @Test void mismatchedSkuNeverWrites() {
        sku.setProductId(11L); assertThrows(RuntimeException.class,()->service.add(item));
        verify(carts,never()).insert(any());
    }
    @Test void missingAndUnpublishedProductsAreRejected() {
        product.setPublishStatus(0); assertThrows(RuntimeException.class,()->service.add(item));
        when(products.selectByPrimaryKey(10L)).thenReturn(null);
        assertThrows(RuntimeException.class,()->service.add(item));
    }
    @Test void mergeOverflowCannotBecomeNegative() {
        OmsCartItem existing=new OmsCartItem(); existing.setQuantity(Integer.MAX_VALUE);
        when(carts.selectByExample(any())).thenReturn(List.of(existing));
        assertThrows(RuntimeException.class,()->service.add(item));
        verify(carts,never()).updateByPrimaryKey(any());
    }
    @Test void normalMergeStillWorks() {
        OmsCartItem existing=new OmsCartItem(); existing.setQuantity(2);
        when(carts.selectByExample(any())).thenReturn(List.of(existing));
        service.add(item); assertEquals(3,existing.getQuantity());
    }
    @Test void invalidQuantityUpdateNeverWrites() {
        assertThrows(RuntimeException.class,()->service.updateQuantity(1L,1L,-1));
        verifyNoInteractions(carts);
    }
    @Test void otherMembersCannotChangeAttributes() {
        OmsCartItem original=new OmsCartItem(); original.setMemberId(2L); original.setDeleteStatus(0);
        item.setId(30L); when(carts.selectByPrimaryKey(30L)).thenReturn(original);
        assertThrows(RuntimeException.class,()->service.updateAttr(item));
        verify(carts,never()).updateByExampleSelective(any(),any());
        verify(carts,never()).insert(any());
    }
    @Test void invalidReplacementKeepsOriginal() {
        OmsCartItem original=new OmsCartItem(); original.setMemberId(1L); original.setDeleteStatus(0); original.setProductId(10L);
        item.setId(30L); item.setProductSkuId(99L); when(carts.selectByPrimaryKey(30L)).thenReturn(original);
        assertThrows(RuntimeException.class,()->service.updateAttr(item));
        verify(carts,never()).updateByExampleSelective(any(),any());
    }
    @Test void emptyDeleteIsSafe() { assertEquals(0,service.delete(1L,List.of())); verifyNoInteractions(carts); }
}
