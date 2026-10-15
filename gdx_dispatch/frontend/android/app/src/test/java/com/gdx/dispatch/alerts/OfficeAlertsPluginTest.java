package com.gdx.dispatch.alerts;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import android.content.SharedPreferences;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.Test;

public class OfficeAlertsPluginTest {

    // Every reason the server accepts (routers/device_alerts.py
    // DeviceAlertsRevokeRequest) must reach it unchanged.
    @Test
    public void everyServerReasonSurvives() {
        for (String reason : new String[] {"disabled", "logout", "rotated", "user_changed"}) {
            assertEquals("{\"reason\":\"" + reason + "\"}", OfficeAlertsPlugin.revokeBody(reason));
        }
    }

    @Test
    public void nothingCanBreakOutOfTheString() {
        // Quotes, commas and colons go; letters stay.
        assertEquals("{\"reason\":\"logxout\"}", OfficeAlertsPlugin.revokeBody("log\",\"x\":\"out"));
    }

    // A server released before scopes ignores ?scope=planner and answers with
    // the office feed: GDX Planner still posts only planner reminders.
    @Test
    public void thePlannerAppPostsOnlyPlannerReminders() {
        assertTrue(OfficeAlertsService.belongsInScope("planner", "planner_reminder"));
        // Same rule as the server's scope: any planner_ category, not one name.
        assertTrue(OfficeAlertsService.belongsInScope("planner", "planner_assigned"));
        assertFalse(OfficeAlertsService.belongsInScope("planner", null));
        assertFalse(OfficeAlertsService.belongsInScope("planner", "planner"));
        assertFalse(OfficeAlertsService.belongsInScope("planner", "sms"));
        assertFalse(OfficeAlertsService.belongsInScope("planner", "payment"));
        assertFalse(OfficeAlertsService.belongsInScope("planner", ""));
        assertTrue(OfficeAlertsService.belongsInScope("office", "sms"));
        assertTrue(OfficeAlertsService.belongsInScope("office", "lead"));
    }

    // The installed app decides its feed: only GDX Planner's package gets the
    // planner feed, whatever the web layer believes it is.
    @Test
    public void scopeComesFromThePackage() {
        assertEquals("planner", OfficeAlertsService.scopeForPackage("com.gdx.planner"));
        assertEquals("office", OfficeAlertsService.scopeForPackage("com.gdx.dispatch"));
        assertEquals("office", OfficeAlertsService.scopeForPackage(null));
    }

    @Test
    public void feedUrlCarriesTheScope() throws Exception {
        assertEquals("https://h/api/notifications/device-alerts/feed?wait=25&scope=planner&after=a%3Ab",
            OfficeAlertsService.feedUrl("https://h", "a:b", "planner"));
        assertEquals("https://h/api/notifications/device-alerts/feed?wait=25&scope=office",
            OfficeAlertsService.feedUrl("https://h", null, "office"));
    }

    // A response read just before stop() must not write the session back.
    @Test
    public void stopThenLateHandleLeavesPrefsEmpty() {
        FakePrefs p = session("old-token");
        OfficeAlertsService.clearAll(p);
        AtomicInteger posted = new AtomicInteger();

        assertFalse(OfficeAlertsService.recordIfCurrent(p, "old-token", false,
            posted::incrementAndGet, "c-late", "a,b"));
        assertTrue(p.getAll().isEmpty());
        assertEquals(0, posted.get());
    }

    // ...nor land in the next session started after it.
    @Test
    public void lateHandleNeverWritesIntoANewSession() {
        FakePrefs p = session("old-token");
        OfficeAlertsService.clearAll(p);
        p.edit().putString(OfficeAlertsService.KEY_SERVER_URL, "https://x")
            .putString(OfficeAlertsService.KEY_TOKEN, "new-token").commit();
        AtomicInteger posted = new AtomicInteger();

        assertFalse(OfficeAlertsService.recordIfCurrent(p, "old-token", false,
            posted::incrementAndGet, "c-late", "a,b"));
        assertFalse(p.contains(OfficeAlertsService.KEY_CURSOR));
        assertFalse(p.contains(OfficeAlertsService.KEY_SEEN));
        assertEquals(0, posted.get());
    }

    @Test
    public void aStoppingServiceWritesNothing() {
        FakePrefs p = session("t");
        AtomicInteger posted = new AtomicInteger();
        assertFalse(OfficeAlertsService.recordIfCurrent(p, "t", true, posted::incrementAndGet, "c2", "a,b"));
        assertEquals("c1", p.getString(OfficeAlertsService.KEY_CURSOR, null));
        assertEquals("a", p.getString(OfficeAlertsService.KEY_SEEN, null));
        assertEquals(0, posted.get());
    }

    @Test
    public void theLiveSessionStillRecords() {
        FakePrefs p = session("t");
        AtomicInteger posted = new AtomicInteger();
        assertTrue(OfficeAlertsService.recordIfCurrent(p, "t", false, posted::incrementAndGet, "c2", "a,b"));
        assertEquals("c2", p.getString(OfficeAlertsService.KEY_CURSOR, null));
        assertEquals("a,b", p.getString(OfficeAlertsService.KEY_SEEN, null));
        assertEquals(1, posted.get());
    }

