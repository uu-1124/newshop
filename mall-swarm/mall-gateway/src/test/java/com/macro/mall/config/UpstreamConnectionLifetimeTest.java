package com.macro.mall.config;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.config.YamlPropertiesFactoryBean;
import org.springframework.boot.autoconfigure.web.ServerProperties;
import org.springframework.boot.context.properties.bind.Bindable;
import org.springframework.boot.context.properties.bind.Binder;
import org.springframework.boot.context.properties.source.MapConfigurationPropertySource;
import org.springframework.cloud.gateway.config.HttpClientFactory;
import org.springframework.cloud.gateway.config.HttpClientProperties;
import org.springframework.cloud.gateway.config.HttpClientSslConfigurer;
import org.springframework.core.io.ClassPathResource;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.netty.DisposableServer;
import reactor.netty.http.client.HttpClient;
import reactor.netty.http.server.HttpServer;

import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class UpstreamConnectionLifetimeTest {
    private HttpClientProperties properties() {
        YamlPropertiesFactoryBean yaml = new YamlPropertiesFactoryBean();
        yaml.setResources(new ClassPathResource("application.yml"));
        Map<String, Object> values = new HashMap<>();
        yaml.getObject().forEach((key, value) -> values.put(key.toString(), value));
        return new Binder(new MapConfigurationPropertySource(values))
                .bind("spring.cloud.gateway.server.webflux.httpclient", Bindable.of(HttpClientProperties.class)).get();
    }

    @Test void idleUpstreamIsRetiredButActiveStreamSurvivesIdleDeadline() throws Exception {
        HttpClientProperties properties = properties();
        assertTrue(properties.getPool().getMaxIdleTime().compareTo(Duration.ofSeconds(5)) < 0);
        assertTrue(properties.getPool().getEvictionInterval().compareTo(Duration.ZERO) > 0);
        DisposableServer server = HttpServer.create().host("127.0.0.1").port(0)
                .handle((request, response) -> {
                    if (request.uri().equals("/events")) {
                        return response.header("Content-Type", "text/event-stream")
                                .sendString(Flux.interval(Duration.ofSeconds(1)).take(3).map(i -> ": heartbeat\n\n"));
                    }
                    String[] id = new String[1];
                    request.withConnection(connection -> id[0] = connection.channel().id().asLongText());
                    return response.sendString(Mono.just(id[0]));
                }).bindNow();
        ServerProperties serverProperties = new ServerProperties();
        HttpClientFactory factory = new HttpClientFactory(properties, serverProperties,
                new HttpClientSslConfigurer(properties.getSsl(), serverProperties, null), List.of());
        try {
            factory.afterPropertiesSet();
            HttpClient client = factory.getObject().baseUrl("http://127.0.0.1:" + server.port());
            String first = client.get().uri("/identity").responseSingle((r, b) -> b.asString()).block(Duration.ofSeconds(5));
            // Response body completion precedes the connection-release finalizer.
            Thread.sleep(100);
            String reused = client.get().uri("/identity").responseSingle((r, b) -> b.asString()).block(Duration.ofSeconds(5));
            assertEquals(first, reused);
            Thread.sleep(properties.getPool().getMaxIdleTime().plusMillis(400).toMillis());
            String renewed = client.get().uri("/identity").responseSingle((r, b) -> b.asString()).block(Duration.ofSeconds(5));
            assertNotEquals(first, renewed, "An idle upstream must not remain reusable past the configured deadline");
            String stream = client.get().uri("/events").responseSingle((r, b) -> b.asString()).block(Duration.ofSeconds(7));
            assertEquals(3, stream.split(": heartbeat", -1).length - 1,
                    "Eviction must not terminate an active SSE response longer than the idle deadline");
        } finally {
            factory.destroy();
            server.disposeNow();
        }
    }
}
