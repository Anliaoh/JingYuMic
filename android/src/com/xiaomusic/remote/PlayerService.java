package com.xiaomusic.remote;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.drawable.Icon;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaMetadata;
import android.media.MediaPlayer;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 手机本地播放器：MediaPlayer + MediaSession + 前台通知
 * 系统媒体控制（锁屏 / 通知栏 / 蓝牙 / 耳机线控）都用这套原生能力，
 * 也就是"手机自带的播放器"的效果。
 */
public class PlayerService extends Service {

    public static final String CH_ID = "jy_play";

    public static final int ST_IDLE = 0;
    public static final int ST_PREP = 1;
    public static final int ST_PLAY = 2;
    public static final int ST_PAUSE = 3;

    public static volatile PlayerService inst;

    private MediaPlayer mp;
    private MediaSession session;
    private AudioManager am;
    private PowerManager.WakeLock wake;
    private final Handler ui = new Handler(Looper.getMainLooper());

    // 音频焦点请求只建一次：每次 resume 都新建一个 request，系统会给旧的 listener
    // 发 AUDIOFOCUS_LOSS，我们自己的回调于是把刚恢复的播放又按回暂停（踩过）。
    private AudioFocusRequest focusReq;
    private AudioManager.OnAudioFocusChangeListener focusListener;

    private String curUrl = "";
    private String curTitle = "";
    private String curArtist = "";
    private String curCover = "";
    private int state = ST_IDLE;
    private float vol = 1f;
    private Bitmap art;
    private String artFor = "";

    private final MediaSession.Callback cb = new MediaSession.Callback() {
        @Override public void onPlay() { resume(); }
        @Override public void onPause() { pause(); }
        @Override public void onStop() { stopSelfPlay(); }
        @Override public void onSkipToNext() { MainActivity.callJs("__onMediaKey('next')"); }
        @Override public void onSkipToPrevious() { MainActivity.callJs("__onMediaKey('prev')"); }
        @Override public void onSeekTo(long pos) { seekToMs(pos); }
    };

