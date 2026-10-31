package com.xiaomusic.remote;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.PorterDuff;
import android.graphics.PorterDuffXfermode;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.drawable.Drawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 桌面小组件（播放条 / 播放卡片）的公共逻辑。
 *
 * 数据流：页面 JS 把「当前在放什么」推给 AppBridge.widgetState()，
 * 这里落到 SharedPreferences 并把封面存成文件，然后刷新小组件；
 * 小组件上的按钮发广播回来，直接转成页面里的 __widgetKey()，
 * 页面没在跑就先把 App 拉起来、等页面加载完再补发。
 */
final class WidgetCore {

    static final String SP = "widget";
    static final String A_KEY = "com.xiaomusic.remote.WIDGET_KEY";
    static final String E_KEY = "k";
    private static final String COVER_FILE = "wcover.png";

    private static final Handler UI = new Handler(Looper.getMainLooper());
    private static Bitmap sSrc;          // 封面原图缓存
    private static Bitmap sDef;          // App 图标兜底缓存
    private static long sSrcTs = -1;
    private static String sSrcPath = "";

    private WidgetCore() { }

    // ------------------------------------------------------------ 状态
    static void saveState(Context ctx, String json) {
        try {
            JSONObject o = new JSONObject(json);
            final Context app = ctx.getApplicationContext();
            SharedPreferences sp = app.getSharedPreferences(SP, Context.MODE_PRIVATE);
            // 两路数据都会调这里：页面 JS（字段全）和原生轮询（只有播放相关字段）。
            // 所以缺哪个字段就沿用上次存的值，别把设备/音量/面板冲回默认。
            String title = o.has("title") ? o.optString("title", "") : sp.getString("t", "");
            String artist = o.has("artist") ? o.optString("artist", "") : sp.getString("a", "");
            boolean playing = o.has("playing") ? o.optBoolean("playing", false) : sp.getBoolean("p", false);
            boolean has = o.has("has") ? o.optBoolean("has", false) : sp.getBoolean("hs", false);
            int pos = o.has("pos") ? o.optInt("pos", 0) : sp.getInt("pos", 0);
            int dur = o.has("dur") ? o.optInt("dur", 0) : sp.getInt("dur", 0);
            String cover = o.has("cover") ? o.optString("cover", "") : sp.getString("cu", "");
            String lrc = o.has("lrc") ? o.optString("lrc", "") : sp.getString("lrc", "");
            String lrc2 = o.has("lrc2") ? o.optString("lrc2", "") : sp.getString("lrc2", "");
            String dev = o.has("dev") ? o.optString("dev", "box") : sp.getString("dev", "box");
            int vol = o.has("vol") ? o.optInt("vol", 35) : sp.getInt("vol", 35);
            boolean multi = o.has("multi") ? o.optBoolean("multi", false) : sp.getBoolean("multi", false);
            String host = o.optString("host", "");
            String did = o.optString("did", "");

            String oldCover = sp.getString("cu", "");
            SharedPreferences.Editor e = sp.edit();
            e.putString("t", title)
                    .putString("a", artist)
                    .putBoolean("p", playing)
                    .putBoolean("hs", has)
                    .putInt("pos", Math.max(0, pos))
                    .putInt("dur", Math.max(0, dur))
                    .putString("cu", cover)
                    .putString("lrc", lrc)
                    .putString("lrc2", lrc2)
                    .putString("dev", dev)
                    .putBoolean("multi", multi)
                    .putInt("vol", Math.max(0, Math.min(100, vol)))
                    // 三个面板的数据（\n 分隔）：喜欢 / 已播 / 最近搜索词
                    .putString("f", o.has("fav") ? join(o, "fav") : sp.getString("f", ""))
                    .putString("r", o.has("rec") ? join(o, "rec") : sp.getString("r", ""))
                    .putString("kw", o.has("kw") ? join(o, "kw") : sp.getString("kw", ""));
            // 原生轮询要用的服务地址：页面推一次就记住，之后 App 关着也能自己拉
            if (!host.isEmpty()) e.putString("host", host);
            if (!did.isEmpty()) e.putString("did", did);
            // 页面每推一次就盖个时间戳：原生心跳看到它还新鲜就不插手，避免两边抢着写
            if (o.has("fgt")) e.putLong("fgt", o.optLong("fgt", 0));
            e.apply();

            if (!cover.equals(oldCover)) {
                if (cover.isEmpty()) {
                    deleteCover(app);
                    refresh(app);
                } else {
                    fetchCover(app, cover);
                }
            } else {
                refresh(app);
            }
        } catch (Throwable ignored) { }
    }

