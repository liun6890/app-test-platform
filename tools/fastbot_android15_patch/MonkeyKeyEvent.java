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
import android.hardware.input.InputManager;
import android.os.SystemClock;
import android.view.IWindowManager;
import android.view.InputDevice;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;

import com.android.commands.monkey.events.MonkeyEvent;
import com.android.commands.monkey.source.MonkeySourceRandom;
import com.android.commands.monkey.utils.Logger;

/** Fastbot key event that forwards generated key actions unchanged. */
public class MonkeyKeyEvent extends MonkeyEvent {
    private static boolean strictBackSuppressionLogged;
    private int mDeviceId;
    private long mEventTime;
    private long mDownTime;
    private int mAction;
    private int mKeyCode;
    private int mScanCode;
    private int mMetaState;
    private int mRepeatCount;
    private KeyEvent mKeyEvent;

    public MonkeyKeyEvent(int action, int keyCode) {
        this(-1, -1, action, keyCode, 0, 0, KeyCharacterMap.VIRTUAL_KEYBOARD, 0);
    }

    public MonkeyKeyEvent(long downTime, long eventTime, int action, int keyCode,
                          int repeatCount, int metaState, int device, int scanCode) {
        super(EVENT_TYPE_KEY);
        mDownTime = downTime;
        mEventTime = eventTime;
        mAction = action;
        mKeyCode = keyCode;
        mRepeatCount = repeatCount;
        mMetaState = metaState;
        mDeviceId = device;
        mScanCode = scanCode;
    }

    public MonkeyKeyEvent(KeyEvent event) {
        super(EVENT_TYPE_KEY);
        mKeyEvent = event;
    }

    public int getKeyCode() {
        return mKeyEvent != null ? mKeyEvent.getKeyCode() : mKeyCode;
    }

    public int getAction() {
        return mKeyEvent != null ? mKeyEvent.getAction() : mAction;
    }

    public long getDownTime() {
        return mKeyEvent != null ? mKeyEvent.getDownTime() : mDownTime;
    }

    public void setDownTime(long downTime) {
        if (mKeyEvent != null) {
            throw new IllegalStateException("Cannot modify down time of this key event.");
        }
        mDownTime = downTime;
    }

    public long getEventTime() {
        return mKeyEvent != null ? mKeyEvent.getEventTime() : mEventTime;
    }

    public void setEventTime(long eventTime) {
        if (mKeyEvent != null) {
            throw new IllegalStateException("Cannot modify event time of this key event.");
        }
        mEventTime = eventTime;
    }

    @Override
    public boolean isThrottlable() {
        return getAction() == KeyEvent.ACTION_UP;
    }

    private boolean systemUiRecovery;

    public MonkeyKeyEvent forSystemUiRecovery() {
        systemUiRecovery = true;
        return this;
    }

    @Override
    public int injectEvent(IWindowManager windowManager, IActivityManager activityManager, int verbose) {
        if (!System.getProperty("app.press.targetPackage", "").isEmpty()
                && getKeyCode() == KeyEvent.KEYCODE_BACK && !systemUiRecovery) {
            // Native Fastbot can emit BACK as its first event even with --pct-nav 0.
            // On the prepared home page this exits the target before input begins.
            if (getAction() == KeyEvent.ACTION_DOWN && !strictBackSuppressionLogged) {
                Logger.warningPrintln("// Strict input guard suppressed BACK to keep the target foreground");
                strictBackSuppressionLogged = true;
            }
            return MonkeyEvent.INJECT_SUCCESS;
        }
        if (!System.getProperty("app.press.targetPackage", "").isEmpty()
                && getKeyCode() != KeyEvent.KEYCODE_BACK
                && getKeyCode() != KeyEvent.KEYCODE_ENTER
                && getKeyCode() != KeyEvent.KEYCODE_DEL
                && getKeyCode() != KeyEvent.KEYCODE_SEARCH) return MonkeyEvent.INJECT_SUCCESS;
        if ((AppInputGuard.isBlocked() && !systemUiRecovery)
                || getKeyCode() == KeyEvent.KEYCODE_HOME
                || getKeyCode() == KeyEvent.KEYCODE_APP_SWITCH
                || getKeyCode() == KeyEvent.KEYCODE_NOTIFICATION
                || getKeyCode() == KeyEvent.KEYCODE_POWER) return MonkeyEvent.INJECT_SUCCESS;
        // A key event can dismiss or navigate away from a permission page just
        // like a touch.  Re-check immediately before constructing/injecting it
        // because the Python handoff watcher runs on a separate thread.
        if (PermissionInputGuard.isRuntimePermissionDialogVisible()) {
            PermissionInputGuard.noteSuppressedInput("key");
            return MonkeyEvent.INJECT_SUCCESS;
        }
        int action = getAction();
        int keyCode = getKeyCode();

        if (verbose > 1) {
            String note = action == KeyEvent.ACTION_UP ? "ACTION_UP" : "ACTION_DOWN";
            try {
                Logger.println(":Sending Key (" + note + "): " + keyCode + "    // "
                        + MonkeySourceRandom.getKeyName(keyCode));
            } catch (ArrayIndexOutOfBoundsException exception) {
                Logger.warningPrintln(":Sending Key (" + note + "): " + keyCode
                        + "    // Unknown key event");
            }
        }

        KeyEvent keyEvent = mKeyEvent;
        if (keyEvent == null) {
            long eventTime = mEventTime > 0 ? mEventTime : SystemClock.uptimeMillis();
            long downTime = mDownTime > 0 ? mDownTime : eventTime;
            keyEvent = new KeyEvent(
                    downTime,
                    eventTime,
                    action,
                    keyCode,
                    mRepeatCount,
                    mMetaState,
                    mDeviceId,
                    mScanCode,
                    KeyEvent.FLAG_FROM_SYSTEM,
                    InputDevice.SOURCE_KEYBOARD
            );
        }

        if (PermissionInputGuard.isRuntimePermissionDialogVisible()) {
            PermissionInputGuard.noteSuppressedInput("key");
            return MonkeyEvent.INJECT_SUCCESS;
        }
        if (AppInputGuard.isBlocked() && !systemUiRecovery) return MonkeyEvent.INJECT_SUCCESS;
        if (!InputManager.getInstance().injectInputEvent(
                keyEvent, InputManager.INJECT_INPUT_EVENT_MODE_WAIT_FOR_RESULT)) {
            return MonkeyEvent.INJECT_FAIL;
        }
        return MonkeyEvent.INJECT_SUCCESS;
    }
}
