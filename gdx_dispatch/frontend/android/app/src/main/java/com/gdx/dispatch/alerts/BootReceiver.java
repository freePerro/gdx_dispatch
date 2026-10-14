package com.gdx.dispatch.alerts;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;
import androidx.core.content.ContextCompat;

/** Brings office alerts back after a reboot or an app update, when they were on. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action) && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
            return;
        }
        if (!OfficeAlertsService.hasCredentials(context)) return;
        try {
            Intent start = new Intent(context, OfficeAlertsService.class);
            start.setAction(OfficeAlertsService.ACTION_START);
            ContextCompat.startForegroundService(context, start);
        } catch (Exception e) {
            Log.w("OfficeAlertsBoot", "Could not restart office alerts: " + e.getMessage());
        }
    }
}
