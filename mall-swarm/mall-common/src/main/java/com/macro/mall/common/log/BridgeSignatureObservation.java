package com.macro.mall.common.log;

import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.IdentityHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.function.Consumer;
import java.util.function.LongSupplier;

/** Optional, bounded diagnostics. This class never decides whether a request is trusted. */
public final class BridgeSignatureObservation implements AutoCloseable {
    public static final String ENABLED_PROPERTY = "mall.agent.bridge.observability.enabled";
    public static final String ENABLED_ENV = "MALL_AGENT_BRIDGE_OBSERVABILITY_ENABLED";

    public enum Guard { CART, MEMBER, STAFF }
    public enum Phase { INPUT, MAC, NONCE }
    public enum Outcome { ACCEPTED, INPUT_REJECTED, MAC_MISMATCH, NONCE_REPLAY, NONCE_RESULT_UNKNOWN }

    enum Method { GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS, TRACE, CONNECT, OTHER }
    enum Result {
        ACCEPTED, INPUT_REJECTED, MAC_MISMATCH, NONCE_REPLAY, NONCE_RESULT_UNKNOWN,
        NONCE_STORE_ERROR, LOCAL_CHECK_ERROR, UNREPORTED
    }
    enum ErrorType {
        NONE, REDIS_COMMAND_TIMEOUT, QUERY_TIMEOUT, REDIS_CONNECTION_FAILURE, REDIS_CONNECTION,
        REDIS_SYSTEM, REDIS_COMMAND_EXECUTION, REDIS_GENERIC, DATA_ACCESS_RESOURCE,
        ILLEGAL_ARGUMENT, NUMBER_FORMAT, DATETIME_PARSE, CRYPTOGRAPHIC,
        SOCKET_TIMEOUT, CONNECT_EXCEPTION, IO_EXCEPTION, OTHER, UNKNOWN_CHAIN
    }

    record Event(Guard guard, Method method, Phase phase, Result outcome, boolean accepted,
                 String requestId, String parentRequestId, long signatureNanos, long nonceNanos,
                 boolean nonceAttempted, ErrorType errorType, ErrorType rootType,
                 int causeDepth, boolean causeTruncated) { }

    private static final String NONE = "NONE";
    private static final int MAX_CAUSES = 16;
    private static final BridgeSignatureObservation DISABLED = new BridgeSignatureObservation();
    private static final Map<String, ErrorType> ERRORS = Map.ofEntries(
            Map.entry("io.lettuce.core.RedisCommandTimeoutException", ErrorType.REDIS_COMMAND_TIMEOUT),
            Map.entry("org.springframework.dao.QueryTimeoutException", ErrorType.QUERY_TIMEOUT),
            Map.entry("org.springframework.data.redis.RedisConnectionFailureException", ErrorType.REDIS_CONNECTION_FAILURE),
            Map.entry("io.lettuce.core.RedisConnectionException", ErrorType.REDIS_CONNECTION),
            Map.entry("org.springframework.data.redis.RedisSystemException", ErrorType.REDIS_SYSTEM),
            Map.entry("io.lettuce.core.RedisCommandExecutionException", ErrorType.REDIS_COMMAND_EXECUTION),
            Map.entry("io.lettuce.core.RedisException", ErrorType.REDIS_GENERIC),
            Map.entry("org.springframework.dao.DataAccessResourceFailureException", ErrorType.DATA_ACCESS_RESOURCE),
            Map.entry("java.lang.IllegalArgumentException", ErrorType.ILLEGAL_ARGUMENT),
            Map.entry("java.lang.NumberFormatException", ErrorType.NUMBER_FORMAT),
            Map.entry("java.time.format.DateTimeParseException", ErrorType.DATETIME_PARSE),
            Map.entry("java.security.InvalidKeyException", ErrorType.CRYPTOGRAPHIC),
            Map.entry("java.security.NoSuchAlgorithmException", ErrorType.CRYPTOGRAPHIC),
            Map.entry("java.net.SocketTimeoutException", ErrorType.SOCKET_TIMEOUT),
            Map.entry("java.net.ConnectException", ErrorType.CONNECT_EXCEPTION),
            Map.entry("java.io.IOException", ErrorType.IO_EXCEPTION)
    );

    private final boolean enabled;
    private final LongSupplier clock;
    private final Consumer<Event> sink;
    private final Guard guard;
    private final Method method;
    private final String requestId;
    private final String parentRequestId;
    private final long started;
    private Phase phase = Phase.INPUT;
    private Result outcome = Result.UNREPORTED;
    private ErrorType errorType = ErrorType.NONE;
    private ErrorType rootType = ErrorType.NONE;
    private int causeDepth;
    private boolean causeTruncated;
    private boolean accepted;
    private boolean nonceAttempted;
    private long nonceStarted;
    private long finished;
    private boolean terminal;
    private boolean closed;
    private boolean failed;

    private BridgeSignatureObservation() {
        enabled = false;
        clock = null;
        sink = null;
        guard = null;
        method = Method.OTHER;
        requestId = NONE;
        parentRequestId = NONE;
        started = 0;
    }

