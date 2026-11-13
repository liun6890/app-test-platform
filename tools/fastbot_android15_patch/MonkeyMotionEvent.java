/*
 * Copyright (c) 2020 Bytedance Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.android.commands.monkey.events.base;

import android.app.IActivityManager;
import android.app.StatusBarManager;
import android.content.res.Resources;
import android.graphics.Rect;
import android.hardware.display.DisplayManagerGlobal;
import android.hardware.input.InputManager;
import android.os.Binder;
import android.os.IBinder;
import android.os.RemoteException;
import android.os.ServiceManager;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.util.SparseArray;
import android.view.Display;
import android.view.IWindowManager;
import android.view.MotionEvent;

import com.android.commands.monkey.events.MonkeyEvent;
import com.android.commands.monkey.utils.Logger;
import com.android.internal.statusbar.IStatusBarService;

import static com.android.commands.monkey.utils.Config.bytestStatusBarHeight;

/** Fastbot motion event with guards for Android-owned system gesture areas. */
public abstract class MonkeyMotionEvent extends MonkeyEvent {

    private static final int SYSTEM_GESTURE_GUARD_MARGIN_DP = 4;
    private static final int NAVIGATION_GESTURE_FALLBACK_DP = 24;
    private static final int NAVIGATION_GESTURE_FALLBACK_HEIGHT_PERCENT = 4;
    private static final int SIDE_GESTURE_FALLBACK_DP = 18;
    private static final long STATUS_BAR_COLLAPSE_WAIT_MILLIS = 250L;
    private static final long STATUS_BAR_DISABLE_RETRY_MILLIS = 1000L;
    private static final String STATUS_BAR_DISABLE_OWNER = "com.android.commands.monkey";
    private static final Object SYSTEM_GESTURE_LOCK = new Object();
    private static final Object STATUS_BAR_DISABLE_LOCK = new Object();
    private static final IBinder STATUS_BAR_DISABLE_TOKEN = new Binder();
    private static boolean suppressSystemGesture;
    private static int suppressedSystemGestureCount;
    private static volatile boolean statusBarExpansionDisabled;
    private static int statusBarDisableFailureCount;
    private static long nextStatusBarDisableAttemptAt;

    private long mDownTime;
    private long mEventTime;
    private int mAction;
    private SparseArray<MotionEvent.PointerCoords> mPointers;
    private int mMetaState;
    private float mXPrecision;
    private float mYPrecision;
    private int mDeviceId;
    private int mSource;
    private int mFlags;
    private int mEdgeFlags;
    private int type = 0;
    private boolean mIntermediateNote;

    protected MonkeyMotionEvent(int type, int source, int action) {
        super(type);
        mSource = source;
        mDownTime = -1;
        mEventTime = -1;
        mAction = action;
        mPointers = new SparseArray<MotionEvent.PointerCoords>();
        mXPrecision = 1;
        mYPrecision = 1;
    }

    public MonkeyMotionEvent addPointer(int id, float x, float y) {
        return addPointer(id, x, y, 0, 0);
    }

    public MonkeyMotionEvent addPointer(int id, float x, float y, float pressure, float size) {
        MotionEvent.PointerCoords point = new MotionEvent.PointerCoords();
        point.x = x;
        point.y = y;
        point.pressure = pressure;
        point.size = size;
        mPointers.append(id, point);
        return this;
    }

    public boolean getIntermediateNote() {
        return mIntermediateNote;
    }

    public MonkeyMotionEvent setIntermediateNote(boolean value) {
        mIntermediateNote = value;
        return this;
    }

    public int getAction() {
        return mAction;
    }

    public long getDownTime() {
        return mDownTime;
    }

    public MonkeyMotionEvent setDownTime(long downTime) {
        mDownTime = downTime;
        return this;
    }

    public long getEventTime() {
        return mEventTime;
    }

    public MonkeyMotionEvent setEventTime(long eventTime) {
        mEventTime = eventTime;
        return this;
    }

    public MonkeyMotionEvent setMetaState(int metaState) {
        mMetaState = metaState;
        return this;
    }

    public MonkeyMotionEvent setPrecision(float xPrecision, float yPrecision) {
        mXPrecision = xPrecision;
        mYPrecision = yPrecision;
        return this;
    }

    public MonkeyMotionEvent setDeviceId(int deviceId) {
        mDeviceId = deviceId;
        return this;
    }

    public MonkeyMotionEvent setEdgeFlags(int edgeFlags) {
        mEdgeFlags = edgeFlags;
        return this;
    }

    public MonkeyMotionEvent setType(int value) {
        type = value;
        return this;
    }

    MotionEvent getEvent() {
        int pointerCount = mPointers.size();
        int[] pointerIds = new int[pointerCount];
        MotionEvent.PointerCoords[] pointerCoords = new MotionEvent.PointerCoords[pointerCount];
        for (int index = 0; index < pointerCount; index++) {
            pointerIds[index] = mPointers.keyAt(index);
            pointerCoords[index] = mPointers.valueAt(index);
        }

        return MotionEvent.obtain(
                mDownTime,
                mEventTime < 0 ? SystemClock.uptimeMillis() : mEventTime,
                mAction,
                pointerCount,
                pointerIds,
                pointerCoords,
                mMetaState,
                mXPrecision,
                mYPrecision,
                mDeviceId,
                mEdgeFlags,
                mSource,
                mFlags
        );
    }

