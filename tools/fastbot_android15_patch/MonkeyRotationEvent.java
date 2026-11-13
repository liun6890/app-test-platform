/*
 * Android 15 compatibility override for Fastbot 2.3.23.
 *
 * Android 15 removed the legacy IWindowManager rotation methods used by this
 * Fastbot release. Rotation control is auxiliary; keep event injection alive
 * when those methods are unavailable instead of terminating the whole engine.
 */
package com.android.commands.monkey.events.base;

import android.app.IActivityManager;
import android.os.RemoteException;
import android.view.IWindowManager;

import com.android.commands.monkey.events.MonkeyEvent;
import com.android.commands.monkey.utils.Logger;

public class MonkeyRotationEvent extends MonkeyEvent {

    public final int mRotationDegree;
    public final boolean mPersist;

    private static boolean rotationControlUnsupported;

    public MonkeyRotationEvent(int degree, boolean persist) {
        super(EVENT_TYPE_ROTATION);
        mRotationDegree = degree;
        mPersist = persist;
    }

    @Override
    public int injectEvent(IWindowManager iwm, IActivityManager iam, int verbose) {
        if (verbose > 0) {
            Logger.println(":Sending rotation degree=" + mRotationDegree + ", persist=" + mPersist);
        }

        if (rotationControlUnsupported) {
            return MonkeyEvent.INJECT_SUCCESS;
        }

        try {
            iwm.freezeRotation(mRotationDegree);
            if (!mPersist) {
                iwm.thawRotation();
            }
            return MonkeyEvent.INJECT_SUCCESS;
        } catch (NoSuchMethodError | AbstractMethodError ex) {
            rotationControlUnsupported = true;
            Logger.warningPrintln(
                    "Rotation control is unavailable on this Android version; continuing without it."
            );
            return MonkeyEvent.INJECT_SUCCESS;
        } catch (RemoteException ex) {
            return MonkeyEvent.INJECT_ERROR_REMOTE_EXCEPTION;
        }
    }
}
