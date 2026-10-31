package com.xiaomusic.remote;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * 小组件的原生心跳。
 *
 * 之前小组件的数据全靠页面 JS 每 2 秒推一次，App 一旦被划掉/被系统回收，
 * 桌面上的歌词和「已播」就永远停在最后一帧——歌还在音箱里放着，小组件却不动了。
 * 这里在 App 进程里自己拉 /api/widget（服务端已经算好当前歌词行），
 * 只要桌面还有鲸屿的小组件就常驻，不依赖 WebView。
 *
 * 关键修复（v4.19）：原本是普通后台 Service，靠主线程 Handler.postDelayed 每 3 秒一拍。
 * Android 8+ 把后台应用的服务会随时被系统杀掉/冻结（尤其 MIUI 这类激进回收的 ROM），
 * App 一退到后台，这个定时器就停了——歌词、进度、封面全卡在最后一帧（用户说的"又卡住"）。
 * 现在改成【前台服务 + 常驻低优先级通知】，进程不会被回收；并且把调度从主线程 Handler
 * 挪到独立线程池（ScheduledExecutorService），即便 WebView 在后台被冻结也不影响心跳。
 */
public class WidgetSync extends Service {

    static final String TAG = "JYWidgetSync";

    /** 3 秒一拍：歌词一句通常 4~8 秒，够跟得上，又不会太费电 */
    private static final long PERIOD = 3000L;
    private static final int NOTIFY_ID = 9031;
    private static final String CHANNEL = "widget_sync";

    private volatile boolean running = false;
    private ScheduledExecutorService sched;
    private ScheduledFuture<?> future;

    static void ensure(Context c) {
        try {
            Context app = c.getApplicationContext();
            Intent i = new Intent(app, WidgetSync.class);
            // Android 8+ 起，后台进程用 startService 会被 IllegalStateException 挡掉；
            // 前台服务要用 startForegroundService，系统给 5 秒窗口让你 startForeground。
            if (Build.VERSION.SDK_INT >= 26) app.startForegroundService(i);
            else app.startService(i);
        } catch (Throwable e) {
            Log.w(TAG, "start service failed: " + e);
        }
    }

    static void stop(Context c) {
        try {
            c.getApplicationContext().stopService(new Intent(c.getApplicationContext(), WidgetSync.class));
        } catch (Throwable ignored) { }
    }

    @Override public void onCreate() {
        super.onCreate();
        running = true;
        startForegroundSafe();
        sched = Executors.newSingleThreadScheduledExecutor();
        future = sched.scheduleWithFixedDelay(new Runnable() {
            @Override public void run() {
                if (!running) return;
                try { poll(); } catch (Throwable ignored) { }
            }
        }, 500, PERIOD, TimeUnit.MILLISECONDS);
    }

    @Override public int onStartCommand(Intent i, int flags, int startId) {
        // 系统按 START_STICKY 把被杀掉的服务拉回来时，onCreate 已重建调度；
        // 但如果只对 onStartCommand 重入（极端情况），兜底再起一次。
        if (!running) {
            running = true;
            startForegroundSafe();
            if (sched == null || sched.isShutdown()) {
                sched = Executors.newSingleThreadScheduledExecutor();
                future = sched.scheduleWithFixedDelay(new Runnable() {
                    @Override public void run() {
                        if (!running) return;
                        try { poll(); } catch (Throwable ignored) { }
                    }
                }, 300, PERIOD, TimeUnit.MILLISECONDS);
            }
        }
        return START_STICKY;
    }

    @Override public void onDestroy() {
        running = false;
        if (future != null) { try { future.cancel(false); } catch (Throwable ignored) { } }
        if (sched != null) { try { sched.shutdownNow(); } catch (Throwable ignored) { } }
        sched = null;
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent i) { return null; }

    /** 提升为前台服务：挂一个低优先级常驻通知，保证进程不被后台回收/冻结 */
    private void startForegroundSafe() {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            Notification n;
            if (Build.VERSION.SDK_INT >= 26) {
                if (nm != null) {
                    NotificationChannel ch = nm.getNotificationChannel(CHANNEL);
                    if (ch == null) {
                        ch = new NotificationChannel(CHANNEL, "播放同步", NotificationManager.IMPORTANCE_LOW);
                        ch.setShowBadge(false);
                        ch.setLockscreenVisibility(Notification.VISIBILITY_SECRET);
                        nm.createNotificationChannel(ch);
                    }
                }
                n = new Notification.Builder(this, CHANNEL)
                        .setContentTitle("鲸屿 Mic")
                        .setContentText("正在同步播放状态到桌面小组件")
                        .setSmallIcon(getApplicationInfo().icon)
                        .setOngoing(true)
                        .setCategory(Notification.CATEGORY_SERVICE)
                        .setShowWhen(false)
                        .build();
            } else {
                n = new Notification.Builder(this)
                        .setContentTitle("鲸屿 Mic")
                        .setContentText("正在同步播放状态到桌面小组件")
                        .setSmallIcon(getApplicationInfo().icon)
                        .setOngoing(true)
                        .setShowWhen(false)
                        .build();
            }
            startForeground(NOTIFY_ID, n);
        } catch (Throwable e) {
            // 不能静默：Android 14/15 上少权限/少 foregroundServiceType 会在这里抛，
            // 服务退化成后台服务 → App 一退后台就被冻结 → 小组件进度/歌词卡住（v4.23 就是这么卡的）。
            Log.w(TAG, "startForeground failed: " + e);
        }
    }

    private void poll() {
        Context app = getApplicationContext();
        SharedPreferences sp = app.getSharedPreferences(WidgetCore.SP, Context.MODE_PRIVATE);
        String host = sp.getString("host", "");
        if (host == null || host.isEmpty()) return;      // 页面还没推过状态，等下一拍
        // 页面还在前台推着（8 秒内写过）：以它为准，别两边打架
        if (System.currentTimeMillis() - sp.getLong("fgt", 0L) < 8000L) return;
        String did = sp.getString("did", "");
        String url = "http://" + host + ":58091/api/widget"
                + (did == null || did.isEmpty() ? "" : "?did=" + Uri.encode(did));

        HttpURLConnection c = null;
        BufferedReader r = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(2500);
            c.setReadTimeout(3500);
            c.setRequestProperty("Accept", "application/json");
            if (c.getResponseCode() != 200) return;
            r = new BufferedReader(new InputStreamReader(c.getInputStream(), "utf-8"));
            StringBuilder sb = new StringBuilder();
            String ln;
            while ((ln = r.readLine()) != null) sb.append(ln);
            JSONObject o = new JSONObject(sb.toString());
            if (!o.optBoolean("ok", false)) return;
            WidgetCore.saveState(app, o.toString());
        } catch (Throwable ignored) {
        } finally {
            try { if (r != null) r.close(); } catch (Exception ignored) { }
            if (c != null) c.disconnect();
        }
    }
}
