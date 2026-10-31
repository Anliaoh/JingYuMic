package com.xiaomusic.remote;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 账号「直接授权登录」页：
 * 内嵌 WebView 打开账号登录页，用户在此输账号密码（含验证码），
 * 登录成功的回调 URL 里带 passToken/userId，拦截提取后写入服务端。
 * 一台设备即可完成，不需要第二台手机扫码。
 */
public class AuthActivity extends Activity {
    private static final String TAG = "AuthActivity";
    /** 登录 UI 页：清过旧 cookie 后会显示账号密码/二维码表单 */
    private static final String LOGIN_URL =
            "https://account.xiaomi.com/fe/service/login?sid=micoapi&_banner=false";
    private static final String SAVE_URL = "http://127.0.0.1:58091/api/save_login";

    private WebView web;
    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private boolean done = false;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#F7F8FA"));
        root.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // 顶栏
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(Color.WHITE);
        bar.setPadding(24, 8, 24, 8);
        TextView title = new TextView(this);
        title.setText("账号登录");
        title.setTextSize(17);
        title.setTextColor(Color.parseColor("#1A1F2B"));
        LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        title.setLayoutParams(tp);
        Button close = new Button(this);
        close.setText("关闭");
        close.setAllCaps(false);
        close.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) { finish(); }
        });
        bar.addView(title);
        bar.addView(close);
        root.addView(bar);

        // WebView
        web = new WebView(this);
        web.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setSupportMultipleWindows(false);
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // 上游 sts 接口会拒绝默认 WebView UA，伪装成常见手机浏览器
        s.setUserAgentString(
                "Mozilla/5.0 (Linux; Android 15; 23046RP50C Build/AQ3A.080812.002) "
                        + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36");
        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(web, true);
        // 过期的旧会话会让 serviceLogin「假成功」（ticket=0）直接跳错误页，
        // 先清掉才能看到登录表单
        cm.removeAllCookies(null);
        cm.flush();
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                String url = req.getUrl().toString();
                Log.i(TAG, "nav: " + url);
                // 登录成功会 302 到带 passToken 的地址，拦截提取
                if (isCallback(url)) {
                    handleCallback(url);
                    return true;
                }
                return false;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                // 兜底：个别机型/版本的 302 不走 shouldOverrideUrlLoading
                if (isCallback(url)) handleCallback(url);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                // 兜底：有的登录流程不把 passToken 放 URL，而是写进 cookie。
                // 登录完成后延迟一下再读，等 cookie 写盘。
                view.postDelayed(new Runnable() {
                    @Override public void run() { checkCookies(); }
                }, 1200);
            }
        });
        root.addView(web);
        setContentView(root);

        web.loadUrl(LOGIN_URL);
    }

    /** 是否是登录成功回调（同时带 userId 与 passToken 的跳转地址） */
    private boolean isCallback(String url) {
        return url != null && url.contains("userId=") && url.contains("passToken");
    }

    /** 从 cookie 里找登录凭证：登录成功后 account.xiaomi.com 会写 passToken/userId cookie */
    private void checkCookies() {
        if (done) return;
        try {
            String cs = CookieManager.getInstance().getCookie("https://account.xiaomi.com");
            if (cs == null) return;
            String passToken = null, userId = null;
            for (String kv : cs.split(";")) {
                String t = kv.trim();
                int eq = t.indexOf('=');
                if (eq <= 0) continue;
                String k = t.substring(0, eq), v = t.substring(eq + 1);
                if (k.equals("passToken") && v.length() > 10) passToken = v;
                if (k.equals("userId") && !v.isEmpty()) userId = v;
            }
            if (passToken != null && userId != null) {
                Log.i(TAG, "cookies contain login token, userId=" + userId);
                done = true;
                submitLogin(passToken, userId);
            }
        } catch (Exception e) {
            Log.e(TAG, "checkCookies error", e);
        }
    }

    /** 从回调 URL 提取登录凭证 */
    private void handleCallback(String url) {
        if (done) return;
        try {
            android.net.Uri u = android.net.Uri.parse(url);
            String passToken = u.getQueryParameter("passToken");
            String userId = u.getQueryParameter("userId");
            if (passToken == null || userId == null
                    || passToken.isEmpty() || userId.isEmpty()) {
                Log.w(TAG, "callback missing token fields");
                return;
            }
            done = true;
            submitLogin(passToken, userId);
        } catch (Exception e) {
            Log.e(TAG, "handleCallback error", e);
        }
    }

    /** 把登录凭证 POST 给 musictool 落盘并重启 xiaomusic，然后退出本页 */
    private void submitLogin(final String passToken, final String userId) {
        Toast.makeText(this, "授权成功，正在恢复服务…", Toast.LENGTH_LONG).show();
        final JSONObject body = new JSONObject();
        try {
            body.put("passToken", passToken);
            body.put("userId", userId);
        } catch (Exception e) {
            Log.e(TAG, "build body error", e);
        }
        pool.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    HttpURLConnection c = (HttpURLConnection) new URL(SAVE_URL).openConnection();
                    c.setRequestMethod("POST");
                    c.setDoOutput(true);
                    c.setConnectTimeout(8000);
                    c.setReadTimeout(15000);
                    c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                    OutputStream os = c.getOutputStream();
                    os.write(body.toString().getBytes("utf-8"));
                    os.close();
                    BufferedReader r = new BufferedReader(
                            new InputStreamReader(c.getInputStream(), "utf-8"));
                    StringBuilder sb = new StringBuilder();
                    String line;
                    while ((line = r.readLine()) != null) sb.append(line);
                    r.close();
                    Log.i(TAG, "save_login resp: " + sb);
                } catch (Exception e) {
                    Log.e(TAG, "save_login failed", e);
                }
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        Toast.makeText(AuthActivity.this,
                                "登录完成，返回遥控器即可", Toast.LENGTH_LONG).show();
                        finish();
                    }
                });
            }
        });
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
