/*
 * Copyright (c) 2020 Bytedance Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

package com.android.commands.monkey.events.base;

import android.content.ComponentName;
import android.os.SystemClock;

import com.android.commands.monkey.framework.AndroidDevice;
import com.android.commands.monkey.utils.Logger;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/**
 * Small, synchronous input guard for system runtime-permission activities.
 *
 * The Python foreground watcher is still responsible for stopping Fastbot and
 * handing the device to uiautomator2.  This guard closes the short interval
 * between an activity becoming resumed and that watcher completing the handoff.
 */
public final class PermissionInputGuard {
    private static final long PROBE_CACHE_MILLIS = 40L;
    private static final Object LOCK = new Object();

    private static final Set<String> CONTROLLER_PACKAGES = new HashSet<String>(Arrays.asList(
            "com.android.permissioncontroller",
            "com.google.android.permissioncontroller",
            "com.android.packageinstaller",
            "com.google.android.packageinstaller",
            "com.lbe.security.miui",
            "com.miui.securitycenter",
            "com.huawei.systemmanager",
            "com.hihonor.systemmanager",
            "com.vivo.permissionmanager",
            "com.vivo.permissionmanager2",
            "com.coloros.securitypermission",
            "com.oplus.permissioncontroller",
            "com.samsung.android.permissioncontroller"
    ));

    private static final String[] ACTIVITY_MARKERS = new String[] {
            "grantpermissionsactivity",
            "permissiondialog",
            "permissionrequestactivity",
            "servicepermissionactivity"
    };

    private static long lastProbeAt = Long.MIN_VALUE;
    private static boolean lastVisible;
    private static String lastComponent = "";
    private static int suppressedInputCount;

    private PermissionInputGuard() {
    }

    /**
     * Return true when the current top activity is a runtime permission prompt.
     * A very short cache avoids issuing two binder calls for one event while
     * keeping the detection latency below the foreground watcher interval.
     */
    public static boolean isRuntimePermissionDialogVisible() {
        long now = SystemClock.uptimeMillis();
        synchronized (LOCK) {
            if (now - lastProbeAt >= 0L && now - lastProbeAt < PROBE_CACHE_MILLIS) {
                return lastVisible;
            }

            ComponentName component = null;
            try {
                component = AndroidDevice.getTopActivityComponentName();
            } catch (RuntimeException exception) {
                // ActivityManager can briefly reject a probe during a task
                // transition. Preserve a positive result for this short cache
                // window so an already-visible prompt is never exposed to an
                // input event; a negative result simply retries next time.
                lastProbeAt = now;
                return lastVisible;
            }

            boolean visible = isRuntimePermissionComponent(component);
            String componentName = component == null ? "" : component.flattenToShortString();
            if (visible && (!lastVisible || !componentName.equals(lastComponent))) {
                Logger.warningPrintln("// PermissionInputGuard blocked random input on "
                        + componentName);
            } else if (!visible && lastVisible) {
                Logger.println("// PermissionInputGuard released random input after "
                        + suppressedInputCount + " suppressed events");
                suppressedInputCount = 0;
            }
            lastProbeAt = now;
            lastVisible = visible;
            lastComponent = componentName;
            return visible;
        }
    }

    /** Return true when a component identifies a known runtime permission UI. */
    public static boolean isRuntimePermissionComponent(ComponentName component) {
        if (component == null) {
            return false;
        }
        String packageName = component.getPackageName();
        String activityName = component.getClassName();
        if (packageName == null || activityName == null) {
            return false;
        }
        packageName = packageName.toLowerCase(Locale.ENGLISH);
        activityName = activityName.toLowerCase(Locale.ENGLISH);
        boolean controller = CONTROLLER_PACKAGES.contains(packageName)
                || packageName.endsWith(".permissioncontroller")
                || packageName.endsWith(".packageinstaller");
        if (!controller) {
            return false;
        }
        for (String marker : ACTIVITY_MARKERS) {
            if (activityName.contains(marker)) {
                return true;
            }
        }
        return false;
    }

    /** Record a discarded event without emitting one log line per gesture. */
    public static void noteSuppressedInput(String kind) {
        synchronized (LOCK) {
            suppressedInputCount++;
        }
    }
}
