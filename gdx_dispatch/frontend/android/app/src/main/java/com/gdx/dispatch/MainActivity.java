package com.gdx.dispatch;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import com.gdx.dispatch.alerts.OfficeAlertsPlugin;
import com.gdx.dispatch.location.GpsForegroundPlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(GpsForegroundPlugin.class);
        registerPlugin(OfficeAlertsPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