    /**
     * Disable notification-shade expansion for both Fastbot's native model
     * and its Google Monkey fallback. The system owns a death recipient for
     * this process-local token, so the restriction is removed even when the
     * runner is killed before normal teardown.
     */
    private static void ensureStatusBarExpansionDisabled() {
        if (statusBarExpansionDisabled) {
            return;
        }
        synchronized (STATUS_BAR_DISABLE_LOCK) {
            if (statusBarExpansionDisabled) {
                return;
            }
            long now = SystemClock.uptimeMillis();
            if (now < nextStatusBarDisableAttemptAt) {
                return;
            }
            nextStatusBarDisableAttemptAt = now + STATUS_BAR_DISABLE_RETRY_MILLIS;
            try {
                IStatusBarService service = IStatusBarService.Stub.asInterface(
                        ServiceManager.getService("statusbar"));
                if (service == null) {
                    throw new IllegalStateException("status bar service unavailable");
                }
                service.collapsePanels();
                service.disable(
                        StatusBarManager.DISABLE_EXPAND,
                        STATUS_BAR_DISABLE_TOKEN,
                        STATUS_BAR_DISABLE_OWNER);
                statusBarExpansionDisabled = true;
                Logger.println("// StatusBarInputGuard disabled notification shade expansion; "
                        + "process exit restores it");
                // collapsePanels() is asynchronous on vendor SystemUI builds.
                SystemClock.sleep(STATUS_BAR_COLLAPSE_WAIT_MILLIS);
            } catch (RemoteException | RuntimeException exception) {
                statusBarDisableFailureCount++;
                if (statusBarDisableFailureCount <= 3
                        || statusBarDisableFailureCount % 100 == 0) {
                    Logger.warningPrintln("// StatusBarInputGuard could not disable notification "
                            + "shade expansion: " + exception.getClass().getSimpleName());
                }
            }
        }
    }

    private static Rect getApplicationSafeBounds(IWindowManager windowManager) {
        Display display = DisplayManagerGlobal.getInstance().getRealDisplay(Display.DEFAULT_DISPLAY);
        if (display == null || display.getWidth() <= 0 || display.getHeight() <= 0) {
            return null;
        }
        DisplayMetrics metrics = new DisplayMetrics();
        display.getMetrics(metrics);
        float density = metrics.density > 0f ? metrics.density : 1f;
        int width = display.getWidth();
        int height = display.getHeight();
        int statusBarHeight = Math.max(0, bytestStatusBarHeight);
        int navigationBarHeight = 0;
        int navigationBarWidth = 0;
        try {
            Resources resources = Resources.getSystem();
            int statusBarId = resources.getIdentifier(
                    "status_bar_height", "dimen", "android");
            if (statusBarId != 0) {
                statusBarHeight = Math.max(
                        statusBarHeight, resources.getDimensionPixelSize(statusBarId));
            }
            int navigationBarHeightId = resources.getIdentifier(
                    "navigation_bar_height", "dimen", "android");
            if (navigationBarHeightId != 0) {
                navigationBarHeight = Math.max(
                        navigationBarHeight,
                        resources.getDimensionPixelSize(navigationBarHeightId));
            }
            int navigationBarWidthId = resources.getIdentifier(
                    "navigation_bar_width", "dimen", "android");
            if (navigationBarWidthId != 0) {
                navigationBarWidth = Math.max(
                        navigationBarWidth,
                        resources.getDimensionPixelSize(navigationBarWidthId));
            }
        } catch (RuntimeException ignored) {
            // WindowManager and the density fallback below still protect the edge.
        }
        Rect stableInsets = new Rect();
        if (windowManager != null) {
            try {
                windowManager.getStableInsets(display.getDisplayId(), stableInsets);
                statusBarHeight = Math.max(statusBarHeight, stableInsets.top);
            } catch (RemoteException | RuntimeException ignored) {
                // Some vendor builds restrict this hidden API; keep the other values.
            }
        }
        if (statusBarHeight <= 0) {
            statusBarHeight = Math.round(24f * density);
        }
        int bottomGestureFallback = Math.max(
                Math.round(NAVIGATION_GESTURE_FALLBACK_DP * density),
                (height * NAVIGATION_GESTURE_FALLBACK_HEIGHT_PERCENT + 99) / 100);
        int bottomInset = Math.max(
                stableInsets.bottom,
                Math.max(navigationBarHeight, bottomGestureFallback));
        int sideInset = Math.max(
                navigationBarWidth,
                Math.round(SIDE_GESTURE_FALLBACK_DP * density));
        int leftInset = Math.max(stableInsets.left, sideInset);
        int rightInset = Math.max(stableInsets.right, sideInset);
        int guardMargin = Math.max(
                1, Math.round(SYSTEM_GESTURE_GUARD_MARGIN_DP * density));
        int left = Math.min(width / 2, leftInset + guardMargin);
        int top = Math.min(height / 2, statusBarHeight + guardMargin);
        int right = Math.max(left, width - Math.min(width / 2, rightInset + guardMargin));
        int bottom = Math.max(
                top,
                height - Math.min(height / 2, bottomInset + guardMargin));
        return new Rect(left, top, right, bottom);
    }

