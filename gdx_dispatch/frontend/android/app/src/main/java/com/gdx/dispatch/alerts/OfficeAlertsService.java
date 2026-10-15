package com.gdx.dispatch.alerts;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;
import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import com.gdx.dispatch.MainActivity;
import com.gdx.dispatch.R;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Office alerts: long-polls the server's bell feed and posts each new bell row
 * as an Android notification, with the app closed and the phone locked.
 *
 * No Firebase: self-hosted installs
 * have no push service, so a foreground service holds the connection open.
 * The credential is an opaque device token minted by the SPA. A 401 means the
 * token is unknown, revoked or expired, or its user is inactive, and the
 * service then turns itself off. A 403 (module off, not an office user) is
 * retried with backoff: the server's module and permission checks answer a
 * database error as "off", so a 403 can be a blip. It turns the service off
 * once 403s span an hour with no other HTTP answer in between (network
 * errors do not reset that clock).
 */
public class OfficeAlertsService extends Service {
    private static final String TAG = "OfficeAlertsService";

    public static final String ACTION_START = "ACTION_START";
    public static final String ACTION_STOP = "ACTION_STOP";
    public static final String EXTRA_CATEGORY = "gdx_office_alert_category";

    static final String PREFS = "gdx_office_alerts";
    static final String KEY_SERVER_URL = "server_url";
    static final String KEY_TOKEN = "token";
    static final String KEY_TOKEN_SAVED_AT = "token_saved_at";
    static final String KEY_CURSOR = "cursor";
    static final String KEY_SEEN = "seen";

    static final String TOKEN_HEADER = "X-Device-Alerts-Token";
    static final String CHANNEL_STATUS = "gdx_office_alerts_status";
    static final String CHANNEL_ALERTS = "gdx_office_alerts";
    private static final int STATUS_NOTIFICATION_ID = 9101;
    private static final int TURNED_OFF_NOTIFICATION_ID = 9102;

    private static final int WAIT_SECONDS = 25;
    // Well past the server's 25 s hold, so a slow answer is not a timeout.
    private static final int READ_TIMEOUT_MS = 45_000;
    private static final int CONNECT_TIMEOUT_MS = 15_000;
    private static final long BACKOFF_MIN_MS = 5_000;
    private static final long BACKOFF_MAX_MS = 60_000;
    private static final long FORBIDDEN_GIVE_UP_MS = 60 * 60_000;
    private static final int SEEN_LIMIT = 200;

    private static volatile boolean running = false;

    private final Object wakeSignal = new Object();
    private volatile boolean stopping = false;
    private volatile HttpURLConnection inFlight;
    private Thread worker;
    private ConnectivityManager.NetworkCallback networkCallback;

    public static boolean isServiceRunning() {
        return running;
    }

    // The worker reads the token, holds a long-poll, then writes; stop() or a
    // new start() can replace the prefs in between. Each of those writes, and
    // every worker write, checks and writes under this one lock.
    static final Object PREFS_LOCK = new Object();

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean isCurrent(SharedPreferences p, String token) {
        return !token.isEmpty() && token.equals(p.getString(KEY_TOKEN, ""));
    }

    /** stop(): forget the session, after any worker write already under way. */
    static void clearAll(SharedPreferences p) {
        synchronized (PREFS_LOCK) {
            p.edit().clear().commit();
        }
    }

    /**
     * Posts a response's alerts and saves its cursor and seen list, unless the
     * session that fetched it is over: stopped, or its token replaced.
     */
    static boolean recordIfCurrent(SharedPreferences p, String token, boolean stopping,
                                   Runnable postAlerts, String cursor, String seen) {
        synchronized (PREFS_LOCK) {
            if (stopping || !isCurrent(p, token)) return false;
            postAlerts.run();
            p.edit().putString(KEY_CURSOR, cursor).putString(KEY_SEEN, seen).apply();
            return true;
        }
    }

    /** A refusal of a token start() has already replaced is not a turn-off. */
    static boolean clearIfCurrent(SharedPreferences p, String token) {
        synchronized (PREFS_LOCK) {
            if (!isCurrent(p, token)) return false;
            p.edit().clear().commit();
            return true;
        }
    }

    static boolean hasCredentials(Context ctx) {
        SharedPreferences p = prefs(ctx);
        return !p.getString(KEY_SERVER_URL, "").isEmpty() && !p.getString(KEY_TOKEN, "").isEmpty();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createChannels(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            shutdown();
            return START_NOT_STICKY;
        }
        try {
            startForegroundWithStatus();
        } catch (Exception e) {
            Log.e(TAG, "Could not enter the foreground: " + e.getMessage(), e);
            shutdown();
            return START_NOT_STICKY;
        }
        if (!hasCredentials(this)) {
            shutdown();
            return START_NOT_STICKY;
        }
        running = true;
        stopping = false;
        registerNetworkCallback();
        if (worker == null || !worker.isAlive()) {
            worker = new Thread(this::loop, "office-alerts");
            worker.start();
        }
        return START_STICKY;
    }

