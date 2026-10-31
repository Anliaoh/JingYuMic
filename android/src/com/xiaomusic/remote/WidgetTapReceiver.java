package com.xiaomusic.remote;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 小组件四个功能键（搜索 / 输出 / 喜欢 / 已播）的开关。
 *
 * 只做本地的「下半部分换成哪个面板」，不碰播放、不拉起 App：
 * 点完立刻重画卡片，再点同一个键就收起回到歌词。
 * 真正要干活的动作（切设备、点歌、去搜索）走 WidgetKeyActivity 那条路，
 * 因为它们需要页面参与，而广播接收器里 startActivity 会被后台限制拦掉。
 */
public class WidgetTapReceiver extends BroadcastReceiver {

    static final String ACTION = "com.xiaomusic.remote.WIDGET_TAB";
    static final String E_TAB = "tab";

    @Override public void onReceive(Context ctx, Intent it) {
        if (ctx == null || it == null || !ACTION.equals(it.getAction())) return;
        String tab = it.getStringExtra(E_TAB);
        if (tab == null || tab.isEmpty()) return;
        try {
            Context app = ctx.getApplicationContext();
            String cur = WidgetCore.panel(app);
            WidgetCore.setPanel(app, tab.equals(cur) ? "" : tab);
            WidgetCore.refresh(app);
        } catch (Throwable ignored) { }
    }
}
