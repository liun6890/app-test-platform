package com.android.commands.monkey.events.base;

import android.app.IActivityManager;
import android.graphics.Rect;
import android.os.Bundle;
import android.view.IWindowManager;
import android.view.accessibility.AccessibilityNodeInfo;
import com.android.commands.monkey.events.MonkeyEvent;
import com.android.commands.monkey.utils.Logger;

/** Replace text in the selected app widget without keyboard or shell input. */
public final class AppTextEvent extends MonkeyEvent {
    private final Rect bounds;
    private final String text;

    public AppTextEvent(Rect bounds, String text) {
        super(MonkeyEvent.EVENT_TYPE_NOOP);
        this.bounds = bounds == null ? null : new Rect(bounds);
        this.text = text;
    }

    private boolean setText(AccessibilityNodeInfo node, int depth, boolean focusedOnly) {
        if (node == null || depth > 60) return false;
        Rect candidate = new Rect();
        node.getBoundsInScreen(candidate);
        if (node.isEditable() && node.isEnabled() && node.isVisibleToUser()
                && AppInputGuard.targetPackage().contentEquals(
                    node.getPackageName() == null ? "" : node.getPackageName())
                && (focusedOnly || bounds == null ? node.isFocused() : candidate.equals(bounds))) {
            if (AppInputGuard.isBlocked()) return false;
            Bundle arguments = new Bundle();
            arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments);
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            try {
                if (setText(child, depth + 1, focusedOnly)) return true;
            } finally {
                if (child != null) child.recycle();
            }
        }
        return false;
    }

    @Override
    public int injectEvent(IWindowManager windowManager, IActivityManager activityManager, int verbose) {
        if (AppInputGuard.isBlocked()) return INJECT_SUCCESS;
        AccessibilityNodeInfo root = null;
        try {
            root = AppInputGuard.automation().getRootInActiveWindow();
            // Focusing a field can scroll/resize the layout after the model
            // captured its bounds. Only the focused target field may follow it.
            if (setText(root, 0, false) || setText(root, 0, true)) {
                Logger.println("// AppTextEvent set configured text through control");
            } else {
                Logger.warningPrintln("// AppTextEvent found no writable target control");
            }
        } catch (RuntimeException exception) {
            Logger.warningPrintln("// AppTextEvent could not set control text; no keyboard fallback");
        } finally {
            if (root != null) root.recycle();
        }
        return INJECT_SUCCESS;
    }
}
