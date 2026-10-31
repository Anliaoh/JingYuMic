package com.xiaomusic.remote;

import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.widget.RemoteViews;

/** 2x2 播放卡片 */
public class WidgetCard extends AppWidgetProvider {

    @Override public void onUpdate(Context c, AppWidgetManager m, int[] ids) {
        for (int id : ids) {
        WidgetSync.ensure(c);
            int h = m.getAppWidgetOptions(id).getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 110);
            m.updateAppWidget(id, WidgetCore.card(c, h));
        }
    }

    @Override public void onAppWidgetOptionsChanged(Context c, AppWidgetManager m,
                                                    int id, Bundle o) {
        int h = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 110);
        m.updateAppWidget(id, WidgetCore.card(c, h));
    }

    @Override public void onReceive(Context c, Intent i) {
        String k = i == null ? null : i.getStringExtra(WidgetCore.E_KEY);
        if (i != null && WidgetCore.A_KEY.equals(i.getAction()) && k != null) {
            WidgetCore.key(c, k);
            return;
        }
        super.onReceive(c, i);
    }
}
