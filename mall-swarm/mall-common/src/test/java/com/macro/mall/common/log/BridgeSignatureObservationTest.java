package com.macro.mall.common.log;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import io.lettuce.core.RedisCommandTimeoutException;
import jakarta.servlet.http.HttpServletRequest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.parallel.ResourceLock;
import org.junit.jupiter.api.parallel.Resources;
import org.slf4j.LoggerFactory;
import org.springframework.dao.QueryTimeoutException;

import java.util.ArrayList;
import java.util.List;
import java.util.function.LongSupplier;

import static com.macro.mall.common.log.BridgeSignatureObservation.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@ResourceLock(Resources.SYSTEM_PROPERTIES)
class BridgeSignatureObservationTest {
    @Test
    void activationDefaultsOffAndPresentPropertyAlwaysOverridesEnvironment() {
        assertFalse(enabled(null, null));
        assertTrue(enabled(null, "true"));
        assertTrue(enabled(null, "TrUe"));
        assertTrue(enabled("TRUE", "false"));
        for (String property : List.of("false", "FALSE", "", "1", "yes", " true ")) {
            assertFalse(enabled(property, "true"), property);
        }
        assertFalse(enabled(null, " true "));
        assertFalse(enabled(null, "1"));
    }

    @Test
    void disabledObservationDoesNotInspectRequestReadClockOrEmitAndPreservesEveryBoolean() {
        HttpServletRequest request = mock(HttpServletRequest.class);
        var events = new ArrayList<Event>();
        LongSupplier forbiddenClock = () -> { throw new AssertionError("clock must not be read"); };
        var observation = create(request, Guard.CART, false, forbiddenClock, events::add);
        observation.phase(Phase.NONCE);
        assertTrue(observation.result(true, Outcome.ACCEPTED));
        assertFalse(observation.result(false, Outcome.NONCE_REPLAY));
        observation.error(new IllegalArgumentException("not logged"));
        observation.close();
        observation.close();
        assertTrue(events.isEmpty());
        verifyNoInteractions(request);
    }

    @Test
    void nonceTimingStartsOnceAndStopsAtDecisionRatherThanIncludingLogging() {
        var clock = new Clock(1000, 5000, 12000);
        var events = new ArrayList<Event>();
        var observation = create(request("GET"), Guard.MEMBER, true, clock, events::add);
        observation.phase(Phase.INPUT);
        observation.phase(Phase.MAC);
        observation.phase(Phase.NONCE);
        observation.phase(Phase.NONCE);
        observation.phase(Phase.INPUT); // Late diagnostic calls cannot reset the phase or nonce clock.
        assertTrue(observation.result(true, Outcome.ACCEPTED));
        observation.close();
        observation.close();
        assertEquals(3, clock.calls);
        assertEquals(1, events.size());
        Event event = events.get(0);
        assertEquals(11000, event.signatureNanos());
        assertEquals(7000, event.nonceNanos());
        assertEquals(Phase.NONCE, event.phase());
        assertEquals(Result.ACCEPTED, event.outcome());
        assertTrue(event.nonceAttempted());
        assertTrue(event.accepted());
    }

