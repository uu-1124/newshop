package com.macro.mall.portal.service;

import com.macro.mall.order.OrderResourceService;
import java.nio.file.Files;
import java.nio.file.Path;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import static org.mockito.Mockito.*;

/** Isolated database subprocess; exits without shutdown/rollback hooks. Never logs credentials. */
public class OutboxCrashWorker {
    public static void main(String[] args){
        try{
            String url=System.getenv("MALL_CART_TEST_JDBC");
            if(url==null||!url.matches("jdbc:mysql://127\\.0\\.0\\.1:13306/mall_agent_cart_test\\?.*"))throw new IllegalStateException("Isolated test target required");
            String mode=args[0];long oid=Long.parseLong(args[1]);Path marker=Path.of(args[2]);
            var ctx=new AnnotationConfigApplicationContext(AgentOrderDatabaseTest.Config.class);
            var db=ctx.getBean(JdbcTemplate.class);var manager=ctx.getBean(PlatformTransactionManager.class);
            var resources=ctx.getBean(OrderResourceService.class);
            if(mode.equals("claim")){
                var event=ctx.getBean(OrderBusinessOutbox.class).claim();
                if(event==null||event.orderId()!=oid)throw new IllegalStateException("Expected dedicated publication");
                Files.writeString(marker,event.eventKey()+"\n"+event.fence());
                Runtime.getRuntime().halt(73);
            }else if(mode.equals("consume")){
                var dying=mock(OrderResourceService.class);
                when(dying.closePendingOrder(oid)).thenAnswer(inv->{
                    resources.closePendingOrder(oid);
                    Files.writeString(marker,"RESOURCE_MUTATED_BEFORE_COMMIT");
                    Runtime.getRuntime().halt(73);return false;
                });
                new OrderBusinessOutbox(db,manager,dying).consume(oid);
            }
            throw new IllegalStateException("Unreached crash boundary");
        }catch(Exception error){
            System.err.println("ISOLATED_CHILD_FAILED type="+error.getClass().getSimpleName());
            System.exit(1);
        }
    }
}