    private static void deleteCover(Context c) {
        try {
            new File(c.getFilesDir(), COVER_FILE).delete();
            sSrc = null;
            sSrcTs = -1;
        } catch (Exception ignored) { }
    }

    // ------------------------------------------------------------ 封面
    /** 封面下载到应用私有目录，只保留一张；失败就保持上一张 */
    private static void fetchCover(final Context app, final String url) {
        new Thread(new Runnable() {
            @Override public void run() {
                InputStream in = null;
                FileOutputStream fo = null;
                try {
                    Bitmap bmp = null;
                    if (url.startsWith("data:")) {
                        int comma = url.indexOf(',');
                        if (comma > 0) {
                            byte[] raw = Base64.decode(url.substring(comma + 1), Base64.DEFAULT);
                            bmp = BitmapFactory.decodeByteArray(raw, 0, raw.length);
                        }
                    } else if (url.startsWith("http")) {
                        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                        c.setConnectTimeout(4000);
                        c.setReadTimeout(7000);
                        c.setDoInput(true);
                        c.connect();
                        in = c.getInputStream();
                        bmp = BitmapFactory.decodeStream(in);
                    }
                    if (bmp == null) return;

                    Bitmap sq = square(bmp, 240);
                    File dir = app.getFilesDir();
                    File tmp = new File(dir, COVER_FILE + ".tmp");
                    fo = new FileOutputStream(tmp);
                    sq.compress(Bitmap.CompressFormat.PNG, 100, fo);
                    fo.close();
                    fo = null;
                    File dst = new File(dir, COVER_FILE);
                    if (dst.exists()) dst.delete();
                    tmp.renameTo(dst);
                    sSrc = sq;
                    sSrcTs = dst.lastModified();
                    sSrcPath = dst.getAbsolutePath();
                    refresh(app);
                } catch (Throwable ignored) {
                } finally {
                    try { if (in != null) in.close(); } catch (Exception ignored) { }
                    try { if (fo != null) fo.close(); } catch (Exception ignored) { }
                }
            }
        }).start();
    }

    /** 居中裁成正方形缩放到 size */
    private static Bitmap square(Bitmap src, int size) {
        int w = src.getWidth(), h = src.getHeight();
        int side = Math.min(w, h);
        int x = (w - side) / 2, y = (h - side) / 2;
        Bitmap crop = Bitmap.createBitmap(src, x, y, side, side);
        return Bitmap.createScaledBitmap(crop, size, size, true);
    }

    private static Bitmap source(Context c) {
        synchronized (WidgetCore.class) {
            File f = new File(c.getFilesDir(), COVER_FILE);
            if (!f.exists()) {
                sSrc = null;
                sSrcTs = -1;
            } else if (sSrc == null || sSrcTs != f.lastModified() || !f.getAbsolutePath().equals(sSrcPath)) {
                Bitmap b = BitmapFactory.decodeFile(f.getAbsolutePath());
                if (b != null) {
                    sSrc = b;
                    sSrcTs = f.lastModified();
                    sSrcPath = f.getAbsolutePath();
                }
            }
            if (sSrc != null) return sSrc;
            if (sDef != null) return sDef;
            sDef = appIcon(c);
            return sDef;
        }
    }

    /** 没有封面时用 App 图标兜底（自适应图标必须走 Drawable 绘制） */
    private static Bitmap appIcon(Context c) {
        try {
            Drawable d = c.getPackageManager().getApplicationIcon(c.getPackageName());
            int px = 240;
            Bitmap b = Bitmap.createBitmap(px, px, Bitmap.Config.ARGB_8888);
            Canvas cv = new Canvas(b);
            d.setBounds(0, 0, px, px);
            d.draw(cv);
            return b;
        } catch (Throwable e) {
            return null;
        }
    }