    /**
     * Suppress a complete gesture when its first pointer lands in a system-owned
     * status, navigation, or back-gesture edge. Moving that point into app content
     * would turn a discarded random action into a click on an unrelated control,
     * so the sequence is dropped intact until UP/CANCEL instead.
     */
    private static boolean shouldSuppressSystemGesture(
            MotionEvent event, IWindowManager windowManager) {
        int action = event.getActionMasked();
        synchronized (SYSTEM_GESTURE_LOCK) {
            if (action == MotionEvent.ACTION_DOWN) {
                Rect safeBounds = getApplicationSafeBounds(windowManager);
                float x = event.getX(0);
                float y = event.getY(0);
                suppressSystemGesture = safeBounds != null
                        && (x < safeBounds.left || x >= safeBounds.right
                        || y < safeBounds.top || y >= safeBounds.bottom);
                if (suppressSystemGesture) {
                    suppressedSystemGestureCount++;
                    if (suppressedSystemGestureCount <= 3
                            || suppressedSystemGestureCount % 100 == 0) {
                        Logger.warningPrintln("// StatusBarInputGuard dropped gesture #"
                                + suppressedSystemGestureCount + " at x=" + x + ", y=" + y
                                + "; SystemGestureInputGuard safe bounds=" + safeBounds);
                    }
                }
                return suppressSystemGesture;
            }
            if (!suppressSystemGesture) {
                return false;
            }
            if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
                suppressSystemGesture = false;
            }
            return true;
        }
    }

    @Override
    public boolean isThrottlable() {
        return getAction() == MotionEvent.ACTION_UP;
    }

    @Override
    public int injectEvent(IWindowManager windowManager, IActivityManager activityManager, int verbose) {
        ensureStatusBarExpansionDisabled();
        // The foreground watcher stops Fastbot asynchronously.  Check again
        // at the injection boundary so an event already dequeued during that
        // handoff cannot land on a newly-rendered permission prompt.
        if (PermissionInputGuard.isRuntimePermissionDialogVisible()) {
            PermissionInputGuard.noteSuppressedInput("motion");
            return MonkeyEvent.INJECT_SUCCESS;
        }
        MotionEvent event = getEvent();
        if (AppInputGuard.suppressMotion(event)) {
            event.recycle();
            return MonkeyEvent.INJECT_SUCCESS;
        }
        if (shouldSuppressSystemGesture(event, windowManager)) {
            event.recycle();
            return MonkeyEvent.INJECT_SUCCESS;
        }
        if (PermissionInputGuard.isRuntimePermissionDialogVisible()) {
            PermissionInputGuard.noteSuppressedInput("motion");
            event.recycle();
            return MonkeyEvent.INJECT_SUCCESS;
        }
        if ((verbose > 0 && !mIntermediateNote) || verbose > 1) {
            StringBuilder message = new StringBuilder(":Sending ");
            message.append(getTypeLabel()).append(" (");
            switch (event.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    message.append("ACTION_DOWN");
                    break;
                case MotionEvent.ACTION_MOVE:
                    message.append("ACTION_MOVE");
                    break;
                case MotionEvent.ACTION_UP:
                    message.append("ACTION_UP");
                    break;
                case MotionEvent.ACTION_CANCEL:
                    message.append("ACTION_CANCEL");
                    break;
                case MotionEvent.ACTION_POINTER_DOWN:
                    message.append("ACTION_POINTER_DOWN ").append(event.getPointerId(event.getActionIndex()));
                    break;
                case MotionEvent.ACTION_POINTER_UP:
                    message.append("ACTION_POINTER_UP ").append(event.getPointerId(event.getActionIndex()));
                    break;
                default:
                    message.append(event.getAction());
                    break;
            }
            message.append("):");
            for (int index = 0; index < event.getPointerCount(); index++) {
                message.append(" ").append(event.getPointerId(index));
                message.append(":(").append(event.getX(index)).append(",").append(event.getY(index)).append(")");
            }
            Logger.println(message.toString());
        }

        try {
            if (AppInputGuard.blocksMotion(event)) return MonkeyEvent.INJECT_SUCCESS;
            if (PermissionInputGuard.isRuntimePermissionDialogVisible()) {
                PermissionInputGuard.noteSuppressedInput("motion");
                return MonkeyEvent.INJECT_SUCCESS;
            }
            if (!InputManager.getInstance().injectInputEvent(
                    event, InputManager.INJECT_INPUT_EVENT_MODE_WAIT_FOR_RESULT)) {
                return MonkeyEvent.INJECT_FAIL;
            }
        } catch (SecurityException exception) {
            return MonkeyEvent.INJECT_FAIL;
        } finally {
            event.recycle();
        }
        return MonkeyEvent.INJECT_SUCCESS;
    }

    protected abstract String getTypeLabel();
}
