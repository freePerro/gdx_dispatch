package com.gdx.dispatch.alerts;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Log;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * JS face of {@link OfficeAlertsService}: start with a server URL and device
 * token, stop (revoking the token natively, so it works after the app session
 * ended), resume, status, the battery-settings prompt, and notification taps.
 */
@CapacitorPlugin(
    name = "OfficeAlerts",
    permissions = {
        @Permission(alias = "notifications", strings = { Manifest.permission.POST_NOTIFICATIONS })
    }
)
public class OfficeAlertsPlugin extends Plugin {
    private static final String TAG = "OfficeAlertsPlugin";
    @Override
    public void load() {
        if (getActivity() != null) takeTap(getActivity().getIntent());
    }

    @Override
    protected void handleOnNewIntent(Intent intent) {
        super.handleOnNewIntent(intent);
        takeTap(intent);
    }

    private void takeTap(Intent intent) {
        if (intent == null || !intent.hasExtra(OfficeAlertsService.EXTRA_CATEGORY)) return;
        String category = intent.getStringExtra(OfficeAlertsService.EXTRA_CATEGORY);
        intent.removeExtra(OfficeAlertsService.EXTRA_CATEGORY);
        JSObject event = new JSObject();
        event.put("category", category == null ? "" : category);
        // Retained until JS attaches its listener, so a tap that cold-starts
        // the app still routes.
        notifyListeners("alertTap", event, true);
    }

    /** Restart the service with the stored token, when alerts are on. */
    @PluginMethod
    public void resume(PluginCall call) {
        Context ctx = getContext();
        if (OfficeAlertsService.hasCredentials(ctx)) {
            try {
                Intent intent = new Intent(ctx, OfficeAlertsService.class);
                intent.setAction(OfficeAlertsService.ACTION_START);
                ContextCompat.startForegroundService(ctx, intent);
            } catch (Exception e) {
                call.reject("Could not start office alerts: " + e.getMessage(), e);
                return;
            }
        }
        call.resolve(statusObject());
    }