    static void createChannels(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel status = new NotificationChannel(
            CHANNEL_STATUS, "Phone alerts status", NotificationManager.IMPORTANCE_LOW
        );
        status.setDescription("Shows that office alerts are on for this phone");
        status.setShowBadge(false);
        nm.createNotificationChannel(status);

        NotificationChannel alerts = new NotificationChannel(
            CHANNEL_ALERTS, "Office alerts", NotificationManager.IMPORTANCE_HIGH
        );
        alerts.setDescription("New customer texts, payments, leads and other office notifications");
        alerts.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
        nm.createNotificationChannel(alerts);
    }

    private PendingIntent openAppIntent(int requestCode, @Nullable String category) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        if (category != null) open.putExtra(EXTRA_CATEGORY, category);
        return PendingIntent.getActivity(
            this,
            requestCode,
            open,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT
        );
    }

    private void startForegroundWithStatus() {
        Notification notification = new NotificationCompat.Builder(this, CHANNEL_STATUS)
            .setContentTitle("GDX Dispatch")
            .setContentText("Phone alerts are on")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(openAppIntent(STATUS_NOTIFICATION_ID, null))
            .build();
        int type = 0;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            type = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE;
        }
        ServiceCompat.startForeground(this, STATUS_NOTIFICATION_ID, notification, type);
    }

    // A dropped network sends the loop into backoff; a network coming back
    // wakes it at once instead of waiting the backoff out.
    private void registerNetworkCallback() {
        if (networkCallback != null || Build.VERSION.SDK_INT < Build.VERSION_CODES.N) return;
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        if (cm == null) return;
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                synchronized (wakeSignal) {
                    wakeSignal.notifyAll();
                }
            }
        };
        try {
            cm.registerDefaultNetworkCallback(networkCallback);
        } catch (Exception e) {
            Log.w(TAG, "No network callback: " + e.getMessage());
            networkCallback = null;
        }
    }

    private void unregisterNetworkCallback() {
        if (networkCallback == null) return;
        ConnectivityManager cm = getSystemService(ConnectivityManager.class);
        try {
            if (cm != null) cm.unregisterNetworkCallback(networkCallback);
        } catch (Exception ignored) {}
        networkCallback = null;
    }

    private void loop() {
        long backoff = BACKOFF_MIN_MS;
        long forbiddenSince = 0;
        while (!stopping) {
            SharedPreferences p = prefs(this);
            String serverUrl = p.getString(KEY_SERVER_URL, "");
            String token = p.getString(KEY_TOKEN, "");
            if (serverUrl.isEmpty() || token.isEmpty()) break;
            long started = System.currentTimeMillis();
            try {
                Response r = fetchFeed(serverUrl, token, p.getString(KEY_CURSOR, null));
                if (r.code == 403) {
                    if (forbiddenSince == 0) forbiddenSince = started;
                } else {
                    forbiddenSince = 0;
                }
                boolean refused = r.code == 401
                    || (r.code == 403 && started - forbiddenSince >= FORBIDDEN_GIVE_UP_MS);
                if (refused) {
                    // A rotation saves the new token before revoking the old
                    // one; a refusal of the old token is not a turn-off.
                    if (!clearIfCurrent(prefs(this), token)) continue;
                    Log.i(TAG, "Server refused the device token (" + r.code + "); turning alerts off");
                    turnOffAfterRefusal();
                    return;
                }
                if (r.code != 200) throw new IOException("HTTP " + r.code);
                handle(r.body, token);
                backoff = BACKOFF_MIN_MS;
                // Never spin if something in between answers at once.
                if (System.currentTimeMillis() - started < 1_000) pause(2_000);
            } catch (Exception e) {
                if (stopping) break;
                Log.w(TAG, "Feed failed, retrying in " + backoff / 1000 + "s: " + e.getMessage());
                pause(backoff);
                backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
            }
        }
    }

    private void pause(long ms) {
        synchronized (wakeSignal) {
            try {
                wakeSignal.wait(ms);
            } catch (InterruptedException ignored) {}
        }
    }

    private static final class Response {
        final int code;
        final String body;

        Response(int code, String body) {
            this.code = code;
            this.body = body;
        }
    }

    private Response fetchFeed(String serverUrl, String token, @Nullable String cursor) throws IOException {
        StringBuilder url = new StringBuilder(serverUrl)
            .append("/api/notifications/device-alerts/feed?wait=").append(WAIT_SECONDS);
        if (cursor != null && !cursor.isEmpty()) {
            url.append("&after=").append(URLEncoder.encode(cursor, "UTF-8"));
        }
        HttpURLConnection conn = (HttpURLConnection) new URL(url.toString()).openConnection();
        inFlight = conn;
        try {
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(READ_TIMEOUT_MS);
            conn.setRequestProperty(TOKEN_HEADER, token);
            conn.setRequestProperty("Accept", "application/json");
            int code = conn.getResponseCode();
            String body = code == 200 ? readAll(conn.getInputStream()) : "";
            return new Response(code, body);
        } finally {
            inFlight = null;
            conn.disconnect();
        }
    }

    static String readAll(InputStream in) throws IOException {
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = s.read(buf)) != -1) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }

    // A short wake lock only while a response is handled: Doze can defer the
    // next request, but never cut a notification off half-posted.
    private void handle(String body, String token) throws Exception {
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        PowerManager.WakeLock lock = pm == null ? null
            : pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "GDX::OfficeAlerts");
        if (lock != null) lock.acquire(10_000);
        try {
            JSONObject json = new JSONObject(body);
            SharedPreferences p = prefs(this);
            LinkedHashSet<String> seen = new LinkedHashSet<>();
            String stored = p.getString(KEY_SEEN, "");
            if (!stored.isEmpty()) seen.addAll(Arrays.asList(stored.split(",")));

            List<JSONObject> fresh = new ArrayList<>();
            JSONArray items = json.optJSONArray("items");
            if (items != null) {
                for (int i = 0; i < items.length(); i++) {
                    JSONObject item = items.getJSONObject(i);
                    String id = item.optString("id", "");
                    // Every response re-sends two minutes of history.
                    if (id.isEmpty() || seen.contains(id)) continue;
                    fresh.add(item);
                    seen.add(id);
                }
            }
            List<String> keep = new ArrayList<>(seen);
            if (keep.size() > SEEN_LIMIT) keep = keep.subList(keep.size() - SEEN_LIMIT, keep.size());
            recordIfCurrent(p, token, stopping, () -> {
                for (JSONObject item : fresh) {
                    postAlert(item.optString("id", ""), item.optString("title", "GDX Dispatch"),
                        item.optString("message", ""), item.optString("category", ""));
                }
            }, json.optString("cursor", p.getString(KEY_CURSOR, "")), String.join(",", keep));
        } finally {
            if (lock != null && lock.isHeld()) lock.release();
        }
    }

    private void postAlert(String id, String title, String message, String category) {
        int notificationId = id.hashCode();
        Notification publicVersion = new NotificationCompat.Builder(this, CHANNEL_ALERTS)
            .setContentTitle("GDX Dispatch")
            .setContentText("New office notification")
            .setSmallIcon(R.mipmap.ic_launcher)
            .build();
        Notification n = new NotificationCompat.Builder(this, CHANNEL_ALERTS)
            .setContentTitle(title)
            .setContentText(message)
            .setStyle(new NotificationCompat.BigTextStyle().bigText(message))
            .setSmallIcon(R.mipmap.ic_launcher)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_MESSAGE)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(publicVersion)
            .setAutoCancel(true)
            .setContentIntent(openAppIntent(notificationId, category))
            .build();
        notify(notificationId, n);
    }

    private void notify(int id, Notification n) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        try {
            nm.notify(id, n);
        } catch (SecurityException e) {
            Log.w(TAG, "Notifications are not allowed: " + e.getMessage());
        }
    }

    // The caller has already cleared the prefs, under PREFS_LOCK.
    private void turnOffAfterRefusal() {
        notify(TURNED_OFF_NOTIFICATION_ID, new NotificationCompat.Builder(this, CHANNEL_ALERTS)
            .setContentTitle("Phone alerts turned off")
            .setContentText("Open GDX Dispatch to turn them back on.")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setAutoCancel(true)
            .setContentIntent(openAppIntent(TURNED_OFF_NOTIFICATION_ID, null))
            .build());
        stopping = true;
        running = false;
        unregisterNetworkCallback();
        try {
            stopForeground(true);
        } catch (Exception ignored) {}
        stopSelf();
    }

    private void shutdown() {
        stopping = true;
        running = false;
        HttpURLConnection conn = inFlight;
        if (conn != null) {
            // Unblocks a read parked in the long-poll.
            new Thread(conn::disconnect).start();
        }
        synchronized (wakeSignal) {
            wakeSignal.notifyAll();
        }
        unregisterNetworkCallback();
        try {
            stopForeground(true);
        } catch (Exception ignored) {}
        stopSelf();
    }

    @Override
    public void onDestroy() {
        shutdown();
        super.onDestroy();
    }

    @Nullable
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