    // The check and the write are one step: a stop() that clears while the
    // worker waits for the lock wins, it is not check-then-act.
    @Test
    public void aClearWhileTheWorkerWaitsWins() throws Exception {
        FakePrefs p = session("t");
        AtomicBoolean recorded = new AtomicBoolean(true);
        Thread worker = new Thread(() ->
            recorded.set(OfficeAlertsService.recordIfCurrent(p, "t", false, () -> {}, "c", "a")));
        synchronized (OfficeAlertsService.PREFS_LOCK) {
            worker.start();
            long deadline = System.currentTimeMillis() + 5_000;
            while (worker.getState() != Thread.State.BLOCKED) {
                assertTrue("worker never reached the lock", System.currentTimeMillis() < deadline);
                Thread.sleep(5);
            }
            OfficeAlertsService.clearAll(p);
        }
        worker.join(5_000);
        assertFalse(recorded.get());
        assertTrue(p.getAll().isEmpty());
    }

    // ...and stop()'s clear waits out a worker write already under way.
    @Test
    public void clearAllWaitsForAWorkerWriteInProgress() throws Exception {
        FakePrefs p = session("t");
        Thread stopper = new Thread(() -> OfficeAlertsService.clearAll(p));
        synchronized (OfficeAlertsService.PREFS_LOCK) {
            stopper.start();
            long deadline = System.currentTimeMillis() + 5_000;
            while (stopper.getState() != Thread.State.BLOCKED
                && stopper.getState() != Thread.State.TERMINATED) {
                assertTrue("stopper never reached the lock", System.currentTimeMillis() < deadline);
                Thread.sleep(5);
            }
            assertEquals(Thread.State.BLOCKED, stopper.getState());
            assertEquals("t", p.getString(OfficeAlertsService.KEY_TOKEN, ""));
        }
        stopper.join(5_000);
        assertTrue(p.getAll().isEmpty());
    }

    // A refusal of a token a new start() replaced must not clear the new one.
    @Test
    public void aRefusalOfAReplacedTokenClearsNothing() {
        FakePrefs p = session("new-token");
        assertFalse(OfficeAlertsService.clearIfCurrent(p, "old-token"));
        assertEquals("new-token", p.getString(OfficeAlertsService.KEY_TOKEN, ""));
        assertTrue(OfficeAlertsService.clearIfCurrent(p, "new-token"));
        assertTrue(p.getAll().isEmpty());
    }

    private static FakePrefs session(String token) {
        FakePrefs p = new FakePrefs();
        p.edit().putString(OfficeAlertsService.KEY_SERVER_URL, "https://x")
            .putString(OfficeAlertsService.KEY_TOKEN, token)
            .putString(OfficeAlertsService.KEY_CURSOR, "c1")
            .putString(OfficeAlertsService.KEY_SEEN, "a").commit();
        return p;
    }

    /** In-memory SharedPreferences: edits apply on commit()/apply(), clear() first. */
    private static final class FakePrefs implements SharedPreferences {
        private final Map<String, Object> values = new HashMap<>();

        @Override public synchronized Map<String, ?> getAll() { return new HashMap<>(values); }
        @Override public synchronized String getString(String k, String d) {
            return values.containsKey(k) ? (String) values.get(k) : d;
        }
        @SuppressWarnings("unchecked")
        @Override public synchronized Set<String> getStringSet(String k, Set<String> d) {
            return values.containsKey(k) ? (Set<String>) values.get(k) : d;
        }
        @Override public synchronized int getInt(String k, int d) {
            return values.containsKey(k) ? (Integer) values.get(k) : d;
        }
        @Override public synchronized long getLong(String k, long d) {
            return values.containsKey(k) ? (Long) values.get(k) : d;
        }
        @Override public synchronized float getFloat(String k, float d) {
            return values.containsKey(k) ? (Float) values.get(k) : d;
        }
        @Override public synchronized boolean getBoolean(String k, boolean d) {
            return values.containsKey(k) ? (Boolean) values.get(k) : d;
        }
        @Override public synchronized boolean contains(String k) { return values.containsKey(k); }
        @Override public Editor edit() { return new FakeEditor(); }
        @Override public void registerOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener l) {}
        @Override public void unregisterOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener l) {}

        private final class FakeEditor implements Editor {
            private final Map<String, Object> puts = new HashMap<>();
            private final Set<String> removes = new java.util.HashSet<>();
            private boolean clear;

            @Override public Editor putString(String k, String v) { puts.put(k, v); return this; }
            @Override public Editor putStringSet(String k, Set<String> v) { puts.put(k, v); return this; }
            @Override public Editor putInt(String k, int v) { puts.put(k, v); return this; }
            @Override public Editor putLong(String k, long v) { puts.put(k, v); return this; }
            @Override public Editor putFloat(String k, float v) { puts.put(k, v); return this; }
            @Override public Editor putBoolean(String k, boolean v) { puts.put(k, v); return this; }
            @Override public Editor remove(String k) { removes.add(k); return this; }
            @Override public Editor clear() { clear = true; return this; }
            @Override public boolean commit() {
                synchronized (FakePrefs.this) {
                    if (clear) values.clear();
                    for (String k : removes) values.remove(k);
                    values.putAll(puts);
                }
                return true;
            }
            @Override public void apply() { commit(); }
        }
    }
}