    @PluginMethod
    public void start(PluginCall call) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
            && getPermissionState("notifications") != PermissionState.GRANTED) {
            requestPermissionForAlias("notifications", call, "notificationsPermCallback");
            return;
        }
        doStart(call);
    }

    @PermissionCallback
    private void notificationsPermCallback(PluginCall call) {
        if (getPermissionState("notifications") == PermissionState.GRANTED) {
            doStart(call);
        } else {
            // The token was minted for this start and will never be saved:
            // revoke it, or it stays valid on the server for 90 days.
            String serverUrl = call.getString("serverUrl", "").replaceAll("/+$", "");
            String token = call.getString("token", "");
            if (!serverUrl.isEmpty() && !token.isEmpty()) {
                new Thread(() -> revoke(serverUrl, token, "disabled")).start();
            }
            call.reject("Allow notifications for GDX Dispatch to get office alerts on this phone");
        }
    }

    private void doStart(PluginCall call) {
        String serverUrl = call.getString("serverUrl", "");
        String token = call.getString("token", "");
        if (serverUrl.isEmpty() || token.isEmpty()) {
            call.reject("serverUrl and token are required");
            return;
        }
        serverUrl = serverUrl.replaceAll("/+$", "");
        Context ctx = getContext();
        SharedPreferences p = OfficeAlertsService.prefs(ctx);
        String oldUrl = p.getString(OfficeAlertsService.KEY_SERVER_URL, "");
        String oldToken = p.getString(OfficeAlertsService.KEY_TOKEN, "");
        SharedPreferences.Editor edit = p.edit()
            .putString(OfficeAlertsService.KEY_SERVER_URL, serverUrl)
            .putString(OfficeAlertsService.KEY_TOKEN, token)
            .putLong(OfficeAlertsService.KEY_TOKEN_SAVED_AT, System.currentTimeMillis());
        if (!serverUrl.equals(oldUrl)) {
            edit.remove(OfficeAlertsService.KEY_CURSOR).remove(OfficeAlertsService.KEY_SEEN);
        }
        edit.commit();
        // A re-mint replaces the old token: revoke it so one phone holds one.
        if (!oldToken.isEmpty() && !oldToken.equals(token) && !oldUrl.isEmpty()) {
            final String url = oldUrl;
            new Thread(() -> revoke(url, oldToken, "rotated")).start();
        }
        try {
            Intent intent = new Intent(ctx, OfficeAlertsService.class);
            intent.setAction(OfficeAlertsService.ACTION_START);
            ContextCompat.startForegroundService(ctx, intent);
            call.resolve(statusObject());
        } catch (Exception e) {
            call.reject("Could not start office alerts: " + e.getMessage(), e);
        }
    }

    @PluginMethod
    public void stop(PluginCall call) {
        Context ctx = getContext();
        SharedPreferences p = OfficeAlertsService.prefs(ctx);
        String serverUrl = p.getString(OfficeAlertsService.KEY_SERVER_URL, "");
        String token = p.getString(OfficeAlertsService.KEY_TOKEN, "");
        p.edit().clear().commit();
        try {
            Intent intent = new Intent(ctx, OfficeAlertsService.class);
            intent.setAction(OfficeAlertsService.ACTION_STOP);
            ctx.stopService(intent);
        } catch (Exception e) {
            Log.w(TAG, "stopService: " + e.getMessage());
        }
        boolean wantRevoke = Boolean.TRUE.equals(call.getBoolean("revoke", true));
        String reason = call.getString("reason", "disabled");
        if (!wantRevoke || serverUrl.isEmpty() || token.isEmpty()) {
            JSObject ret = statusObject();
            ret.put("revoked", false);
            call.resolve(ret);
            return;
        }
        new Thread(() -> {
            boolean revoked = revoke(serverUrl, token, reason);
            JSObject ret = statusObject();
            ret.put("revoked", revoked);
            call.resolve(ret);
        }).start();
    }

    // The device token authenticates its own revoke, so this works after the
    // app session is gone (logout). Best effort: the server also refuses a
    // token after 90 days, and refuses its user once deactivated or demoted.
    static boolean revoke(String serverUrl, String token, String reason) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(serverUrl + "/api/notifications/device-alerts/revoke").openConnection();
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(10_000);
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setRequestProperty(OfficeAlertsService.TOKEN_HEADER, token);
            conn.setRequestProperty("Content-Type", "application/json");
            byte[] body = revokeBody(reason).getBytes(StandardCharsets.UTF_8);
            try (OutputStream out = conn.getOutputStream()) {
                out.write(body);
            }
            int code = conn.getResponseCode();
            // 401: the server no longer knows the token — nothing left to revoke.
            return code == 200 || code == 401;
        } catch (Exception e) {
            Log.w(TAG, "Revoke failed: " + e.getMessage());
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    // The reason is built into JSON by hand, so anything but a-z and _ is
    // dropped. The underscore stays: the server's list includes user_changed,
    // and the first version of this stripped it to a reason the server 422'd.
    static String revokeBody(String reason) {
        return "{\"reason\":\"" + reason.replaceAll("[^a-z_]", "") + "\"}";
    }

    // A token the app minted but will never hand to start() — the mint
    // answered after the app gave up on it. Revoked from here because the
    // device-token header would need a CORS preflight from the WebView.
    @PluginMethod
    public void revokeToken(PluginCall call) {
        String serverUrl = call.getString("serverUrl", "").replaceAll("/+$", "");
        String token = call.getString("token", "");
        if (!serverUrl.isEmpty() && !token.isEmpty()) {
            new Thread(() -> revoke(serverUrl, token, "disabled")).start();
        }
        call.resolve();
    }

    @PluginMethod
    public void status(PluginCall call) {
        call.resolve(statusObject());
    }

    private JSObject statusObject() {
        Context ctx = getContext();
        SharedPreferences p = OfficeAlertsService.prefs(ctx);
        JSObject ret = new JSObject();
        ret.put("enabled", OfficeAlertsService.hasCredentials(ctx));
        ret.put("running", OfficeAlertsService.isServiceRunning());
        ret.put("serverUrl", p.getString(OfficeAlertsService.KEY_SERVER_URL, ""));
        ret.put("tokenSavedAt", p.getLong(OfficeAlertsService.KEY_TOKEN_SAVED_AT, 0));
        ret.put("notificationsAllowed", NotificationManagerCompat.from(ctx).areNotificationsEnabled());
        ret.put("batteryUnrestricted", isIgnoringBatteryOptimizations(ctx));
        return ret;
    }

    private static boolean isIgnoringBatteryOptimizations(Context ctx) {
        PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        return pm != null && pm.isIgnoringBatteryOptimizations(ctx.getPackageName());
    }

    @PluginMethod
    public void openBatterySettings(PluginCall call) {
        Context ctx = getContext();
        try {
            if (!isIgnoringBatteryOptimizations(ctx)) {
                Intent ask = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                ask.setData(Uri.parse("package:" + ctx.getPackageName()));
                ask.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                ctx.startActivity(ask);
            }
            call.resolve(statusObject());
        } catch (Exception e) {
            try {
                Intent list = new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
                list.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                ctx.startActivity(list);
                call.resolve(statusObject());
            } catch (Exception e2) {
                call.reject("Could not open battery settings: " + e2.getMessage(), e2);
            }
        }
    }
}
