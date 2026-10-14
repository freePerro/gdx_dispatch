package com.gdx.dispatch.location;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.location.Location;
import android.location.LocationManager;
import androidx.core.content.ContextCompat;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

@CapacitorPlugin(
    name = "GpsForeground",
    permissions = {
        @Permission(
            alias = "location",
            strings = {
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION
            }
        ),
        @Permission(
            alias = "notifications",
            strings = {
                Manifest.permission.POST_NOTIFICATIONS
            }
        )
    }
)
public class GpsForegroundPlugin extends Plugin {

    @PluginMethod
    public void start(PluginCall call) {
        if (getPermissionState("location") != PermissionState.GRANTED) {
            requestPermissionForAlias("location", call, "locationPermCallback");
            return;
        }

        doStart(call);
    }

    @PermissionCallback
    private void locationPermCallback(PluginCall call) {
        if (getPermissionState("location") == PermissionState.GRANTED) {
            doStart(call);
        } else {
            call.reject("Location permission is required for persistent GPS tracking while clocked in");
        }
    }

    private void doStart(PluginCall call) {
        try {
            String title = call.getString("title", "GDX Dispatch");
            String text = call.getString("text", "GPS tracking active while clocked in");

            Intent intent = new Intent(getContext(), GpsForegroundService.class);
            intent.setAction(GpsForegroundService.ACTION_START);
            intent.putExtra(GpsForegroundService.EXTRA_TITLE, title);
            intent.putExtra(GpsForegroundService.EXTRA_TEXT, text);

            ContextCompat.startForegroundService(getContext(), intent);

            JSObject ret = new JSObject();
            ret.put("running", true);
            call.resolve(ret);
        } catch (Exception e) {
            call.reject("Could not start GPS foreground service: " + e.getMessage(), e);
        }
    }

    @PluginMethod
    public void stop(PluginCall call) {
        try {
            Intent intent = new Intent(getContext(), GpsForegroundService.class);
            intent.setAction(GpsForegroundService.ACTION_STOP);
            getContext().stopService(intent);

            JSObject ret = new JSObject();
            ret.put("running", false);
            call.resolve(ret);
        } catch (Exception e) {
            call.reject("Could not stop GPS foreground service: " + e.getMessage(), e);
        }
    }

    @PluginMethod
    public void isRunning(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("running", GpsForegroundService.isServiceRunning());
        call.resolve(ret);
    }

    @PluginMethod
    public void getLastLocation(PluginCall call) {
        try {
            LocationManager lm = (LocationManager) getContext().getSystemService(Context.LOCATION_SERVICE);
            if (lm == null) {
                call.reject("LocationManager unavailable");
                return;
            }
            Location bestLocation = null;
            for (String provider : lm.getProviders(true)) {
                try {
                    Location l = lm.getLastKnownLocation(provider);
                    if (l == null) continue;
                    if (bestLocation == null || l.getAccuracy() < bestLocation.getAccuracy()) {
                        bestLocation = l;
                    }
                } catch (SecurityException ignored) {}
            }
            if (bestLocation != null) {
                JSObject ret = new JSObject();
                JSObject coords = new JSObject();
                coords.put("latitude", bestLocation.getLatitude());
                coords.put("longitude", bestLocation.getLongitude());
                coords.put("accuracy", bestLocation.getAccuracy());
                coords.put("speed", bestLocation.hasSpeed() ? bestLocation.getSpeed() : null);
                coords.put("heading", bestLocation.hasBearing() ? bestLocation.getBearing() : null);
                ret.put("coords", coords);
                ret.put("timestamp", bestLocation.getTime());
                call.resolve(ret);
            } else {
                call.reject("No location fix available yet");
            }
        } catch (Exception e) {
            call.reject("Error getting last location: " + e.getMessage(), e);
        }
    }
}
