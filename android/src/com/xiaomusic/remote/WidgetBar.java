package com.xiaomusic.remote;

import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.widget.RemoteViews;

/** 4x1 播放条 */
public class WidgetBar extends AppWidgetProvider {

    @Override public void onUpdate(Context c, AppWidgetManager m, int[] ids) {
        RemoteViews v = WidgetCore.bar(c);
        for (int id : ids) m.updateAppWidget(id, v);
        WidgetSync.ensure(c);
    }

    @Override public void onAppWidgetOptionsChanged(Context c, AppWidgetManager m,
                                                    int id, Bundle o) {
        m.updateAppWidget(id, WidgetCore.bar(c));
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