    @Test
    void wrappedNonceTimeoutIsDifferentFromLocalValidationAndReplay() {
        var events = new ArrayList<Event>();
        var timeout = create(request("POST"), Guard.STAFF, true, new Clock(10, 20, 5020), events::add);
        timeout.phase(Phase.MAC);
        timeout.phase(Phase.NONCE);
        timeout.error(new QueryTimeoutException("token must not leave the process",
                new RedisCommandTimeoutException("nonce must not leave the process")));
        timeout.close();
        Event failed = events.get(0);
        assertEquals(Result.NONCE_STORE_ERROR, failed.outcome());
        assertEquals(ErrorType.QUERY_TIMEOUT, failed.errorType());
        assertEquals(ErrorType.REDIS_COMMAND_TIMEOUT, failed.rootType());
        assertEquals(1, failed.causeDepth());
        assertFalse(failed.causeTruncated());
        assertEquals(5010, failed.signatureNanos());
        assertEquals(5000, failed.nonceNanos());

        var local = create(request("GET"), Guard.CART, true, new Clock(0, 25), events::add);
        local.phase(Phase.MAC);
        local.error(new IllegalArgumentException("raw signature is not diagnostic data"));
        local.close();
        assertEquals(Result.LOCAL_CHECK_ERROR, events.get(1).outcome());
        assertFalse(events.get(1).nonceAttempted());
        assertEquals(0, events.get(1).nonceNanos());

        var replay = create(request("PUT"), Guard.CART, true, new Clock(0, 5, 15), events::add);
        replay.phase(Phase.NONCE);
        assertFalse(replay.result(false, Outcome.NONCE_REPLAY));
        replay.close();
        assertEquals(Result.NONCE_REPLAY, events.get(2).outcome());
        assertEquals(ErrorType.NONE, events.get(2).errorType());

        var unknown = create(request("PATCH"), Guard.CART, true, new Clock(0, 10, 30), events::add);
        unknown.phase(Phase.NONCE);
        assertFalse(unknown.result(false, Outcome.NONCE_RESULT_UNKNOWN));
        unknown.close();
        assertEquals(Result.NONCE_RESULT_UNKNOWN, events.get(3).outcome());
    }

    @Test
    void malformedCauseChainsAreBoundedAndUnknownExceptionNamesAreNotRecorded() {
        var events = new ArrayList<Event>();
        var first = new IllegalStateException("secret one");
        var second = new IllegalStateException("secret two");
        first.initCause(second);
        second.initCause(first);
        var observation = create(request("GET"), Guard.STAFF, true, new Clock(1, 2), events::add);
        observation.error(first);
        observation.close();
        assertEquals(Result.LOCAL_CHECK_ERROR, events.get(0).outcome());
        assertTrue(events.get(0).causeTruncated());
        assertEquals(ErrorType.UNKNOWN_CHAIN, events.get(0).rootType());
    }

    @Test
    void requestClockAndSinkFailuresCannotChangeDecisionsOrEscapeClose() {
        var brokenRequest = mock(HttpServletRequest.class);
        when(brokenRequest.getMethod()).thenThrow(new IllegalStateException("private request"));
        var fromRequest = create(brokenRequest, Guard.CART, true, new Clock(0), ignored -> fail());
        assertTrue(fromRequest.result(true, Outcome.ACCEPTED));
        assertDoesNotThrow(fromRequest::close);

        LongSupplier brokenClock = () -> { throw new AssertionError("clock unavailable"); };
        var fromClock = create(request("GET"), Guard.CART, true, brokenClock, ignored -> fail());
        assertFalse(fromClock.result(false, Outcome.MAC_MISMATCH));
        assertDoesNotThrow(fromClock::close);

        var phaseClock = create(request("GET"), Guard.CART, true, new Clock(0), ignored -> fail());
        assertDoesNotThrow(() -> phaseClock.phase(Phase.NONCE));
        assertTrue(phaseClock.result(true, Outcome.ACCEPTED));
        assertDoesNotThrow(phaseClock::close);

        var sinkFailure = create(request("GET"), Guard.CART, true, new Clock(0, 1), ignored -> {
            throw new AssertionError("logging unavailable");
        });
        assertTrue(sinkFailure.result(true, Outcome.ACCEPTED));
        assertDoesNotThrow(sinkFailure::close);
    }

