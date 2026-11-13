package com.android.commands.monkey.events.base;

import android.app.UiAutomation;
import android.graphics.Rect;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import com.android.commands.monkey.utils.Logger;
import java.util.List;

/** Checks window ownership again at the actual input-injection boundary. */
public final class AppInputGuard {
    private static UiAutomation automation;
    private static String targetPackage = "";
    private static boolean previouslyBlocked;
    private static boolean suppressedGesture;

    private AppInputGuard() {}

    public static void configure(UiAutomation owner, String packageName) {
        automation = owner;
        targetPackage = packageName;
        previouslyBlocked = false;
        suppressedGesture = false;
    }

    public static UiAutomation automation() { return automation; }
    public static String targetPackage() { return targetPackage; }

    public static boolean isBlocked() { return isBlocked(null); }

    private static boolean containsPointers(Rect bounds, MotionEvent event) {
        if (event == null) return true;
        for (int i = 0; i < event.getPointerCount(); i++) {
            if (!bounds.contains((int) event.getX(i), (int) event.getY(i))) return false;
        }
        return true;
    }

    private static boolean overlapsPointer(Rect bounds, MotionEvent event) {
        if (event == null) return false;
        for (int i = 0; i < event.getPointerCount(); i++) {
            if (bounds.contains((int) event.getX(i), (int) event.getY(i))) return true;
        }
        return false;
    }

    private static boolean isBlocked(MotionEvent event) {
        // Legacy sources have no UiAutomation owner. Strict runs explicitly
        // select ApeNative so configure() always runs before the first event.
        if (automation == null) return !System.getProperty("app.press.targetPackage", "").isEmpty();
        boolean targetFound = false;
        boolean blocked = targetPackage.isEmpty();
        String activePackage = "<missing>";
        int windowCount = -1;
        String blockingWindow = "";
        String windowDetails = "";
        boolean targetWindowSeen = false;
        List<AccessibilityWindowInfo> windows = null;
        AccessibilityNodeInfo active = null;
        try {
            active = automation.getRootInActiveWindow();
            if (active != null && active.getPackageName() != null) {
                activePackage = active.getPackageName().toString();
            }
            if (active == null || !targetPackage.contentEquals(
                    active.getPackageName() == null ? "" : active.getPackageName())) blocked = true;
            windows = automation.getWindows();
            if (windows != null) windowCount = windows.size();
            if (windows != null && windows.isEmpty() && !blocked) {
                // Some Android 9 builds expose the focused app root but no window list.
                // The root still bounds where a motion event may be delivered.
                Rect rootBounds = new Rect();
                active.getBoundsInScreen(rootBounds);
                targetFound = !rootBounds.isEmpty() && containsPointers(rootBounds, event);
            }
            if (windows != null) for (AccessibilityWindowInfo window : windows) {
                if (window == null) continue;
                Rect bounds = new Rect();
                window.getBoundsInScreen(bounds);
                if (bounds.isEmpty()) continue;
                AccessibilityNodeInfo root = window.getRoot();
                try {
                    if (windowDetails.isEmpty()) {
                        windowDetails = "type=" + window.getType() + ",active="
                                + window.isActive() + ",focused=" + window.isFocused()
                                + ",root=" + (root == null || root.getPackageName() == null
                                ? "<missing>" : root.getPackageName().toString());
                    }
                    boolean ours = root != null && root.getPackageName() != null
                            && targetPackage.contentEquals(root.getPackageName());
                    if (ours) targetWindowSeen = true;
                    if (ours && (window.isActive() || window.isFocused())) {
                        targetFound = targetFound || containsPointers(bounds, event);
                    } else if (!ours && (window.isActive() || window.isFocused()
                            || window.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD
                            || overlapsPointer(bounds, event))) {
                        // IMEs often are neither active nor focused. Inspect
                        // their visible window type rather than just ActivityManager.
                        blocked = true;
                        blockingWindow = root == null || root.getPackageName() == null
                                ? "<missing>" : root.getPackageName().toString();
                    }
                } finally {
                    if (root != null) root.recycle();
                }
            }
        } catch (RuntimeException exception) {
            blocked = true;
            blockingWindow = exception.getClass().getSimpleName();
        } finally {
            if (active != null) active.recycle();
            if (windows != null) for (AccessibilityWindowInfo window : windows) {
                if (window != null) window.recycle();
            }
        }
        blocked = blocked || !targetFound;
        if (blocked && !previouslyBlocked) {
            Logger.warningPrintln("// AppInputGuard blocked external window, IME or unknown ownership"
                    + ": active=" + activePackage + " windows=" + windowCount
                    + " targetWindow=" + targetWindowSeen + " foreign=" + blockingWindow
                    + " firstWindow=" + windowDetails);
        }
        previouslyBlocked = blocked;
        return blocked;
    }

    public static synchronized boolean suppressMotion(MotionEvent event) {
        int action = event.getActionMasked();
        if (action == MotionEvent.ACTION_DOWN) suppressedGesture = false;
        boolean blocked = isBlocked(event);
        if (blocked) suppressedGesture = true;
        boolean suppress = suppressedGesture;
        if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
            suppressedGesture = false;
        }
        return suppress;
    }

    public static boolean blocksMotion(MotionEvent event) { return isBlocked(event); }
}