    @Override public void onCreate() {
        super.onCreate();
        inst = this;
        am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        createChannel();
        session = new MediaSession(this, "JingYuMic");
        session.setCallback(cb);
        session.setFlags(MediaSession.FLAG_HANDLES_MEDIA_BUTTONS
                | MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS);
        session.setActive(true);
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "jingyu:play");
    }

    @Override public int onStartCommand(Intent it, int flags, int startId) {
        if (it != null && it.getAction() != null) {
            String a = it.getAction();
            if (a.equals("play")) {
                play(it.getStringExtra("url"), it.getStringExtra("title"),
                        it.getStringExtra("artist"), it.getStringExtra("cover"));
            }
            else if (a.equals("toggle")) { if (state == ST_PLAY) pause(); else resume(); }
            else if (a.equals("pause")) pause();
            else if (a.equals("resume")) resume();
            else if (a.equals("stop")) stopSelfPlay();
            else if (a.equals("next")) MainActivity.callJs("__onMediaKey('next')");
            else if (a.equals("prev")) MainActivity.callJs("__onMediaKey('prev')");
        }
        return START_STICKY;
    }

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public void onDestroy() {
        releasePlayer();
        try { session.release(); } catch (Exception ignored) { }
        if (wake != null && wake.isHeld()) { try { wake.release(); } catch (Exception ignored) { } }
        inst = null;
        super.onDestroy();
    }

    // ------------------------------------------------------------ 播放控制
    public void play(String url, String title, String artist, String cover) {
        if (url == null || url.isEmpty()) return;
        boolean sameUrl = url.equals(curUrl) && mp != null && state != ST_IDLE;
        curUrl = url;
        curTitle = title == null ? "" : title;
        curArtist = artist == null ? "" : artist;
        curCover = cover == null ? "" : cover;
        if (sameUrl) {                 // 同一首歌：继续播，不重头开始
            resume();
            return;
        }
        releasePlayer();
        requestFocus();
        state = ST_PREP;
        pushState();
        try {
            mp = new MediaPlayer();
            mp.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build());
            mp.setDataSource(url);
            mp.setOnPreparedListener(new MediaPlayer.OnPreparedListener() {
                @Override public void onPrepared(MediaPlayer p) {
                    state = ST_PLAY;
                    p.setVolume(vol, vol);
                    p.start();
                    pushState();
                    if (wake != null && !wake.isHeld()) wake.acquire(6 * 60 * 60 * 1000L);
                }
            });
            mp.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override public void onCompletion(MediaPlayer p) {
                    state = ST_PAUSE;
                    pushState();
                    MainActivity.callJs("__onLocalEnded()");
                }
            });
            mp.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                @Override public boolean onError(MediaPlayer p, int what, int extra) {
                    state = ST_IDLE;
                    pushState();
                    MainActivity.callJs("__onLocalError(" + what + ")");
                    return true;
                }
            });
            mp.prepareAsync();
            loadArt(cover);
        } catch (Exception e) {
            state = ST_IDLE;
            pushState();
            MainActivity.callJs("__onLocalError(-1)");
        }
        foreground();
    }

    public void resume() {
        if (mp == null) {                      // 播放器被系统回收过：拿同一首重新准备
            if (!curUrl.isEmpty()) play(curUrl, curTitle, curArtist, curCover);
            return;
        }
        requestFocus();
        try {
            mp.start();
            state = ST_PLAY;
        } catch (Exception e) {
            // MediaPlayer 状态坏了（音频轨道丢失 / 已 error）：重开同一首，别让按钮变成死的
            String u = curUrl, t = curTitle, a = curArtist, c = curCover;
            releasePlayer();
            state = ST_IDLE;
            if (!u.isEmpty()) play(u, t, a, c);
            return;
        }
        pushState();
        foreground();
    }

    public void pause() {
        if (mp == null) return;
        try { if (mp.isPlaying()) mp.pause(); } catch (Exception ignored) { }
        state = ST_PAUSE;
        pushState();
    }

    public void stopSelfPlay() {
        releasePlayer();
        state = ST_IDLE;
        pushState();
        stopForeground(true);
        stopSelf();
    }

    public void seekToMs(long ms) {
        if (mp == null) return;
        try { mp.seekTo((int) Math.max(0, ms)); } catch (Exception ignored) { }
        pushState();
    }

    public void setVolumePct(int pct) {
        vol = Math.max(0, Math.min(100, pct)) / 100f;
        try { if (mp != null) mp.setVolume(vol, vol); } catch (Exception ignored) { }
        pushState();
    }

    private void releasePlayer() {
        try { if (mp != null) { mp.reset(); mp.release(); } } catch (Exception ignored) { }
        mp = null;
        if (wake != null && wake.isHeld()) { try { wake.release(); } catch (Exception ignored) { } }
    }

    private void requestFocus() {
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                if (focusListener == null) {
                    focusListener = new AudioManager.OnAudioFocusChangeListener() {
                        @Override public void onAudioFocusChange(int f) {
                            // 只在真的被别的应用抢走时才暂停；
                            // 自己 requestFocus 引起的 LOSS 不会再触发（request 只建一次）
                            if (f == AudioManager.AUDIOFOCUS_LOSS
                                    || f == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
                                if (state == ST_PLAY) pause();
                            }
                        }
                    };
                }
                if (focusReq == null) {
                    AudioAttributes attrs = new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build();
                    focusReq = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                            .setAudioAttributes(attrs)
                            .setOnAudioFocusChangeListener(focusListener)
                            .build();
                }
                am.requestAudioFocus(focusReq);   // 复用同一个 request，不产生自伤 LOSS
            } else {
                am.requestAudioFocus(focusListener, AudioManager.STREAM_MUSIC,
                        AudioManager.AUDIOFOCUS_GAIN);
            }
        } catch (Exception ignored) { }
    }

    // ------------------------------------------------------------ 状态
    public static int stateCode() {
        PlayerService s = inst;
        return s == null ? ST_IDLE : s.state;
    }

    public static String stateJson() {        PlayerService s = inst;
        if (s == null) return "{\"state\":0,\"pos\":0,\"dur\":0}";
        int pos = 0, dur = 0;
        try {
            if (s.mp != null) { pos = s.mp.getCurrentPosition(); dur = s.mp.getDuration(); }
        } catch (Exception ignored) { }
        return "{\"state\":" + s.state
                + ",\"pos\":" + (pos / 1000.0)
                + ",\"dur\":" + (dur > 0 ? dur / 1000.0 : 0)
                + ",\"vol\":" + Math.round(s.vol * 100)
                + ",\"title\":\"" + esc(s.curTitle) + "\""
                + ",\"artist\":\"" + esc(s.curArtist) + "\"}";
    }

    private static String esc(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ");
    }

    // ------------------------------------------------------- 通知 / 会话
    private void pushState() {
        try {
            PlaybackState.Builder b = new PlaybackState.Builder()
                    .setActions(PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE
                            | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_STOP
                            | PlaybackState.ACTION_SKIP_TO_NEXT | PlaybackState.ACTION_SKIP_TO_PREVIOUS
                            | PlaybackState.ACTION_SEEK_TO);
            b.setState(state == ST_PLAY ? PlaybackState.STATE_PLAYING
                            : state == ST_PREP ? PlaybackState.STATE_BUFFERING
                            : state == ST_PAUSE ? PlaybackState.STATE_PAUSED
                            : PlaybackState.STATE_NONE,
                    0, state == ST_PLAY ? 1f : 0f);
            session.setPlaybackState(b.build());
            MediaMetadata.Builder m = new MediaMetadata.Builder()
                    .putString(MediaMetadata.METADATA_KEY_TITLE, curTitle)
                    .putString(MediaMetadata.METADATA_KEY_ARTIST, curArtist);
            if (art != null) m.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, art);
            session.setMetadata(m.build());
        } catch (Exception ignored) { }
        if (state != ST_IDLE) notifyBar();
    }

    private PendingIntent pi(String action) {
        Intent i = new Intent(this, PlayerService.class).setAction(action);
        int flag = Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0;
        return PendingIntent.getService(this, action.hashCode(), i, flag);
    }

    private void notifyBar() {
        try {
            Notification.Action prev = new Notification.Action.Builder(
                    Icon.createWithResource(this, android.R.drawable.ic_media_previous), "上一首", pi("prev")).build();
            Notification.Action pp = new Notification.Action.Builder(
                    Icon.createWithResource(this, state == ST_PLAY
                            ? android.R.drawable.ic_media_pause : android.R.drawable.ic_media_play),
                    state == ST_PLAY ? "暂停" : "播放", pi("toggle")).build();
            Notification.Action next = new Notification.Action.Builder(
                    Icon.createWithResource(this, android.R.drawable.ic_media_next), "下一首", pi("next")).build();

            Intent open = new Intent(this, MainActivity.class);
            int flag = Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0;
            PendingIntent openPi = PendingIntent.getActivity(this, 7, open, flag);

            Notification.Builder b = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, CH_ID)
                    : new Notification.Builder(this);
            b.setSmallIcon(getApplicationInfo().icon)
                    .setContentTitle(curTitle.isEmpty() ? "鲸屿Mic" : curTitle)
                    .setContentText(curArtist.isEmpty() ? "手机播放" : curArtist)
                    .setContentIntent(openPi)
                    .setOngoing(state == ST_PLAY)
                    .setOnlyAlertOnce(true)
                    .setShowWhen(false)
                    .setVisibility(Notification.VISIBILITY_PUBLIC);
            if (art != null) b.setLargeIcon(art);
            b.addAction(prev).addAction(pp).addAction(next);
            Notification.MediaStyle ms = new Notification.MediaStyle()
                    .setMediaSession(session.getSessionToken())
                    .setShowActionsInCompactView(0, 1, 2);
            b.setStyle(ms);
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            nm.notify(1, b.build());
        } catch (Exception ignored) { }
    }

    private void foreground() {
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                Notification.Builder b = new Notification.Builder(this, CH_ID)
                        .setSmallIcon(getApplicationInfo().icon)
                        .setContentTitle(curTitle.isEmpty() ? "鲸屿Mic" : curTitle)
                        .setContentText(curArtist.isEmpty() ? "手机播放" : curArtist)
                        .setOngoing(state == ST_PLAY)
                        .setOnlyAlertOnce(true)
                        .setShowWhen(false);
                Notification.MediaStyle ms = new Notification.MediaStyle()
                        .setMediaSession(session.getSessionToken());
                b.setStyle(ms);
                startForeground(1, b.build());
            }
        } catch (Exception ignored) { }
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm.getNotificationChannel(CH_ID) != null) return;
        NotificationChannel c = new NotificationChannel(CH_ID, "本地播放", NotificationManager.IMPORTANCE_LOW);
        c.setShowBadge(false);
        c.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(c);
    }

    // --------------------------------------------------------- 封面异步
    private void loadArt(final String url) {
        if (url == null || url.isEmpty()) return;
        // 同一张封面已经在用就不要再清空重下（否则连播同一专辑时会没有图）
        if (url.equals(artFor) && art != null) return;
        art = null;
        artFor = url;
        new Thread(new Runnable() {
            @Override public void run() {
                InputStream in = null;
                try {
                    HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                    c.setConnectTimeout(4000);
                    c.setReadTimeout(6000);
                    c.setDoInput(true);
                    c.connect();
                    in = c.getInputStream();
                    final Bitmap bmp = BitmapFactory.decodeStream(in);
                    if (bmp != null) {
                        ui.post(new Runnable() {
                            @Override public void run() {
                                art = bmp;
                                pushState();
                            }
                        });
                    }
                } catch (Exception ignored) {
                } finally {
                    try { if (in != null) in.close(); } catch (Exception ignored) { }
                }
            }
        }).start();
    }
}