    private BridgeSignatureObservation(HttpServletRequest request, Guard guard,
                                       LongSupplier clock, Consumer<Event> sink) {
        this.enabled = true;
        this.clock = clock;
        this.sink = sink;
        this.started = clock.getAsLong();
        this.guard = java.util.Objects.requireNonNull(guard);
        this.method = method(request.getMethod());
        this.requestId = uuid(request.getHeader("X-Request-ID"));
        this.parentRequestId = uuid(request.getHeader("X-Agent-Parent-Request-ID"));
    }

    /** A present JVM property overrides the environment, including explicit false. */
    public static BridgeSignatureObservation start(HttpServletRequest request, Guard guard) {
        try {
            String property = System.getProperty(ENABLED_PROPERTY);
            boolean enabled = enabled(property, property == null ? System.getenv(ENABLED_ENV) : null);
            return create(request, guard, enabled, System::nanoTime, Log::write);
        } catch (Throwable ignored) {
            return DISABLED;
        }
    }

    static boolean enabled(String property, String environment) {
        return "true".equalsIgnoreCase(property != null ? property : environment);
    }

    static BridgeSignatureObservation create(HttpServletRequest request, Guard guard, boolean enabled,
                                              LongSupplier clock, Consumer<Event> sink) {
        if (!enabled) return DISABLED;
        try {
            return new BridgeSignatureObservation(request, guard, clock, sink);
        } catch (Throwable ignored) {
            return DISABLED;
        }
    }

    public void phase(Phase next) {
        if (!enabled || failed || closed || terminal || next == null) return;
        try {
            if (next.ordinal() < phase.ordinal()) return;
            phase = next;
            if (next == Phase.NONCE && !nonceAttempted) {
                nonceStarted = clock.getAsLong();
                nonceAttempted = true;
            }
        } catch (Throwable ignored) {
            failed = true;
        }
    }

    /** Always returns the original decision, even when diagnostics fail. */
    public boolean result(boolean value, Outcome result) {
        if (enabled && !failed && !closed && !terminal) {
            try {
                accepted = value;
                outcome = value ? Result.ACCEPTED
                        : result == null || result == Outcome.ACCEPTED ? Result.UNREPORTED : Result.valueOf(result.name());
                finish();
            } catch (Throwable ignored) {
                failed = true;
            }
        }
        return value;
    }

    public void error(Exception error) {
        if (!enabled || failed || closed || terminal) return;
        try {
            accepted = false;
            outcome = phase == Phase.NONCE ? Result.NONCE_STORE_ERROR : Result.LOCAL_CHECK_ERROR;
            errorType = type(error);
            Throwable cursor = error;
            var seen = new IdentityHashMap<Throwable, Boolean>();
            while (cursor != null) {
                seen.put(cursor, true);
                Throwable next = cursor.getCause();
                if (next == null) break;
                if (causeDepth >= MAX_CAUSES || seen.containsKey(next)) {
                    causeTruncated = true;
                    break;
                }
                causeDepth++;
                cursor = next;
            }
            rootType = causeTruncated ? ErrorType.UNKNOWN_CHAIN : type(cursor);
            finish();
        } catch (Throwable ignored) {
            failed = true;
        }
    }

    private void finish() {
        finished = clock.getAsLong();
        terminal = true;
    }

    @Override
    public void close() {
        if (!enabled || failed || closed) return;
        closed = true;
        try {
            if (!terminal) finish();
            sink.accept(new Event(guard, method, phase, outcome, accepted, requestId, parentRequestId,
                    elapsed(finished, started), nonceAttempted ? elapsed(finished, nonceStarted) : 0,
                    nonceAttempted, errorType, rootType, causeDepth, causeTruncated));
        } catch (Throwable ignored) {
            // Diagnostics, including logger failures, never change the caller's decision.
        }
    }

    private static long elapsed(long end, long begin) {
        return Math.max(0, end - begin);
    }

    private static ErrorType type(Throwable error) {
        return error == null ? ErrorType.NONE : ERRORS.getOrDefault(error.getClass().getName(), ErrorType.OTHER);
    }

    private static String uuid(String raw) {
        if (raw == null || raw.length() != 36) return NONE;
        try {
            String canonical = UUID.fromString(raw).toString();
            return canonical.equalsIgnoreCase(raw) ? canonical : NONE;
        } catch (IllegalArgumentException ignored) {
            return NONE;
        }
    }

    private static Method method(String raw) {
        if (raw == null || raw.length() > 8) return Method.OTHER;
        try {
            return Method.valueOf(raw.toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException ignored) {
            return Method.OTHER;
        }
    }

    private static final class Log {
        private static final Logger LOGGER = LoggerFactory.getLogger(BridgeSignatureObservation.class);

        private static void write(Event event) {
            LOGGER.info("bridge_signature guard={} method={} phase={} outcome={} accepted={} request_id={} parent_request_id={} signature_us={} nonce_us={} nonce_attempted={} error_type={} root_type={} cause_depth={} cause_truncated={}",
                    event.guard(), event.method(), event.phase(), event.outcome(), event.accepted(),
                    event.requestId(), event.parentRequestId(), event.signatureNanos() / 1000,
                    event.nonceNanos() / 1000, event.nonceAttempted(), event.errorType(), event.rootType(),
                    event.causeDepth(), event.causeTruncated());
        }
    }
}
