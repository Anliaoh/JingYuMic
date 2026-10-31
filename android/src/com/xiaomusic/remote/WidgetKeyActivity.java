package com.xiaomusic.remote;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

/**
 * 小组件按钮的透明中转页。
 *
 * 为什么需要它：小组件的点击目标如果是广播接收器，收到广播时进程处于
 * 「后台 RECEIVER」状态，Android 10+ 会直接拦掉 startActivity
 * （日志里是 Background activity launch blocked），于是 App 已经被杀掉时
 * 点播放就只有「没反应」。改成由 PendingIntent.getActivity 拉起这个不显示
 * 界面的空 Activity，它算「有可见窗口的调用方」，再去拉主界面就允许了。
 *
 * App 还活着的话，这里只是把按键转发给页面，然后立刻 finish，用户看不到任何东西。
 */
public class WidgetKeyActivity extends Activity {

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        Intent it = getIntent();
        String k = it == null ? null : it.getStringExtra(WidgetCore.E_KEY);
        // fore：这个键要人看见结果（比如「搜索」得把搜索页翻出来），
        // 光在页面里切 tab 是不够的，还得把 App 拉到前台。
        boolean fore = it != null && it.getBooleanExtra("fore", false);
        if (k != null) {
            try { MainActivity.deliverKey(this, k); } catch (Throwable ignored) { }
        }
        if (fore) {
            try {
                Intent m = new Intent(this, MainActivity.class);
                m.setAction(Intent.ACTION_MAIN);
                m.addCategory(Intent.CATEGORY_LAUNCHER);
                m.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP
                        | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                startActivity(m);
            } catch (Throwable ignored) { }
        }
        finish();
        overridePendingTransition(0, 0);
    }
}