    @Test
    void publicStartReadsPropertyPerRequestAndLogsOnlySanitizedFixedFields() {
        String previous = System.getProperty(ENABLED_PROPERTY);
        Logger logger = (Logger) LoggerFactory.getLogger(BridgeSignatureObservation.class);
        Level oldLevel = logger.getLevel();
        boolean oldAdditive = logger.isAdditive();
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        logger.addAppender(appender);
        logger.setLevel(Level.INFO);
        logger.setAdditive(false);
        try {
            var disabledRequest = mock(HttpServletRequest.class);
            System.setProperty(ENABLED_PROPERTY, "false");
            try (var observation = start(disabledRequest, Guard.CART)) {
                assertFalse(observation.result(false, Outcome.INPUT_REJECTED));
            }
            verifyNoInteractions(disabledRequest);
            assertTrue(appender.list.isEmpty());

            String secret = "DO_NOT_LOG_NONCE_TOKEN_SIGNATURE_CANONICAL_URL_BODY";
            HttpServletRequest request = request("POST\r\n" + secret);
            when(request.getHeader("X-Request-ID")).thenReturn("A1B2C3D4-1111-4222-8333-0123456789AB");
            when(request.getHeader("X-Agent-Parent-Request-ID")).thenReturn("parent\r\n" + secret);
            when(request.getHeader("X-Agent-Nonce")).thenReturn(secret);
            when(request.getHeader("X-Agent-Signature")).thenReturn(secret);
            when(request.getHeader("Authorization")).thenReturn("Bearer " + secret);
            when(request.getRequestURI()).thenReturn("/private/" + secret);
            System.setProperty(ENABLED_PROPERTY, "TrUe");
            try (var observation = start(request, Guard.STAFF)) {
                observation.phase(Phase.NONCE);
                observation.error(new IllegalArgumentException(secret, new PrivateException(secret)));
            }
            assertEquals(1, appender.list.size());
            ILoggingEvent event = appender.list.get(0);
            String logged = event.getFormattedMessage();
            assertTrue(logged.contains("request_id=a1b2c3d4-1111-4222-8333-0123456789ab"));
            assertTrue(logged.contains("parent_request_id=NONE"));
            assertTrue(logged.contains("method=OTHER"));
            assertTrue(logged.contains("outcome=NONCE_STORE_ERROR"));
            assertTrue(logged.contains("error_type=ILLEGAL_ARGUMENT root_type=OTHER"));
            assertFalse(logged.contains(secret));
            assertFalse(logged.contains(PrivateException.class.getName()));
            assertNull(event.getThrowableProxy());
            for (Object argument : event.getArgumentArray()) assertFalse(argument instanceof Throwable);
            verify(request).getMethod();
            verify(request).getHeader("X-Request-ID");
            verify(request).getHeader("X-Agent-Parent-Request-ID");
            verifyNoMoreInteractions(request);
        } finally {
            if (previous == null) System.clearProperty(ENABLED_PROPERTY);
            else System.setProperty(ENABLED_PROPERTY, previous);
            logger.detachAppender(appender);
            appender.stop();
            logger.setLevel(oldLevel);
            logger.setAdditive(oldAdditive);
        }
    }

    @Test
    void validParentIdIsCanonicalAndShortUuidFormsAreDropped() {
        var events = new ArrayList<Event>();
        var request = request("GET");
        when(request.getHeader("X-Request-ID")).thenReturn("1-1-1-1-1");
        when(request.getHeader("X-Agent-Parent-Request-ID")).thenReturn("F0E1D2C3-4444-4555-8666-AABBCCDDEEFF");
        var observation = create(request, Guard.MEMBER, true, new Clock(0, 1), events::add);
        observation.result(false, Outcome.INPUT_REJECTED);
        observation.close();
        assertEquals("NONE", events.get(0).requestId());
        assertEquals("f0e1d2c3-4444-4555-8666-aabbccddeeff", events.get(0).parentRequestId());
    }

    private static HttpServletRequest request(String method) {
        HttpServletRequest request = mock(HttpServletRequest.class);
        when(request.getMethod()).thenReturn(method);
        return request;
    }

    private static final class Clock implements LongSupplier {
        private final long[] values;
        private int calls;
        private Clock(long... values) { this.values = values; }
        @Override public long getAsLong() {
            if (calls >= values.length) throw new AssertionError("unexpected clock read");
            return values[calls++];
        }
    }

    private static final class PrivateException extends RuntimeException {
        private PrivateException(String message) { super(message); }
        @Override public String getMessage() { throw new AssertionError("message must not be inspected"); }
    }
}