    /** 圆角方形封面 */
    private static Bitmap rounded(Context c, int dp, float radiusDp) {
        float d = c.getResources().getDisplayMetrics().density;
        int px = Math.max(1, (int) (dp * d));
        Bitmap src = source(c);
        if (src == null) return null;
        Bitmap s = Bitmap.createScaledBitmap(src, px, px, true);

        Bitmap out = Bitmap.createBitmap(px, px, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(out);
        Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        float r = radiusDp * d;
        canvas.drawRoundRect(new RectF(0, 0, px, px), r, r, p);
        p.setXfermode(new PorterDuffXfermode(PorterDuff.Mode.SRC_IN));
        canvas.drawBitmap(s, new Rect(0, 0, s.getWidth(), s.getHeight()),
                new Rect(0, 0, px, px), p);
        return out;
    }

    // ------------------------------------------------------------ 组装视图
    static RemoteViews bar(Context c) {
        RemoteViews v = new RemoteViews(c.getPackageName(), R.layout.widget_bar);
        bind(c, v, false, 0);
        v.setOnClickPendingIntent(R.id.wRoot, openPi(c, 101));
        v.setOnClickPendingIntent(R.id.wPrev, keyPi(c, "prev", 111));
        v.setOnClickPendingIntent(R.id.wPlay, keyPi(c, "toggle", 112));
        v.setOnClickPendingIntent(R.id.wNext, keyPi(c, "next", 113));
        return v;
    }

    static RemoteViews card(Context c, int minH) {
        RemoteViews v = new RemoteViews(c.getPackageName(), R.layout.widget_card);
        bind(c, v, true, minH);
        SharedPreferences sp = c.getSharedPreferences(SP, Context.MODE_PRIVATE);
        String[] fav = arr(sp, "f");
        String[] rec = arr(sp, "r");
        String[] kw = arr(sp, "kw");

        v.setOnClickPendingIntent(R.id.wRoot, openPi(c, 201));
        v.setOnClickPendingIntent(R.id.wPlay, keyPi(c, "toggle", 212));

        // 四个功能键：纯本地面板切换，不拉起 App，点完当场展开 / 再点收起
        v.setOnClickPendingIntent(R.id.wTabSch, tabPi(c, "sch", 231));
        v.setOnClickPendingIntent(R.id.wTabOut, tabPi(c, "out", 232));
        v.setOnClickPendingIntent(R.id.wTabFav, tabPi(c, "fav", 233));
        v.setOnClickPendingIntent(R.id.wTabRec, tabPi(c, "rec", 234));

        // 输出面板：设备芯片 / 环绕 / 音量步进
        v.setOnClickPendingIntent(R.id.wDevBox, keyPi(c, "dev:box", 221));
        v.setOnClickPendingIntent(R.id.wDevPhone, keyPi(c, "dev:phone", 222));
        v.setOnClickPendingIntent(R.id.wDevTv, keyPi(c, "dev:tv", 223));
        v.setOnClickPendingIntent(R.id.wDevPc, keyPi(c, "dev:pc", 224));
        v.setOnClickPendingIntent(R.id.wDevMulti, keyPi(c, "multi", 225));
        v.setOnClickPendingIntent(R.id.wVolDown, keyPi(c, "volm", 226));
        v.setOnClickPendingIntent(R.id.wVolUp, keyPi(c, "volp", 227));

        // 列表项：点哪首就播哪首
        int[] favIds = { R.id.wFav1, R.id.wFav2, R.id.wFav3 };
        for (int i = 0; i < favIds.length && i < fav.length; i++) {
            v.setOnClickPendingIntent(favIds[i], keyPi(c, "play:" + fav[i], 240 + i));
        }
        int[] recIds = { R.id.wRec1, R.id.wRec2, R.id.wRec3 };
        for (int i = 0; i < recIds.length && i < rec.length; i++) {
            v.setOnClickPendingIntent(recIds[i], keyPi(c, "play:" + rec[i], 250 + i));
        }
        if (kw.length > 0) v.setOnClickPendingIntent(R.id.wSch1, keyPi(c, "kw:" + kw[0], 261, true));
        if (kw.length > 1) v.setOnClickPendingIntent(R.id.wSch2, keyPi(c, "kw:" + kw[1], 262, true));
        v.setOnClickPendingIntent(R.id.wSchGo, keyPi(c, "search", 263, true));
        return v;
    }

    private static void bind(Context c, RemoteViews v, boolean card, int minH) {
        SharedPreferences sp = c.getSharedPreferences(SP, Context.MODE_PRIVATE);
        String title = sp.getString("t", "");
        String artist = sp.getString("a", "");
        boolean playing = sp.getBoolean("p", false);
        boolean has = sp.getBoolean("hs", false);
        int pos = sp.getInt("pos", 0);
        int dur = sp.getInt("dur", 0);
        String lrc = sp.getString("lrc", "");
        String lrc2 = sp.getString("lrc2", "");
        String dev = sp.getString("dev", "box");
        int vol = sp.getInt("vol", 35);
        boolean multi = sp.getBoolean("multi", false);
        String panel = sp.getString("panel", "");

        if (!has || title.isEmpty()) {
            title = c.getString(R.string.app_name);
            artist = c.getString(R.string.w_idle_sub);
            playing = false;
            lrc = "";
            lrc2 = "";
        } else if (artist == null || artist.isEmpty()) {
            artist = "未知歌手";
        }

        v.setTextViewText(R.id.wTitle, title);
        v.setTextViewText(R.id.wArtist, artist);
        v.setImageViewResource(R.id.wPlay, playing ? R.drawable.ic_w_pause : R.drawable.ic_w_play);
        v.setContentDescription(R.id.wPlay, c.getString(playing ? R.string.w_pause : R.string.w_play));

        Bitmap cover = rounded(c, card ? 50 : 38, card ? 13f : 10f);
        if (cover != null) v.setImageViewBitmap(R.id.wCover, cover);

        // 歌词行恰好就是「歌名」或「歌名 - 歌手」时不再显示，
        // 只认完全相等或紧跟着少量尾巴的情况，startsWith 太宽会误杀正常歌词
        String l0 = lrc == null ? "" : lrc.trim().toLowerCase();
        String t0 = title.trim().toLowerCase();
        if (t0.length() >= 2 && (l0.equals(t0) || (l0.startsWith(t0) && l0.length() <= t0.length() + 3))) {
            lrc = "";
        }

        if (!card) {
            // 4x1 播放条：只有一行歌词
            boolean hasLrc = lrc != null && !lrc.isEmpty();
            v.setViewVisibility(R.id.wLrc, hasLrc ? View.VISIBLE : View.GONE);
            if (hasLrc) {
                v.setTextViewText(R.id.wLrc, "♪  " + lrc);
                // 不要用 setBoolean(...,"setSelected",true) 触发 marquee：
                // MIUI 桌面把 TextView 换成 com.miui.miuiwidget.views.MIUIWidgetTextView，
                // 该类没开 RemoteViews 反射白名单，apply 时直接抛 ActionException，
                // 整张卡片会变成「载入窗口小组件时出现问题」。跑马灯交给布局里的
                // focusable + marquee 属性，滚动与否都只是视觉差异，不会崩。
            }
            return;
        }

        // ---------- 2x2 卡片 ----------
        // 细进度条（没有时间数字，位置让给歌词 / 面板）
        if (dur > 0) {
            v.setProgressBar(R.id.wProg, dur, Math.max(0, Math.min(pos, dur)), false);
        } else {
            v.setProgressBar(R.id.wProg, 100, 0, false);
        }

        // 功能键选中态：当前展开的那个用淡玫红底 + 玫红字
        tabOn(v, R.id.wTabSch, R.id.wTabSchT, "sch".equals(panel));
        tabOn(v, R.id.wTabOut, R.id.wTabOutT, "out".equals(panel));
        tabOn(v, R.id.wTabFav, R.id.wTabFavT, "fav".equals(panel));
        tabOn(v, R.id.wTabRec, R.id.wTabRecT, "rec".equals(panel));

        // 下半部分：默认歌词，点开功能键后原地换成对应面板
        v.setViewVisibility(R.id.wLrcBox, panel.isEmpty() ? View.VISIBLE : View.GONE);
        v.setViewVisibility(R.id.wPanelOut, "out".equals(panel) ? View.VISIBLE : View.GONE);
        v.setViewVisibility(R.id.wPanelFav, "fav".equals(panel) ? View.VISIBLE : View.GONE);
        v.setViewVisibility(R.id.wPanelRec, "rec".equals(panel) ? View.VISIBLE : View.GONE);
        v.setViewVisibility(R.id.wPanelSch, "sch".equals(panel) ? View.VISIBLE : View.GONE);

        if (panel.isEmpty()) {
            // 当前行 + 下一行，看上去就是跟着歌在往下滚；
            boolean h1 = lrc != null && !lrc.isEmpty();
            boolean h2 = lrc2 != null && !lrc2.isEmpty();
            // 同上：不用 setSelected 触发 marquee（MIUI 的自定义 TextView 会抛异常）
            v.setTextViewText(R.id.wLrc1, h1 ? ("♪  " + lrc) : (has ? "纯音乐 · 请欣赏" : "点这里开始播放"));
            v.setTextViewText(R.id.wLrc2, h2 ? lrc2 : "");
        } else if ("out".equals(panel)) {
            bindDevChip(v, R.id.wDevBox, "box", dev, c.getString(R.string.w_dev_box));
            bindDevChip(v, R.id.wDevPhone, "phone", dev, c.getString(R.string.w_dev_phone));
            bindDevChip(v, R.id.wDevTv, "tv", dev, c.getString(R.string.w_dev_tv));
            bindDevChip(v, R.id.wDevPc, "pc", dev, c.getString(R.string.w_dev_pc));
            // 环绕是开关，不是第 5 台设备
            v.setTextViewText(R.id.wDevMulti, "环绕");
            v.setTextColor(R.id.wDevMulti, multi ? 0xFFFFFFFF : 0xFF6B7688);
            v.setInt(R.id.wDevMulti, "setBackgroundResource",
                    multi ? R.drawable.w_chip_on : R.drawable.w_chip_n);
            v.setTextViewText(R.id.wVolVal, vol + "%");
        } else if ("fav".equals(panel)) {
            fillList(v, new int[]{ R.id.wFav1, R.id.wFav2, R.id.wFav3 },
                    arr(sp, "f"), "还没有喜欢的歌", "播放页点 ♡ 就收进来");
        } else if ("rec".equals(panel)) {
            fillList(v, new int[]{ R.id.wRec1, R.id.wRec2, R.id.wRec3 },
                    arr(sp, "r"), "还没有播放记录", "放一首歌就会出现在这里");
        } else if ("sch".equals(panel)) {
            String[] kw = arr(sp, "kw");
            String k0 = kw.length > 0 ? kw[0] : "";
            String k1 = kw.length > 1 ? kw[1] : "";
            // 还没有搜过词的时候不留空板：摆两句提示，顺便说明这块能干什么
            v.setTextViewText(R.id.wSch1, k0.isEmpty() ? "搜索本地 + 在线曲库" : "⌕  " + k0);
            v.setTextColor(R.id.wSch1, k0.isEmpty() ? 0xFF8B96A8 : 0xFF42506A);
            v.setTextViewText(R.id.wSch2, k1.isEmpty() ? "搜过的词会记在这里" : "⌕  " + k1);
            v.setTextColor(R.id.wSch2, k1.isEmpty() ? 0xFFB0B9CB : 0xFF42506A);
            v.setViewVisibility(R.id.wSch1, View.VISIBLE);
            v.setViewVisibility(R.id.wSch2, View.VISIBLE);
        }
    }

    /** 列表面板：有几首填几首，空的时候给两行提示（第一行当标题用） */
    private static void fillList(RemoteViews v, int[] ids, String[] names, String empty1, String empty2) {
        if (names.length == 0) {
            v.setTextViewText(ids[0], empty1);
            v.setTextColor(ids[0], 0xFF8B96A8);
            v.setViewVisibility(ids[0], View.VISIBLE);
            v.setTextViewText(ids[1], empty2);
            v.setTextColor(ids[1], 0xFFB0B9CB);
            v.setViewVisibility(ids[1], View.VISIBLE);
            v.setViewVisibility(ids[2], View.GONE);
            return;
        }
        for (int i = 0; i < ids.length; i++) {
            boolean on = i < names.length;
            v.setViewVisibility(ids[i], on ? View.VISIBLE : View.GONE);
            if (on) {
                v.setTextViewText(ids[i], "♪  " + names[i]);
                v.setTextColor(ids[i], 0xFF42506A);
            }
        }
    }

    /** 功能键选中态 */
    private static void tabOn(RemoteViews v, int box, int label, boolean on) {
        v.setTextColor(label, on ? 0xFFF2506E : 0xFF6B7688);
        v.setInt(box, "setBackgroundResource", on ? R.drawable.w_tab_on : R.drawable.w_tab_bg);
    }

    /** 设备芯片：当前输出设备玫红高亮（白字），其余白底浅灰字 */
    private static void bindDevChip(RemoteViews v, int id, String t, String cur, String label) {
        boolean on = t.equals(cur);
        v.setTextViewText(id, label);
        v.setTextColor(id, on ? 0xFFFFFFFF : 0xFF6B7688);
        v.setInt(id, "setBackgroundResource", on ? R.drawable.w_chip_on : R.drawable.w_chip_n);
    }

    /** SharedPreferences 里 \n 分隔的列表 */
    private static String[] arr(SharedPreferences sp, String key) {
        String s = sp.getString(key, "");
        if (s == null || s.isEmpty()) return new String[0];
        String[] raw = s.split("\n");
        java.util.ArrayList<String> out = new java.util.ArrayList<>();
        for (String x : raw) {
            if (x != null && !x.trim().isEmpty()) out.add(x);
        }
        return out.toArray(new String[0]);
    }

    /** JSON 里的数组 → \n 分隔串（最多留 3 条，小组件就显示三行） */
    private static String join(JSONObject o, String key) {
        try {
            org.json.JSONArray a = o.optJSONArray(key);
            if (a == null) return "";
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < a.length() && i < 3; i++) {
                String s = a.optString(i, "");
                if (s == null || s.trim().isEmpty()) continue;
                if (sb.length() > 0) sb.append('\n');
                sb.append(s.replace('\n', ' '));
            }
            return sb.toString();
        } catch (Throwable e) {
            return "";
        }
    }

