"""Execute the actual Java guard against offline Android API doubles."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


STUBS = {
    "android/graphics/Rect.java": """
package android.graphics;
public class Rect {
    public int left, top, right, bottom;
    public Rect() {}
    public Rect(int l, int t, int r, int b) {left=l; top=t; right=r; bottom=b;}
    public Rect(Rect b) {this(b.left,b.top,b.right,b.bottom);}
    public boolean equals(Object other) {
        if (!(other instanceof Rect)) return false;
        Rect b=(Rect)other; return left==b.left && top==b.top && right==b.right && bottom==b.bottom;
    }
    public boolean isEmpty() {return right <= left || bottom <= top;}
    public boolean contains(int x, int y) {return x>=left && x<right && y>=top && y<bottom;}
}
""",
    "android/view/MotionEvent.java": """
package android.view;
public class MotionEvent {
    public static final int ACTION_DOWN=0, ACTION_UP=1, ACTION_MOVE=2, ACTION_CANCEL=3;
    private int action; private float x, y;
    public MotionEvent(int a, float x, float y) {action=a; this.x=x; this.y=y;}
    public int getActionMasked() {return action;}
    public int getPointerCount() {return 1;}
    public float getX(int i) {return x;}
    public float getY(int i) {return y;}
}
""",
    "android/view/accessibility/AccessibilityNodeInfo.java": """
package android.view.accessibility;
import android.graphics.Rect;
import android.os.Bundle;
public class AccessibilityNodeInfo {
    public String pkg;
    public static final int ACTION_SET_TEXT=2097152;
    public static final String ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE="text";
    public boolean editable, focused;
    public boolean enabled=true, visible=true;
    public Rect bounds=new Rect(0,0,100,100);
    public String value="";
    public int writes;
    public AccessibilityNodeInfo[] children=new AccessibilityNodeInfo[0];
    public AccessibilityNodeInfo(String p) {pkg=p;}
    public CharSequence getPackageName() {return pkg;}
    public void getBoundsInScreen(Rect b) {b.left=bounds.left;b.top=bounds.top;b.right=bounds.right;b.bottom=bounds.bottom;}
    public boolean isEditable() {return editable;}
    public boolean isEnabled() {return enabled;}
    public boolean isVisibleToUser() {return visible;}
    public boolean isFocused() {return focused;}
    public int getChildCount() {return children.length;}
    public AccessibilityNodeInfo getChild(int i) {return children[i];}
    public boolean performAction(int action, Bundle args) {
        if (action!=ACTION_SET_TEXT) throw new AssertionError("unexpected action");
        value=args.value.toString(); writes++; return true;
    }
    public void recycle() {}
}
""",
    "android/os/Bundle.java": """
package android.os;
public class Bundle {
    public CharSequence value;
    public void putCharSequence(String key, CharSequence value) {this.value=value;}
}
""",
    "android/app/IActivityManager.java": "package android.app; public interface IActivityManager {}",
    "android/view/IWindowManager.java": "package android.view; public interface IWindowManager {}",
    "com/android/commands/monkey/events/MonkeyEvent.java": """
package com.android.commands.monkey.events;
import android.app.IActivityManager;
import android.view.IWindowManager;
public abstract class MonkeyEvent {
    public static final int EVENT_TYPE_NOOP=9, INJECT_SUCCESS=1;
    public MonkeyEvent(int type) {}
    public abstract int injectEvent(IWindowManager wm,IActivityManager am,int verbose);
}
""",
    "android/view/accessibility/AccessibilityWindowInfo.java": """
package android.view.accessibility;
import android.graphics.Rect;
public class AccessibilityWindowInfo {
    public static final int TYPE_INPUT_METHOD=2;
    public boolean active, focused; public int type;
    public Rect bounds; public AccessibilityNodeInfo root;
    public AccessibilityWindowInfo(String pkg, boolean a, boolean f, int type, Rect b) {
        root=new AccessibilityNodeInfo(pkg); active=a; focused=f; this.type=type; bounds=b;
    }
    public boolean isActive() {return active;}
    public boolean isFocused() {return focused;}
    public int getType() {return type;}
    public AccessibilityNodeInfo getRoot() {return root;}
    public void getBoundsInScreen(Rect b) {b.left=bounds.left; b.top=bounds.top; b.right=bounds.right; b.bottom=bounds.bottom;}
    public void recycle() {}
}
""",
    "android/app/UiAutomation.java": """
package android.app;
import java.util.List;
import android.view.accessibility.*;
public class UiAutomation {
    public AccessibilityNodeInfo root; public List<AccessibilityWindowInfo> windows; public boolean fail;
    public AccessibilityNodeInfo getRootInActiveWindow() {
        if (fail) throw new IllegalStateException(); return root;
    }
    public List<AccessibilityWindowInfo> getWindows() {return windows;}
}
""",
    "com/android/commands/monkey/utils/Logger.java": """
package com.android.commands.monkey.utils;
public class Logger {
    public static void warningPrintln(String value) {}
    public static void println(String value) {}
}
""",
    "GuardHarness.java": """
