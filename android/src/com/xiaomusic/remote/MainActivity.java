package com.xiaomusic.remote;

import android.app.Activity;
import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.Icon;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity {

    private static final String PORT = "58090";
    private static final String PAGE = "/static/remote.html";
    /** 页面版本号：每次发版 +1，用于绕过 WebView 缓存 */
    // v3.95：改了 remote.html 的页面结构（设置面板重整），页面戳必须 +1，
    // 否则 App 会拿 WebView 缓存里的旧页面，用户以为改了没生效。
    private static final String PAGE_VER = "59";
    private static final String DEF_HOST = "192.168.1.100";
    private static final String BG = "#0c0f14";
    private static final String ACCENT = "#f2506e";
    private static final long RETRY_MS = 5000L;
    /** 一直连不上时，等多久才进「软件内的连接设置」页 */
    private static final long FALLBACK_MS = 12000L;

    private WebView web;
    private FrameLayout root;
    private LinearLayout errBox;
    private EditText hostInput;
    private View tipView;
    private TextView tipText;
    private String host;
    private boolean pageOk = false;
    private boolean mainError = false;
    /** 当前 WebView 里显示的是「连接设置」兜底页（不算连上） */
    private boolean fallbackOn = false;
    /** 本次启动已经进过兜底设置页：之后再失败就直接回设置页，别来回闪加载页 */
    private boolean setupSeen = false;
    private long lastBack = 0L;
    /** <input type="file"> 的回调（页面「从相册选图」用） */
    private static final int REQ_PICK = 4321;
    private ValueCallback<Uri[]> fileCb;
    /** 本次启动已经试过的地址，避免反复重连死循环 */
    private final java.util.HashSet<String> tried = new java.util.HashSet<String>();
    private static MainActivity live;
    /** 小组件点按钮时如果页面还没就绪，先存这里，等 onPageFinished 再发 */
    private String pendingWa = null;

    private final Handler ui = new Handler(Looper.getMainLooper());

    /** 桌面入口 alias，顺序要和页面里的图标列表一致 */
    private static final String[] ICON_ALIASES = {
            "IconMaidNavy", "IconChibiNavy"
    };

    private final Runnable retryTask = new Runnable() {
        @Override public void run() {
            if (web == null) return;
            if (!pageOk) {
                if (!tryNext()) {          // 所有候选都试过了，从头再来一轮
                    tried.clear();
                    tried.add(host);
                    /* 兜底设置页正露着时别把它刷掉：用户可能在填地址，
                       只有扫到新地址（tryNext 成功）才值得切走 */
                    if (!fallbackOn) web.loadUrl(urlOf(host));
                }
            }
            ui.postDelayed(this, RETRY_MS);
        }
    };

    /** 长时间连不上：不再干等，进「软件内的连接设置」页让用户自己配 */
    private final Runnable fallbackTask = new Runnable() {
        @Override public void run() {
            if (!pageOk && !fallbackOn) showSetupPage();
        }
    };

    /** 每次真正去连一个地址时，重设「多久算连不上」的计时 */
    private void armFallback() {
        ui.removeCallbacks(fallbackTask);
        ui.postDelayed(fallbackTask, FALLBACK_MS);
    }

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        live = this;
        if (b == null) pendingWa = getIntent().getStringExtra("wa");
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(Color.parseColor(BG));
        getWindow().setNavigationBarColor(Color.parseColor(BG));

        SharedPreferences sp = getSharedPreferences("cfg", MODE_PRIVATE);
        host = sp.getString("host", DEF_HOST);

        // 启动时静默对齐桌面图标（升级后旧的图标索引可能越界）
        final int savedIcon = sp.getInt("icon_idx", 0);
        ui.post(new Runnable() {
            @Override public void run() { applyIcon(savedIcon, true); }
        });

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor(BG));

        WebView.setWebContentsDebuggingEnabled(true);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor(BG));
        web.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadsImagesAutomatically(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(false);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // 页面是本地服务实时渲染的，绝不能用缓存：否则服务端更新后 App 仍显示旧页面
        // （曾因缓存导致扫码登录成功后界面仍显示离线，排查半天）
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setUserAgentString(s.getUserAgentString() + " XiaoRemote/1.0");

        web.addJavascriptInterface(new Bridge(), "AppBridge");
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb,
                                             FileChooserParams params) {
                if (fileCb != null) fileCb.onReceiveValue(null);
                fileCb = cb;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("image/*");
                try {
                    startActivityForResult(Intent.createChooser(i, "选择图片"), REQ_PICK);
                    return true;
                } catch (Exception e) {
                    fileCb = null;
                    return false;
                }
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                Uri u = req.getUrl();
                if (u == null) return false;
                String sch = u.getScheme() == null ? "" : u.getScheme();
                if (sch.startsWith("http")) {
                    if (isOurs(u)) return false;
                    openOutside(u);
                    return true;
                }
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true;
            }

            @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap fav) {
                mainError = false;
            }

            @Override public void onPageFinished(WebView v, String url) {
                if (mainError) return;
                /* 兜底「连接设置」页加载完成不算连上服务，别停掉重连、也别记 last_ok */
                if (fallbackOn) return;
                if (tipView != null) tipView.setVisibility(View.GONE);
                pageOk = true;
                setupSeen = false;
                tried.clear();
                tried.add(host);
                getSharedPreferences("cfg", MODE_PRIVATE).edit()
                        .putString("last_ok", host).putString("host", host).apply();
                ui.removeCallbacks(retryTask);
                ui.removeCallbacks(fallbackTask);
                if (errBox != null) errBox.setVisibility(View.GONE);
                flushWidgetKey();
            }

            @Override public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) {
                    pageOk = false;
                    mainError = true;
                    onMainFail();
                }
            }

            @Override public void onReceivedHttpError(WebView v, WebResourceRequest req, android.webkit.WebResourceResponse res) {
                if (req.isForMainFrame()) {
                    pageOk = false;
                    mainError = true;
                    onMainFail();
                }
            }
        });

        /* 加载页 = 启动页观感：图标 + 品牌名 + 版本号 + 转圈 + 状态。
           实心底色是必须的 —— 主框架加载失败时 WebView 会显示自带的错误页，
           加载页要整个盖住它 */
        float dp = getResources().getDisplayMetrics().density;
        LinearLayout loading = new LinearLayout(this);
        loading.setOrientation(LinearLayout.VERTICAL);
        loading.setGravity(Gravity.CENTER_HORIZONTAL);
        loading.setBackgroundColor(Color.parseColor(BG));
        loading.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        /* 启动页整体垂直居中：上下各垫一块等高的空白（Gravity.CENTER 在有
           MATCH_PARENT 子项时会失效，这里用 weight 的 spacer 更稳） */
        View spaceTop = new View(this);
        loading.addView(spaceTop, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        ImageView logoV = new ImageView(this);
        logoV.setImageResource(R.mipmap.ic_launcher);
        int logoSize = (int) (72 * dp);
        LinearLayout.LayoutParams llp = new LinearLayout.LayoutParams(logoSize, logoSize);
        llp.bottomMargin = (int) (14 * dp);
        loading.addView(logoV, llp);

        TextView brandV = new TextView(this);
        brandV.setText(R.string.app_name);
        brandV.setTextColor(Color.parseColor("#e8eef7"));
        brandV.setTextSize(19f);
        brandV.setGravity(Gravity.CENTER);
        brandV.setTypeface(Typeface.DEFAULT_BOLD);
        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        blp.bottomMargin = (int) (4 * dp);
        loading.addView(brandV, blp);

        TextView verV = new TextView(this);
        verV.setText("v" + getVersionName());
        verV.setTextColor(Color.parseColor("#59657a"));
        verV.setTextSize(12f);
        verV.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams vlp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        vlp.bottomMargin = (int) (34 * dp);
        loading.addView(verV, vlp);

        ProgressBar spin = new ProgressBar(this);
        spin.setIndeterminate(true);
        LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        slp.bottomMargin = (int) (14 * dp);
        loading.addView(spin, slp);

        TextView loadingText = new TextView(this);
        loadingText.setText("正在连接遥控服务…");
        loadingText.setTextColor(Color.parseColor("#8b98ab"));
        loadingText.setTextSize(13f);
        loadingText.setGravity(Gravity.CENTER);
        loading.addView(loadingText);

        View spaceBot = new View(this);
        loading.addView(spaceBot, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        tipView = loading;
        tipText = loadingText;

        buildErrorView();

        root.addView(web);
        root.addView(loading);
        root.addView(errBox);
        errBox.setVisibility(View.GONE);
        setContentView(root);

        loadBest();
        // 启动即后台扫一遍局域网，把发现的服务地址并入候选；
        // 若当前页面连不上，发现后自动切换过去
        asyncDiscover(true);
    }

    /** 读取候选地址列表（第一个是首选） */
    private java.util.List<String> loadAddrs() {
        SharedPreferences sp = getSharedPreferences("cfg", MODE_PRIVATE);
        java.util.List<String> out = new java.util.ArrayList<String>();
        String raw = sp.getString("addrs", "");
        if (raw != null) {
            for (String p : raw.split("[,\\n]")) {
                String x = normAddr(p);
                if (!x.isEmpty() && !out.contains(x)) out.add(x);
            }
        }
        if (out.isEmpty()) {
            String d = normAddr(sp.getString("host", DEF_HOST));
            out.add(d.isEmpty() ? "http://" + DEF_HOST + ":" + PORT : d);
        }
        return out;
    }

    private void saveAddrs(java.util.List<String> list) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) sb.append(',');
            sb.append(list.get(i));
        }
        getSharedPreferences("cfg", MODE_PRIVATE).edit().putString("addrs", sb.toString()).apply();
    }

    /** 启动时选一个地址：优先上次成功的，其次列表第一个 */
    private void loadBest() {
        java.util.List<String> list = loadAddrs();
        String prefer = getSharedPreferences("cfg", MODE_PRIVATE).getString("last_ok", "");
        String start = list.contains(prefer) ? prefer : list.get(0);
        tried.clear();
        tried.add(start);
        host = start;
        getSharedPreferences("cfg", MODE_PRIVATE).edit().putString("host", start).apply();
        pageOk = false;
        mainError = false;
        fallbackOn = false;
        ui.removeCallbacks(retryTask);
        ui.postDelayed(retryTask, RETRY_MS);
        armFallback();
        web.loadUrl(urlOf(start));
    }

    /** 当前地址失败时换下一个候选，全部试过返回 false */
    private boolean tryNext() {
        for (String a : loadAddrs()) {
            if (!tried.contains(a)) {
                tried.add(a);
                host = a;
                getSharedPreferences("cfg", MODE_PRIVATE).edit()
                        .putString("host", a).putString("last_ok", a).apply();
                if (hostInput != null) hostInput.setText(a);
                fallbackOn = false;
                armFallback();
                web.loadUrl(urlOf(a));
                return true;
            }
        }
        // 候选全失败：后台扫一遍局域网，找到新地址会自动续接
        asyncDiscover(true);
        return false;
    }

    /* ---------------- 服务器地址自动获取 ----------------
       平板是 DHCP 拿 IP，会变。App 启动/换网/候选全挂时，
       自动扫描所在网段的 58090 端口找到 xiaomusic 服务。 */

    private long lastDiscoverTs = 0L;

    /** 取本机所在网段前缀，如 "192.168.1."；拿不到返回 "" */
    private String subnetPrefix() {
        try {
            for (java.util.Enumeration<java.net.NetworkInterface> en =
                     java.net.NetworkInterface.getNetworkInterfaces(); en.hasMoreElements(); ) {
                java.util.Enumeration<java.net.InetAddress> addrs = en.nextElement().getInetAddresses();
                while (addrs.hasMoreElements()) {
                    java.net.InetAddress a = addrs.nextElement();
                    if (!a.isLoopbackAddress() && a instanceof java.net.Inet4Address) {
                        String h = a.getHostAddress();
                        int dot = h.lastIndexOf('.');
                        if (dot > 0) return h.substring(0, dot + 1);
                    }
                }
            }
        } catch (Exception ignored) { }
        return "";
    }

    /** 探测 ip:58090 是否是 xiaomusic 服务 */
    private boolean probeHost(String ip) {
        java.net.HttpURLConnection c = null;
        try {
            c = (java.net.HttpURLConnection) new java.net.URL(
                    "http://" + ip + ":" + PORT + "/getstatus").openConnection();
            c.setConnectTimeout(400);
            c.setReadTimeout(600);
            return c.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) try { c.disconnect(); } catch (Exception ignored) { }
        }
    }

    /** 扫描 /24 网段（约 3-6 秒），返回所有响应的服务地址 */
    private java.util.List<String> discoverSync(long budgetMs) {
        java.util.List<String> out = new java.util.ArrayList<String>();
        String prefix = subnetPrefix();
        if (prefix.isEmpty()) return out;
        final java.util.List<String> found =
            java.util.Collections.synchronizedList(new java.util.ArrayList<String>());
        java.util.concurrent.ExecutorService pool =
            java.util.concurrent.Executors.newFixedThreadPool(24);
        for (int i = 1; i <= 254; i++) {
            final String ip = prefix + i;
            pool.submit(new Runnable() { @Override public void run() {
                if (probeHost(ip)) found.add("http://" + ip + ":" + PORT);
            }});
        }
        pool.shutdown();
        try { pool.awaitTermination(budgetMs, java.util.concurrent.TimeUnit.MILLISECONDS); }
        catch (Exception ignored) { }
        if (!pool.isTerminated()) pool.shutdownNow();
        out.addAll(found);
        return out;
    }

    /**
     * 后台扫描局域网并合并进候选列表；kickTry 时若页面还没连上，
     * 让 tryNext 立即使用新发现的服务地址。20 秒内不重复扫。
     */
    private void asyncDiscover(final boolean kickTry) {
        long now = System.currentTimeMillis();
        if (now - lastDiscoverTs < 20000L) return;
        lastDiscoverTs = now;
        new Thread(new Runnable() { @Override public void run() {
            java.util.List<String> found = discoverSync(6000);
            if (found.isEmpty()) {
                ui.post(new Runnable() { @Override public void run() {
                    tellSetup("这个网段里没找到服务，确认平板已开机、服务已启动", false);
                }});
                return;
            }
            java.util.List<String> list = loadAddrs();
            boolean changed = false;
            for (int i = found.size() - 1; i >= 0; i--) {
                if (!list.contains(found.get(i))) { list.add(0, found.get(i)); changed = true; }
            }
            if (changed) saveAddrs(list);
            if (kickTry && !pageOk) {
                ui.post(new Runnable() { @Override public void run() {
                    if (!pageOk) tryNext();
                }});
            }
        }}).start();
    }

    /** 页面通过 AppBridge 调用，切换桌面图标 */
    public class Bridge {
        @JavascriptInterface
        public int iconIndex() {
            return getSharedPreferences("cfg", MODE_PRIVATE).getInt("icon_idx", 0);
        }

        /* ---- 兜底「连接设置」页用 ---- */

        @JavascriptInterface
        public String hostNow() {
            return host == null ? "" : host;
        }

        /** 页面里填了地址点「连接」 */
        @JavascriptInterface
        public void connectTo(final String addr) {
            ui.post(new Runnable() { @Override public void run() { connect(addr); } });
        }

        /** 页面里点「重新扫描局域网」：立刻扫一遍，不等 20 秒节流 */
        @JavascriptInterface
        public void scanNow() {
            ui.post(new Runnable() { @Override public void run() {
                lastDiscoverTs = 0L;
                asyncDiscover(true);
            }});
        }

        @JavascriptInterface
        public void setIcon(final int idx) {
            ui.post(new Runnable() {
                @Override public void run() { applyIcon(idx); }
            });
        }

        /**
         * 自定义图标：页面把合成好的 PNG(dataURL) 传进来，交给系统「固定到桌面」。
         * 系统级换图标在 Android 10+ 已经不可能（activity-alias 只能靠资源），
         * 所以自定义图标走 ShortcutManager 的 pin 快捷方式 —— 效果就是桌面上多一个
         * 长得跟图一样的入口，点开还是这个 App。
         * 返回 "ok" 或 "err:原因"。
         */
        @JavascriptInterface
        public String addIcon(String name, String dataUrl) {
            try {
                if (dataUrl == null || dataUrl.isEmpty()) return "err:没有图片数据";
                int comma = dataUrl.indexOf(',');
                String b64 = comma >= 0 ? dataUrl.substring(comma + 1) : dataUrl;
                byte[] raw = Base64.decode(b64, Base64.DEFAULT);
                Bitmap bmp = BitmapFactory.decodeByteArray(raw, 0, raw.length);
                if (bmp == null) return "err:图片解析失败";
                if (name == null || name.trim().isEmpty()) name = "鲸屿Mic";
                name = name.trim();
                if (name.length() > 12) name = name.substring(0, 12);

                if (Build.VERSION.SDK_INT < 26) return "err:系统版本太低";
                ShortcutManager sm = (ShortcutManager) getSystemService(SHORTCUT_SERVICE);
                if (sm == null) return "err:拿不到快捷方式服务";
                if (!sm.isRequestPinShortcutSupported()) return "err:这台设备的桌面不支持固定图标";

                Intent launch = new Intent(MainActivity.this, MainActivity.class);
                launch.setAction(Intent.ACTION_MAIN);
                launch.addCategory(Intent.CATEGORY_LAUNCHER);
                launch.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);

                ShortcutInfo si = new ShortcutInfo.Builder(MainActivity.this, "jy" + System.currentTimeMillis())
                        .setShortLabel(name)
                        .setLongLabel(name)
                        .setIcon(Icon.createWithBitmap(bmp))
                        .setIntent(launch)
                        .build();
                return sm.requestPinShortcut(si, null) ? "ok" : "err:系统拒绝了固定请求";
            } catch (Throwable e) {
                return "err:" + e.getMessage();
            }
        }

        /** 页面把「现在在放什么」推过来，小组件跟着变 */
        @JavascriptInterface
        public void widgetState(String json) {
            try { WidgetCore.saveState(MainActivity.this, json); } catch (Throwable ignored) { }
        }

        /** 一键把小组件钉到桌面（kind = bar / card） */
        @JavascriptInterface
        public String pinWidget(final String kind) {
            try {
                if (Build.VERSION.SDK_INT < 26) return "err:系统版本太低，请长按桌面手动添加";
                final AppWidgetManager m = AppWidgetManager.getInstance(MainActivity.this);
                if (!m.isRequestPinAppWidgetSupported())
                    return "err:这个桌面不支持一键添加，长按桌面 → 小组件 → 鲸屿Mic";
                final ComponentName cn = new ComponentName(MainActivity.this,
                        "card".equals(kind) ? WidgetCard.class : WidgetBar.class);
                WidgetCore.refresh(MainActivity.this);
                ui.post(new Runnable() {
                    @Override public void run() {
                        try { m.requestPinAppWidget(cn, null, null); } catch (Throwable ignored) { }
                    }
                });
                return "ok";
            } catch (Throwable e) {
                return "err:" + e.getMessage();
            }
        }

        @JavascriptInterface
        public void toastMsg(final String msg) {
            ui.post(new Runnable() {
                @Override public void run() { toast(msg); }
            });
        }

        /**
         * 原生壳版本号。
         * v3.95（元修复 M3）：以前这里写死 return "3.22"，APK 实际已经是 3.24 ——
         * 版本号漂移，而且 remote.html 里根本没人调它（等于死代码）。
         * 现在改成读真实包名版本，关于页才显示得对。
         */
        @JavascriptInterface
        public String version() {
            return getVersionName();
        }

        /**
         * 复制文本到剪贴板。
         * 页面是 http://192.168.x.x 这种非安全上下文，navigator.clipboard 在低版本
         * WebView 里不可用，关于页「复制版本信息」必须有原生兜底，否则用户报障贴不出东西。
         *
         * @return "1" 成功，"0" 失败（JS 侧见 0 会继续退回 execCommand / 手动选中）
         */
        @JavascriptInterface
        public String copyText(final String text) {
            if (text == null) return "0";
            try {
                android.content.ClipboardManager cm =
                        (android.content.ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                if (cm == null) return "0";
                cm.setPrimaryClip(android.content.ClipData.newPlainText("jingyumic", text));
                return "1";
            } catch (Exception e) {
                return "0";
            }
        }

        /**
         * 打开账号「直接授权登录」页（AuthActivity）：
         * 内嵌 WebView 让用户直接输账号密码（含验证码），
         * 授权成功后自动提取 token 写入服务端并重启，无需第二台设备扫码。
         */
        @JavascriptInterface
        public void openAuth() {
            try {
                startActivity(new Intent(MainActivity.this, AuthActivity.class));
            } catch (Exception e) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        Toast.makeText(MainActivity.this,
                                "无法打开授权页：" + e.getMessage(), Toast.LENGTH_LONG).show();
                    }
                });
            }
        }

        /**
         * 唤起其它 App。pkg 必传；activity 可传空串（传空则打开 App 主页）。
         * 典型用法：openApp("com.xiaomi.smarthome",
         *                  "com.xiaomi.qrcode.ScanBarcodeActivity") 直接打开家庭中枢扫一扫。
         * 返回 "ok" 或 "err:原因"
         */
        @JavascriptInterface
        public String openApp(final String pkg, final String activity) {
            if (pkg == null || pkg.isEmpty()) return "err:包名为空";
            try {
                Intent i = null;
                if (activity != null && !activity.isEmpty()) {
                    i = new Intent(Intent.ACTION_MAIN);
                    i.setComponent(new ComponentName(pkg, activity));
                    i.addCategory(Intent.CATEGORY_LAUNCHER);
                } else {
                    i = getPackageManager().getLaunchIntentForPackage(pkg);
                }
                if (i == null) return "err:未安装 " + pkg;
                i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
                return "ok";
            } catch (Exception e) {
                // 指定组件打不开（被隐藏/改名/无权限）就退化为打开 App 主页
                try {
                    Intent f = getPackageManager().getLaunchIntentForPackage(pkg);
                    if (f == null) return "err:未安装 " + pkg;
                    f.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(f);
                    return "ok";
                } catch (Exception e2) {
                    return "err:" + (e2.getMessage() == null ? e2.toString() : e2.getMessage());
                }
            }
        }

        /* ---------------- 多地址（家里 / 外网） ---------------- */

        @JavascriptInterface
        public String host() {
            return host == null ? "" : host;
        }

        @JavascriptInterface
        public String addrs() {
            StringBuilder sb = new StringBuilder();
            java.util.List<String> list = loadAddrs();
            for (int i = 0; i < list.size(); i++) {
                if (i > 0) sb.append('\n');
                sb.append(list.get(i));
            }
            return sb.toString();
        }

        /** 传入换行/逗号分隔的地址列表，第一个为首选 */
        @JavascriptInterface
        public void setAddrs(final String csv) {
            java.util.List<String> list = new java.util.ArrayList<String>();
            if (csv != null) {
                for (String p : csv.split("[,\\n]")) {
                    String x = normAddr(p);
                    if (!x.isEmpty() && !list.contains(x)) list.add(x);
                }
            }
            if (list.isEmpty()) list.add("http://" + DEF_HOST + ":" + PORT);
            saveAddrs(list);
            getSharedPreferences("cfg", MODE_PRIVATE).edit().remove("last_ok").apply();
            ui.post(new Runnable() {
                @Override public void run() { loadBest(); }
            });
        }

        @JavascriptInterface
        public void reconnect() {
            ui.post(new Runnable() {
                @Override public void run() { loadBest(); }
            });
        }

        /* ---------------- 手机本地播放（原生播放器） ---------------- */

        @JavascriptInterface
        public boolean hasLocalPlayer() {
            return true;
        }

        @JavascriptInterface
        public void localPlay(final String url, final String title, final String artist, final String cover) {
            ui.post(new Runnable() {
                @Override public void run() {
                    try {
                        // 一律通过 Intent 派发：Service 可能还没创建（首次播放），
                        // 直接读 PlayerService.inst 会拿到 null，导致这一首被丢掉。
                        Intent i = new Intent(MainActivity.this, PlayerService.class);
                        i.setAction("play");
                        i.putExtra("url", url);
                        i.putExtra("title", title);
                        i.putExtra("artist", artist);
                        i.putExtra("cover", cover);
                        startService(i);
                    } catch (Exception e) {
                        toast("本地播放失败：" + e.getMessage());
                    }
                }
            });
        }

        @JavascriptInterface
        public void localToggle() {
            ui.post(new Runnable() {
                @Override public void run() {
                    if (PlayerService.inst == null) return;
                    if (PlayerService.stateCode() == PlayerService.ST_PLAY) PlayerService.inst.pause();
                    else PlayerService.inst.resume();
                }
            });
        }

        @JavascriptInterface
        public void localPause() {
            ui.post(new Runnable() {
                @Override public void run() {
                    if (PlayerService.inst != null) PlayerService.inst.pause();
                }
            });
        }

        @JavascriptInterface
        public void localStop() {
            ui.post(new Runnable() {
                @Override public void run() {
                    if (PlayerService.inst != null) PlayerService.inst.stopSelfPlay();
                }
            });
        }

        @JavascriptInterface
        public void localSeek(final double sec) {
            ui.post(new Runnable() {
                @Override public void run() {
                    if (PlayerService.inst != null) PlayerService.inst.seekToMs((long) (sec * 1000));
                }
            });
        }

        @JavascriptInterface
        public void localVolume(final int pct) {
            ui.post(new Runnable() {
                @Override public void run() {
                    if (PlayerService.inst != null) PlayerService.inst.setVolumePct(pct);
                }
            });
        }

        @JavascriptInterface
        public String localState() {
            return PlayerService.stateJson();
        }
    }

    /** 原生播放器回调页面（切歌结束、系统媒体键） */
    public static void callJs(final String js) {
        final MainActivity a = live;
        if (a == null || a.web == null) return;
        a.ui.post(new Runnable() {
            @Override public void run() {
                try {
                    if (a.web != null) {
                        a.web.evaluateJavascript("(function(){try{" + js + "}catch(e){}})()", null);
                    }
                } catch (Exception ignored) { }
            }
        });
    }

    /** 小组件上的按钮 → 页面里的 __widgetKey()；页面没在跑就先拉起 App */
    public static void deliverKey(Context c, String k) {
        if (k == null || k.isEmpty()) return;
        MainActivity a = live;
        if (a != null && a.pageOk && a.web != null) {
            callJs("__widgetKey(" + org.json.JSONObject.quote(k) + ")");
            return;
        }
        try {
            Intent i = new Intent(c, MainActivity.class);
            i.setAction(Intent.ACTION_MAIN);
            i.addCategory(Intent.CATEGORY_LAUNCHER);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP
                    | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            i.putExtra("wa", k);
            c.startActivity(i);
        } catch (Exception ignored) { }
    }

    private void flushWidgetKey() {
        final String k = pendingWa;
        pendingWa = null;
        if (k == null) return;
        ui.postDelayed(new Runnable() {
            @Override public void run() { callJs("__widgetKey(" + org.json.JSONObject.quote(k) + ")"); }
        }, 260);
    }

    @Override protected void onNewIntent(Intent it) {
        super.onNewIntent(it);
        setIntent(it);
        String k = it == null ? null : it.getStringExtra("wa");
        if (k == null) return;
        if (pageOk) callJs("__widgetKey(" + org.json.JSONObject.quote(k) + ")");
        else pendingWa = k;
    }

    /** 相册选图回来的结果，交还给 <input type="file"> */
    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_PICK) {
            Uri[] out = null;
            if (res == RESULT_OK && data != null) {
                Uri u = data.getData();
                if (u != null) {
                    out = new Uri[]{ u };
                } else if (data.getClipData() != null) {
                    int n = data.getClipData().getItemCount();
                    out = new Uri[Math.min(n, 1)];
                    for (int i = 0; i < out.length; i++) {
                        out[i] = data.getClipData().getItemAt(i).getUri();
                    }
                }
            }
            if (fileCb != null) {
                fileCb.onReceiveValue(out);
                fileCb = null;
            }
            return;
        }
        super.onActivityResult(req, res, data);
    }

    private void applyIcon(int idx) {
        applyIcon(idx, false);
    }

    private void applyIcon(int idx, boolean quiet) {
        if (ICON_ALIASES.length == 0) return;
        if (idx < 0) idx = 0;
        if (idx >= ICON_ALIASES.length) idx = ICON_ALIASES.length - 1;
        String pkg = getPackageName();
        PackageManager pm = getPackageManager();
        ComponentName target = new ComponentName(pkg, pkg + "." + ICON_ALIASES[idx]);
        try {
            pm.setComponentEnabledSetting(target,
                    PackageManager.COMPONENT_ENABLED_STATE_ENABLED, PackageManager.DONT_KILL_APP);
            for (int i = 0; i < ICON_ALIASES.length; i++) {
                if (i == idx) continue;
                ComponentName cn = new ComponentName(pkg, pkg + "." + ICON_ALIASES[i]);
                if (pm.getComponentEnabledSetting(cn) != PackageManager.COMPONENT_ENABLED_STATE_DISABLED) {
                    pm.setComponentEnabledSetting(cn,
                            PackageManager.COMPONENT_ENABLED_STATE_DISABLED, PackageManager.DONT_KILL_APP);
                }
            }
            getSharedPreferences("cfg", MODE_PRIVATE).edit().putInt("icon_idx", idx).apply();
            if (!quiet) toast("图标已切换，桌面稍等几秒会自动更新");
        } catch (Exception e) {
            toast("切换失败：" + e.getMessage());
        }
    }

    private boolean isOurs(Uri u) {
        String h = u.getHost();
        if (h == null) return false;
        int p = u.getPort();
        if (p == 58090 || p == 58091) return true;
        if (h.equalsIgnoreCase(hostOnly(host))) return true;
        for (String a : loadAddrs()) {
            if (h.equalsIgnoreCase(hostOnly(a))) return true;
        }
        return false;
    }

    private void openOutside(Uri u) {
        try { startActivity(new Intent(Intent.ACTION_VIEW, u)); }
        catch (Exception e) { toast("没有可打开该链接的应用"); }
    }

    /** 归一化成 http(s)://host[:port]；http 默认补 58090，https 走默认端口 */
    private String normAddr(String raw) {
        String x = raw == null ? "" : raw.trim();
        if (x.isEmpty()) return "";
        /* 中文输入法会把 : / . 敲成全角（：／ 。），地址必须先掰回半角 */
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < x.length(); i++) {
            char ch = x.charAt(i);
            if (ch >= 0xFF01 && ch <= 0xFF5E) ch = (char) (ch - 0xFEE0);
            else if (ch == 0x3002) ch = '.';
            else if (ch == 0xFF0F) ch = '/';
            sb.append(ch);
        }
        x = sb.toString().replaceAll("\\s+", "");
        if (x.isEmpty()) return "";
        String scheme = "http://";
        if (x.startsWith("https://")) { scheme = "https://"; x = x.substring(8); }
        else if (x.startsWith("http://")) { x = x.substring(7); }
        int slash = x.indexOf('/');
        if (slash >= 0) x = x.substring(0, slash);
        if (x.isEmpty()) return "";
        int c = x.lastIndexOf(':');
        boolean hasPort = c > 0 && c > x.lastIndexOf(']') && x.substring(c + 1).matches("\\d+");
        if (!hasPort && "http://".equals(scheme)) x = x + ":" + PORT;
        return scheme + x;
    }

    private String hostOnly(String a) {
        try { return java.net.URI.create(normAddr(a)).getHost(); } catch (Exception e) { return ""; }
    }

    private String urlOf(String h) {
        String b = normAddr(h);
        if (b.isEmpty()) b = "http://" + DEF_HOST + ":" + PORT;
        // ?v= 随版本递增，双保险逼 WebView/中间层重新取页
        return b + PAGE + "?v=" + PAGE_VER;
    }

    private int dp(float v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    private void buildErrorView() {
        errBox = new LinearLayout(this);
        errBox.setOrientation(LinearLayout.VERTICAL);
        errBox.setGravity(Gravity.CENTER);
        errBox.setBackgroundColor(Color.parseColor(BG));
        int pad = dp(30);
        errBox.setPadding(pad, pad, pad, pad);
        errBox.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        TextView icon = new TextView(this);
        icon.setText("⚡");
        icon.setTextSize(44f);
        icon.setGravity(Gravity.CENTER);
        errBox.addView(icon);

        TextView title = new TextView(this);
        title.setText("连不上遥控服务");
        title.setTextColor(Color.parseColor("#eef3fa"));
        title.setTextSize(21f);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        tp.topMargin = dp(10);
        errBox.addView(title, tp);

        TextView sub = new TextView(this);
        sub.setText("请确认平板上的服务正在运行：\n在 Termux 里执行  ~/start_xiaomusic.sh\n本机会每 5 秒自动重试。");
        sub.setTextColor(Color.parseColor("#8b98ab"));
        sub.setTextSize(13.5f);
        sub.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams sp2 = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sp2.topMargin = dp(12);
        errBox.addView(sub, sp2);

        hostInput = new EditText(this);
        hostInput.setText(host);
        hostInput.setHint("服务地址，如 192.168.1.100");
        hostInput.setHintTextColor(Color.parseColor("#5d6a7d"));
        hostInput.setTextColor(Color.parseColor("#eef3fa"));
        hostInput.setTextSize(14f);
        hostInput.setSingleLine(true);
        hostInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        hostInput.setImeOptions(EditorInfo.IME_ACTION_GO);
        GradientDrawable box = new GradientDrawable();
        box.setColor(Color.parseColor("#161c25"));
        box.setCornerRadius(dp(12));
        box.setStroke(dp(1), Color.parseColor("#2a3444"));
        hostInput.setBackground(box);
        hostInput.setPadding(dp(14), dp(11), dp(14), dp(11));
        LinearLayout.LayoutParams ip = new LinearLayout.LayoutParams(dp(260), ViewGroup.LayoutParams.WRAP_CONTENT);
        ip.topMargin = dp(20);
        errBox.addView(hostInput, ip);

        Button retry = new Button(this);
        retry.setText("重新连接");
        retry.setTextSize(15f);
        retry.setTextColor(Color.WHITE);
        retry.setAllCaps(false);
        GradientDrawable rb = new GradientDrawable();
        rb.setColor(Color.parseColor(ACCENT));
        rb.setCornerRadius(dp(12));
        retry.setBackground(rb);
        LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(dp(260), dp(46));
        rp.topMargin = dp(14);
        errBox.addView(retry, rp);
        retry.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { connect(hostInput.getText().toString()); }
        });

        Button quitter = new Button(this);
        quitter.setText("退出");
        quitter.setTextSize(14f);
        quitter.setTextColor(Color.parseColor("#8b98ab"));
        quitter.setAllCaps(false);
        quitter.setBackgroundColor(Color.TRANSPARENT);
        LinearLayout.LayoutParams qp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        qp.topMargin = dp(4);
        errBox.addView(quitter, qp);
        quitter.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });

        hostInput.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override public boolean onEditorAction(TextView v, int actionId, android.view.KeyEvent e) {
                connect(hostInput.getText().toString());
                return true;
            }
        });
    }

    private void connect(String raw) {
        String h = normAddr(raw);
        if (h.isEmpty()) h = "http://" + DEF_HOST + ":" + PORT;
        host = h;
        tried.clear();
        tried.add(h);
        java.util.List<String> list = loadAddrs();
        list.remove(h);
        list.add(0, h);
        saveAddrs(list);
        getSharedPreferences("cfg", MODE_PRIVATE).edit()
                .putString("host", h).putString("last_ok", h).apply();
        pageOk = false;
        mainError = false;
        fallbackOn = false;
        if (errBox != null) errBox.setVisibility(View.GONE);
        if (tipText != null) tipText.setText("正在连接遥控服务…");
        if (tipView != null) tipView.setVisibility(View.VISIBLE);
        ui.removeCallbacks(retryTask);
        ui.postDelayed(retryTask, RETRY_MS);
        armFallback();
        web.loadUrl(urlOf(h));
    }

    /** 连不上时不弹报错页：显示「正在连接」加载页，重试交给 retryTask / asyncDiscover */
    private void showConnecting() {
        if (tipText != null) tipText.setText("正在连接遥控服务…");
        if (tipView != null) tipView.setVisibility(View.VISIBLE);
        if (errBox != null) errBox.setVisibility(View.GONE);
    }

    /**
     * 主框架加载失败的统一去处：先换个候选地址再试（这就是「自动连接」），
     * 确实没人应答时，短时间里先停在加载页；等过一会儿还是不行，
     * 就进「软件内的连接设置」页让用户自己填地址（由 fallbackTask 触发）。
     */
    private void onMainFail() {
        if (tryNext()) return;
        if (fallbackOn) return;            // 设置页正露着，别用加载页盖掉它
        if (setupSeen) { showSetupPage(); return; }
        showConnecting();
    }

    /** 读 res/raw 里的兜底设置页 */
    private String readRaw(int id) {
        try {
            java.io.InputStream in = getResources().openRawResource(id);
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            in.close();
            return new String(bos.toByteArray(), "UTF-8");
        } catch (Exception e) {
            return null;
        }
    }

    /** 启动页角落里显示的版本号 */
    private String getVersionName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "";
        }
    }

    /**
     * 一直连不上：把「连接设置」页塞进 WebView —— 它长得跟软件里其它页面一样，
     * 用户在软件内就能改地址 / 重新扫描，而不是卡在一个报错框上。
     * （base URL 仍然是服务地址，这样页面里的相对请求和 isOurs 判定都不变）
     */
    private void showSetupPage() {
        if (web == null || fallbackOn) return;
        String html = readRaw(R.raw.setup);
        if (html == null) { showConnecting(); return; }
        fallbackOn = true;
        setupSeen = true;
        if (tipView != null) tipView.setVisibility(View.GONE);
        if (errBox != null) errBox.setVisibility(View.GONE);
        String msg = "自动连接了几次都没通";
        try {
            html = html.replace("__HOST__", host)
                       .replace("__MSG__", msg);
        } catch (Exception ignored) { }
        web.loadDataWithBaseURL(host.endsWith("/") ? host : host + "/",
                html, "text/html", "utf-8", null);
    }

    /** 扫描结束后，把结果说给兜底设置页听 */
    private void tellSetup(final String msg, final boolean good) {
        if (!fallbackOn) return;
        callJs("typeof window.__setupMsg==='function' && window.__setupMsg("
                + org.json.JSONObject.quote(msg) + "," + (good ? "'ok'" : "'err'") + ")");
    }

    /* 报错页（闪电 + 地址输入 + 重新连接）已按用户要求停用：
       buildErrorView() 仍保留结构但没有任何调用方，errBox 永远隐藏 */
    private void showError() {
        if (errBox == null) return;
        if (errBox.getVisibility() == View.VISIBLE) return;
        errBox.setVisibility(View.VISIBLE);
        if (hostInput != null && !hostInput.hasFocus()) hostInput.setText(host);
        ui.removeCallbacks(retryTask);
        ui.postDelayed(retryTask, RETRY_MS);
    }

    private void toast(String m) {
        Toast.makeText(this, m, Toast.LENGTH_SHORT).show();
    }

    @Override public void onBackPressed() {
        if (errBox != null && errBox.getVisibility() == View.VISIBLE) {
            finish();
            return;
        }
        web.evaluateJavascript(
                "(function(){try{return (window.__appBack&&window.__appBack())||'exit';}catch(e){return 'exit';}})()",
                new android.webkit.ValueCallback<String>() {
                    @Override public void onReceiveValue(String value) {
                        String v = value == null ? "" : value.replace("\"", "").trim();
                        if ("exit".equals(v) || v.isEmpty()) {
                            long now = System.currentTimeMillis();
                            if (now - lastBack < 2000) { finish(); }
                            else { lastBack = now; toast("再按一次返回键退出"); }
                        }
                    }
                });
    }

    @Override protected void onResume() {
        super.onResume();
        if (web != null) { web.onResume(); }
        if (!pageOk && web != null) {
            ui.removeCallbacks(retryTask);
            ui.postDelayed(retryTask, 600);
            armFallback();
            /* 要是正停在设置页上，回到前台就顺手再扫一遍，能连通就自动走 */
            if (fallbackOn) { lastDiscoverTs = 0L; asyncDiscover(true); }
        }
    }

    @Override protected void onPause() {
        // 注意：这里不能调用 web.pauseTimers()，否则退到后台/熄屏后页面 JS 停摆，
        // 播完一首就不会自动切下一首、状态也不会再轮询。
        if (web != null) { web.onPause(); }
        super.onPause();
    }

    @Override protected void onDestroy() {
        ui.removeCallbacksAndMessages(null);
        if (live == this) live = null;
        if (web != null) {
            web.setWebChromeClient(null);
            web.setWebViewClient(null);
            root.removeView(web);
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