    // ------------------------------------------------------------ 面板状态
    static String panel(Context c) {
        return c.getSharedPreferences(SP, Context.MODE_PRIVATE).getString("panel", "");
    }

    static void setPanel(Context c, String p) {
        c.getSharedPreferences(SP, Context.MODE_PRIVATE).edit()
                .putString("panel", p == null ? "" : p).apply();
    }

    private static String fmt(int sec) {
        if (sec < 0) sec = 0;
        return (sec / 60) + ":" + (sec % 60 < 10 ? "0" : "") + (sec % 60);
    }

    private static String nameOf(String t) {
        if ("box".equals(t)) return "音箱";
        if ("phone".equals(t)) return "手机";
        if ("tv".equals(t)) return "电视";
        if ("pc".equals(t)) return "电脑";
        return t == null ? "" : t;
    }

    private static PendingIntent openPi(Context c, int req) {
        Intent i = new Intent(c, MainActivity.class);
        i.setAction(Intent.ACTION_MAIN);
        i.addCategory(Intent.CATEGORY_LAUNCHER);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP
                | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int fl = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) fl |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getActivity(c, req, i, fl);
    }

    /**
     * 按键走「透明中转页」而不是广播：广播接收器调 startActivity 会被
     * 后台启动限制拦掉，App 已被杀时点按钮就没反应了。
     */
    private static PendingIntent keyPi(Context c, String k, int req) {
        return keyPi(c, k, req, false);
    }

    /** fore=true：执行完还要把 App 翻到前台（搜索这类「得看见结果」的动作） */
    private static PendingIntent keyPi(Context c, String k, int req, boolean fore) {
        Intent i = new Intent(c, WidgetKeyActivity.class).putExtra(E_KEY, k);
        if (fore) i.putExtra("fore", true);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP
                | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int fl = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) fl |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getActivity(c, req, i, fl);
    }

    /**
     * 功能键走广播，不走中转页：面板切换是纯本地动作（改一下记忆 + 重画卡片），
     * 不需要页面参与，用户点完当场就能看到下半部分换内容，App 也不用被拉起来。
     */
    private static PendingIntent tabPi(Context c, String tab, int req) {
        Intent i = new Intent(c, WidgetTapReceiver.class)
                .setAction(WidgetTapReceiver.ACTION)
                .putExtra(WidgetTapReceiver.E_TAB, tab);
        int fl = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) fl |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getBroadcast(c, req, i, fl);
    }

    // ------------------------------------------------------------ 刷新
    static void refresh(final Context c) {
        final Context app = c.getApplicationContext();
        UI.post(new Runnable() {
            @Override public void run() {
                try {
                    AppWidgetManager m = AppWidgetManager.getInstance(app);
                    int[] bars = m.getAppWidgetIds(new ComponentName(app, WidgetBar.class));
                    if (bars != null && bars.length > 0) {
                        RemoteViews v = bar(app);
                        for (int id : bars) m.updateAppWidget(id, v);
                    }
                    int[] cards = m.getAppWidgetIds(new ComponentName(app, WidgetCard.class));
                    if (cards != null && cards.length > 0) {
                        for (int id : cards) {
                            Bundle opts = m.getAppWidgetOptions(id);
                            int h = opts.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 110);
                            m.updateAppWidget(id, card(app, h));
                        }
                    }
                    boolean any = (bars != null && bars.length > 0) || (cards != null && cards.length > 0);
                    if (any) WidgetSync.ensure(app); else WidgetSync.stop(app);
                } catch (Throwable ignored) { }
            }
        });
    }

    // ------------------------------------------------------------ 按钮
    static void key(Context c, String k) {
        MainActivity.deliverKey(c.getApplicationContext(), k);
    }
}
