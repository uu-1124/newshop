package com.macro.mall.portal.service;

import com.macro.mall.mapper.*;
import com.macro.mall.model.*;
import com.macro.mall.portal.domain.OrderParam;
import com.macro.mall.portal.service.impl.OmsPortalOrderServiceImpl;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class CheckoutValidationTest {
    private final OmsPortalOrderServiceImpl service=new OmsPortalOrderServiceImpl();
    private final OmsCartItemService carts=mock(OmsCartItemService.class);
    private final UmsMemberReceiveAddressService addresses=mock(UmsMemberReceiveAddressService.class);
    private final OmsOrderMapper orders=mock(OmsOrderMapper.class);
    private final PmsProductMapper products=mock(PmsProductMapper.class);
    private final PmsSkuStockMapper skus=mock(PmsSkuStockMapper.class);
    private OrderParam param;
    private OmsCartItem item;
    @BeforeEach void setUp(){
        UmsMemberService members=mock(UmsMemberService.class);
        ReflectionTestUtils.setField(service,"cartGuard",org.mockito.Mockito.mock(CartGuard.class));
        ReflectionTestUtils.setField(service,"memberService",members);
        ReflectionTestUtils.setField(service,"cartItemService",carts);
        ReflectionTestUtils.setField(service,"memberReceiveAddressService",addresses);
        ReflectionTestUtils.setField(service,"orderMapper",orders);
        ReflectionTestUtils.setField(service,"productMapper",products);
        ReflectionTestUtils.setField(service,"skuStockMapper",skus);
        UmsMember member=new UmsMember(); member.setId(1L); when(members.getCurrentMember()).thenReturn(member);
        when(addresses.getItem(1L)).thenReturn(new UmsMemberReceiveAddress());
        item=new OmsCartItem(); item.setId(3L); item.setQuantity(1); item.setProductId(10L); item.setProductSkuId(20L);
        when(carts.list(1L)).thenReturn(List.of(item));
        param=new OrderParam();param.setMemberReceiveAddressId(1L);param.setCartIds(List.of(3L));param.setUseIntegration(0);
    }
    private void rejected(){ assertThrows(RuntimeException.class,()->service.generateOrder(param)); verifyNoInteractions(orders); verify(carts,never()).listPromotion(anyLong(),any()); }
    @Test void emptySelectionNeverOrdersEntireCart(){param.setCartIds(List.of());rejected();}
    @Test void missingSelectionIsRejected(){param.setCartIds(null);rejected();}
    @Test void duplicateSelectionIsRejected(){param.setCartIds(List.of(3L,3L));rejected();}
    @Test void staleOrForeignSelectionIsRejected(){param.setCartIds(List.of(3L,4L));rejected();}
    @Test void invalidAddressIsRejectedBeforeStockMutation(){param.setMemberReceiveAddressId(99L);rejected();verifyNoInteractions(skus);}
    @Test void negativeIntegrationIsRejected(){param.setUseIntegration(-1);rejected();}
    @Test void historicalNegativeCartCannotBeOrdered(){item.setQuantity(-1);rejected();}
    @Test void unavailableProductIsRejected(){rejected();}
    @Test void emptyConfirmationIsRejected(){assertThrows(RuntimeException.class,()->service.generateConfirmOrder(List.of()));verify(carts,never()).listPromotion(anyLong(),any());}
}