import android.app.UiAutomation;
import android.graphics.Rect;
import android.view.MotionEvent;
import android.view.accessibility.*;
import com.android.commands.monkey.events.base.AppInputGuard;
import java.util.*;
public class GuardHarness {
    static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    public static void main(String[] args) {
        UiAutomation ui=new UiAutomation();
        AccessibilityWindowInfo app=new AccessibilityWindowInfo("com.target", true, true, 1, new Rect(0,50,1000,1900));
        ui.root=app.root; ui.windows=Arrays.asList(app);
        AppInputGuard.configure(ui, "com.target");
        check(!AppInputGuard.isBlocked(), "app should accept input");
        check(!AppInputGuard.blocksMotion(new MotionEvent(0, 500, 500)), "app point blocked");
        check(AppInputGuard.blocksMotion(new MotionEvent(0, 500, 1950)), "outside app accepted");
        AccessibilityWindowInfo ime=new AccessibilityWindowInfo("com.keyboard", false, false, 2, new Rect(0,1200,1000,1900));
        ui.windows=Arrays.asList(app, ime);
        check(AppInputGuard.isBlocked(), "unfocused keyboard accepted");
        check(AppInputGuard.suppressMotion(new MotionEvent(0,500,1500)), "keyboard DOWN accepted");
        ui.windows=Arrays.asList(app);
        check(AppInputGuard.suppressMotion(new MotionEvent(1,500,500)), "discarded gesture UP replayed");
        check(!AppInputGuard.suppressMotion(new MotionEvent(0,500,500)), "next app gesture blocked");
        AccessibilityWindowInfo overlay=new AccessibilityWindowInfo("com.other", false, false, 3, new Rect(200,200,400,400));
        ui.windows=Arrays.asList(app, overlay);
        check(AppInputGuard.blocksMotion(new MotionEvent(0,300,300)), "overlay click accepted");
        overlay.focused=true;
        check(AppInputGuard.isBlocked(), "external focus accepted");
        app.root.bounds=new Rect(0,50,1000,1900);
        ui.windows=Collections.emptyList();
        check(!AppInputGuard.isBlocked(), "target root blocked when window list is empty");
        check(!AppInputGuard.blocksMotion(new MotionEvent(0,500,500)),
                "target root point blocked when window list is empty");
        check(AppInputGuard.blocksMotion(new MotionEvent(0,500,1950)),
                "outside root accepted when window list is empty");
        ui.windows=null;
        check(AppInputGuard.isBlocked(), "unknown window list accepted");
        ui.windows=Arrays.asList(app); ui.root=null;
        check(AppInputGuard.isBlocked(), "unknown active window accepted");
        ui.root=new AccessibilityNodeInfo("com.launcher");
        check(AppInputGuard.isBlocked(), "launcher accepted");
        ui.root=app.root; ui.fail=true;
        check(AppInputGuard.isBlocked(), "failed window probe accepted");
        System.out.println("guard behavior verified");
    }
}
""",
    "TextHarness.java": """
import android.app.UiAutomation;
import android.graphics.Rect;
import android.view.accessibility.*;
import com.android.commands.monkey.events.base.*;
import java.util.*;
public class TextHarness {
    public static void main(String[] args) {
        UiAutomation ui=new UiAutomation();
        AccessibilityWindowInfo app=new AccessibilityWindowInfo("com.target",true,true,1,new Rect(0,0,1000,1900));
        AccessibilityNodeInfo field=new AccessibilityNodeInfo("com.target");
        field.editable=true; field.bounds=new Rect(100,100,500,200);
        app.root.children=new AccessibilityNodeInfo[]{field};
        ui.root=app.root; ui.windows=Arrays.asList(app);
        AppInputGuard.configure(ui,"com.target");
        new AppTextEvent(field.bounds,"first").injectEvent(null,null,0);
        GuardHarness.check("first".equals(field.value),"native text not set");
        Rect priorBounds=new Rect(field.bounds);
        field.bounds=new Rect(100,400,500,500); field.focused=true;
        new AppTextEvent(priorBounds,"second").injectEvent(null,null,0);
        GuardHarness.check("second".equals(field.value),"focused moved field not set");
        field.pkg="com.other";
        new AppTextEvent(null,"external").injectEvent(null,null,0);
        GuardHarness.check(field.writes==2,"external control received text");
        field.pkg="com.target";
        AccessibilityWindowInfo ime=new AccessibilityWindowInfo("com.keyboard",false,false,2,new Rect(0,1200,1000,1900));
        ui.windows=Arrays.asList(app,ime);
        new AppTextEvent(field.bounds,"keyboard").injectEvent(null,null,0);
        GuardHarness.check(field.writes==2,"visible IME received input");
        System.out.println("native text behavior verified");
    }
}
""",
}


class AndroidInputGuardTests(unittest.TestCase):
    def test_window_guard_behavior_without_a_device(self):
        java_home = Path(os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk1.8.0_261"))
        javac = shutil.which("javac") or str(java_home / "bin" / "javac.exe")
        java = shutil.which("java") or str(java_home / "bin" / "java.exe")
        if not Path(javac).is_file() or not Path(java).is_file():
            self.skipTest("JDK is required for the offline Java guard test")
        source_root = Path(__file__).resolve().parents[1] / "tools/fastbot_android15_patch"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = []
            for name, content in STUBS.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                sources.append(str(path))
            compiled = subprocess.run(
                [javac, "-encoding", "UTF-8", "-d", str(root),
                 str(source_root / "AppInputGuard.java"), str(source_root / "AppTextEvent.java"), *sources],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            result = subprocess.run(
                [java, "-cp", str(root), "GuardHarness"],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("guard behavior verified", result.stdout)
            result = subprocess.run(
                [java, "-cp", str(root), "TextHarness"],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("native text behavior verified", result.stdout)
