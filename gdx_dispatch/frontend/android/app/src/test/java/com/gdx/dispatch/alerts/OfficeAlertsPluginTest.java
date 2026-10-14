package com.gdx.dispatch.alerts;

import static org.junit.Assert.assertEquals;

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
}
