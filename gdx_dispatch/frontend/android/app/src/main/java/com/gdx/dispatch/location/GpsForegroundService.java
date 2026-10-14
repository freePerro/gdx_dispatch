package com.gdx.dispatch.location;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;
import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import com.gdx.dispatch.MainActivity;
import com.gdx.dispatch.R;

public class GpsForegroundService extends Service {
    private static final String TAG = "GpsForegroundService";
    public static final String CHANNEL_ID = "gdx_gps_channel";
    public static final int NOTIFICATION_ID = 9001;
    public static final String ACTION_START = "ACTION_START";
    public static final String ACTION_STOP = "ACTION_STOP";
    public static final String EXTRA_TITLE = "EXTRA_TITLE";
    public static final String EXTRA_TEXT = "EXTRA_TEXT";

    private static volatile boolean isRunning = false;
    private PowerManager.WakeLock wakeLock;

    public static boolean isServiceRunning() {
        return isRunning;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopForegroundService();
            return START_NOT_STICKY;
        }

        String title = "GDX Dispatch";
        String text = "GPS tracking active while clocked in";
        if (intent != null) {
            if (intent.hasExtra(EXTRA_TITLE)) title = intent.getStringExtra(EXTRA_TITLE);
            if (intent.hasExtra(EXTRA_TEXT)) text = intent.getStringExtra(EXTRA_TEXT);
        }

        try {
            startForegroundWithNotification(title, text);
            acquireWakeLock();
            isRunning = true;
            return START_STICKY;
        } catch (SecurityException se) {
            Log.e(TAG, "Missing permissions for foreground location service: " + se.getMessage());
            stopForegroundService();
            return START_NOT_STICKY;
        } catch (Exception e) {
            Log.e(TAG, "Error starting foreground service: " + e.getMessage(), e);
            stopForegroundService();
            return START_NOT_STICKY;
        }
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "GDX Location Tracking",
                NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Shows active location tracking while clocked in");
            channel.setShowBadge(false);
            NotificationManager manager = getSystemService(NotificationManager.class);
            if (manager != null) {
                manager.createNotificationChannel(channel);
            }
        }
    }

    private void startForegroundWithNotification(String title, String text) {
        Intent notificationIntent = new Intent(this, MainActivity.class);
        notificationIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
            this,
            0,
            notificationIntent,
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                ? PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT
                : PendingIntent.FLAG_UPDATE_CURRENT
        );

        Notification notification = new NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(pendingIntent)
            .build();

        int foregroundServiceType = 0;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            foregroundServiceType = ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION;
        }

        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, foregroundServiceType);
    }

    private void acquireWakeLock() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "GDX::GpsWakeLock");
                wakeLock.acquire();
            }
        }
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) {
            try {
                wakeLock.release();
            } catch (Exception ignored) {}
            wakeLock = null;
        }
    }

    private void stopForegroundService() {
        isRunning = false;
        releaseWakeLock();
        try {
            stopForeground(true);
        } catch (Exception ignored) {}
        stopSelf();
    }

    @Override
    public void onDestroy() {
        stopForegroundService();
        super.onDestroy();
    }

    @Nullable
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
