#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鲸屿Mic · 服务端

给网页端 / 桌面端 / 手机端提供封面、歌词、推荐与播放状态聚合。
纯标准库实现，默认监听 0.0.0.0:58091。

⚠ 关于在线内容源
本仓库是「as-is 分享版」，只处理本地曲库：扫描、播放控制、歌词解析、推荐排序。
从第三方站点检索 / 试听 / 下载 / 抓取歌词这类能力，本仓库不包含实现，作者也不提供；
相关函数仅作接口占位（签名在、恒返回空值），所以搜索、在线下载、联网歌词匹配默认关闭。
若要接入，请自备已获授权的数据源 —— 见 docs/OPTIONAL-ONLINE-SOURCES.md。
"""
import base64
import difflib
import html
import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import lyrvid        # 电视歌词画面（Pillow + ffmpeg）
except Exception as _e:  # 缺依赖时不影响主服务
    lyrvid = None

try:
    import swap as _swapmod      # JY-SWAP 换源流水线
except Exception as _e:
    _swapmod = None
    sys.stderr.write("[swap] import failed: %r\n" % (_e,))

# MusicMate（musictool）自身版本号。两端「关于」页从这里读 ——
# 页面上不许再出现第二处版本字面量（手写的一定会被忘，历史上出过两次）。
VERSION = "4.01"
BOOT_TS = time.time()

# 公开版本：被移除功能的统一返回提示。凡是返回 {"ok": False, "msg": ...} 的接口都用它。
_JY_NO_SOURCE_MSG = ("此功能需要在线内容源，本仓库不提供。可自行接入后使用，见 docs/OPTIONAL-ONLINE-SOURCES.md")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MUSIC_DIR = "/sdcard/Music"
CONF_DIR = "/root/xiaomusic_conf"
CACHE_DIR = os.path.join(CONF_DIR, "cache")
COVER_DIR = os.path.join(CACHE_DIR, "cover")
LRC_DIR = os.path.join(CACHE_DIR, "lrc")
INDEX_FILE = os.path.join(CACHE_DIR, "index.json")
LVID_DIR = os.path.join(CONF_DIR, "lvideo")
PORT = 58091
BACKEND_API = "http://127.0.0.1:58090"

# ------------------------------------------------- JY-PLCACHE 歌单/推荐「先下后播」缓存
# 用户原话（v4.33）：「出歌单的同时，把歌曲文件、封面、歌词同时储存起来，就不用等那么久的缓冲了。」
# 背景：在线歌原本走 /api/jy_stream 代理上游，起播要等 1~3s 上游缓冲，期间设备还报上一首，
#       连带把播放条/歌词页/播放列表一起「跳回本地歌」（已用 pendLive() 兜住显示，但缓冲本身还在）。
# 思路：推荐/歌单一出就后台把每首在线歌的 音频+封面+歌词 落进一个**独立目录**
#       （PLAYLIST_CACHE，不进 /sdcard/Music，所以不污染「本地曲库」视图），
#       播放时改喂本地文件（/api/plfile），音箱拿到的是已下好的 mp3 → 零缓冲立刻出声。
# 范围：预取「所有推荐的歌」（out["online"] + out["playlists"].songs），第二天（≈1 天）删除，
#       容量+时间双闸兜底（见 PL_MAX_MB / PL_MAX_AGE_D）。单首失败静默，绝不拖慢前台。
PLAYLIST_CACHE = os.path.join(CACHE_DIR, "playlist")
PL_MAX_MB = 1536          # 容量闸：≈1.5GB，够一整天推荐（几十首）还留余量，吃不满 SD 卡
PL_MAX_AGE_D = 1          # 天数闸：用户原话「第二天删除」—— 比今天旧的整批清掉
PL_MAX_FILES = 30 * 1024 * 1024   # 单文件上限 30MB：正常一首 ≤~15MB，远超就是坏流，别落盘
PL_PREFETCH_WORKERS = 2   # 后台预取下歌并发数（上游锁只有 2 名额，别全开抢前台搜索）
PL_PROTECT_S = 600        # 保护窗：10 分钟内被写过/读过的别动（很可能正在播）
PL_MIN_GAP_S = 90         # 两次清理最小间隔：连点几首歌也别连着扫目录
PL_BOOT_DELAY_S = 25      # 开机后先等 xiaomusic 把自己准备好，再动缓存目录

for d in (CACHE_DIR, COVER_DIR, LRC_DIR, LVID_DIR, PLAYLIST_CACHE):
    os.makedirs(d, exist_ok=True)

AVATAR_FILE = os.path.join(CONF_DIR, "avatar.png")

# ------------------------------------------------- JY-WAVCACHE 转码 wav 缓存双闸清理
# 背景：语音助手音箱不认 FLAC/APE/AIFF（实测 status=2、position 恒 0，看着在播其实没声），
# 于是 xiaomusic 的 jy_mstream()（**另一个进程**，源码在
#   /root/venv/lib/python3.11/site-packages/xiaomusic/api/routers/file.py）
# 会把无损源文件实时转成 CD 品质 wav（pcm_s16le / 44.1kHz / 2ch）缓存下来给音箱听。
# 缓存名 = sha1("源绝对路径|源文件mtime|源文件大小")，源文件只要被重新拷过一次 mtime 就变，
# 旧缓存**永远不可能再被命中**，而原项目里没有任何地方负责删 —— 这就是它一路涨的根因。
# 2026-10-30 实测：25 个文件、1,066,599,938 字节（1017.2 MB），两天涨了 231 MB。
#
# 为什么必须「双闸」：
#   只按天数 —— 挡不住爆发式增长（一晚上听 20 首无损就是 800 MB）；
#   只按容量 —— 又会白白留着几个月前、再也命中不到的孤儿文件。
WAVCACHE_DIR = os.path.join(CONF_DIR, "compat")
WAVCACHE_MAX_MB = 512       # 容量闸：总大小超了就按 mtime 从旧到新删，直到降到阈值以下。
                            #   ≈ 12 首 CD 品质无损（单首 4~5 分钟约 40~50 MB），
                            #   够覆盖「这两周常听的那几首」，又不会把 SD 卡吃穿。
WAVCACHE_MAX_AGE_D = 7      # 天数闸：7 天没被重新生成的直接删。听歌习惯基本按周重复，
                            #   一周都没再碰过的那首，下次听重转一次（十几秒）代价可接受。
WAVCACHE_PROTECT_S = 1800   # 保护窗：30 分钟内被写过的文件一律不碰。一首无损要播 4~5 分钟、
                            #   重转一次要十几秒，30 分钟是「很可能还在用」的宽余值。
WAVCACHE_MIN_GAP_S = 60     # 两次清理的最小间隔：连点几首歌也别连着扫目录
WAVCACHE_BOOT_DELAY_S = 25  # 开机后先等 xiaomusic 把自己准备好，再动它的缓存目录

# 歌词报错：两端（App / PC）标记的错误行都记在这里，下次维护时由 lrc_report_fix.py 统一处理
LRCREPORT_FILE = os.path.join(CONF_DIR, "lrc_report.json")
LRC_KINDS = ("time", "text", "pair", "missing", "other")

_lock = threading.BoundedSemaphore(2)  # 上游站点 请求：允许 2 个并发（日均推荐 6 关键词原被完全串行≈9s，放宽到 2 并发约减半；个人低频使用对频控无风险）
_index = {}
_index_dirty = False


# ---------------------------------------------------------------- 工具函数
def load_index():
    global _index
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            _index = json.load(f)
    except Exception:
        _index = {}


def save_index():
    global _index_dirty
    if not _index_dirty:
        return
    try:
        tmp = INDEX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_index, f, ensure_ascii=False)
        os.replace(tmp, INDEX_FILE)
        _index_dirty = False
    except Exception as e:
        log("save_index failed: %r" % (e,))


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def _ex_cancel(ex):
    """收掉线程池：能取消排队任务就取消（Python 3.9+ 的 cancel_futures），
    不能就退回 wait=False。

    为什么要取消：`shutdown(wait=False)` 只是「不等人」，排队的任务**照样会跑**，
    而每个任务都要抢那把只有 2 个名额的全局 上游站点 锁。冷算 12 秒的场景里，
    前一轮超时放弃的任务和新请求挤在同一个门口 —— 这就是「明明只搜 6 个词，
    却要等好几轮」的来源。取消掉没人要的任务，锁就能干净地让给前台请求。
    """
    try:
        ex.shutdown(wait=False, cancel_futures=True)
    except TypeError:                     # 老解释器不认 cancel_futures
        try:
            ex.shutdown(wait=False)
        except Exception:
            pass
    except Exception:
        pass


# ------------------------------------------------- JY-WAVCACHE 清理实现
_wavcache_lock = threading.Lock()
_wavcache_next_ts = 0.0


def wavcache_stat():
    """只读盘点 compat/。返回 (rows, total_bytes)，rows = [(绝对路径, 字节数, mtime)]。"""
    rows = []
    total = 0
    try:
        for fn in os.listdir(WAVCACHE_DIR):
            if not fn.endswith(".wav"):
                continue
            p = os.path.join(WAVCACHE_DIR, fn)
            try:
                st = os.stat(p)
            except Exception:
                continue
            rows.append((p, int(st.st_size), float(st.st_mtime)))
            total += int(st.st_size)
    except Exception:
        pass     # 目录不存在 / 还没生成过：当作空
    return rows, total


def wavcache_open_paths():
    """此刻正被**任意**进程打开着的 .wav 绝对路径集合。

    写这些文件的是 xiaomusic 那个进程，读它的也是它自己（FileResponse）或 ffmpeg ——
    musictool 这边的内存计数器看不见别人手里拿的句柄，所以直接扫 /proc/<pid>/fd：
    这是唯一跨进程可靠的「还在用」信号。Android 只让我们看自己这一组 pid（十几个），
    扫一遍实测约 40 ms，挂在低频事件上完全不心疼。

    注意：这块盘是 f2fs **noatime** 挂载的，**atime 不会更新**，
    所以「最近被读过」这件事不能用 atime 判断（实测读 1MB 后 atime 纹丝不动）。
    """
    out = set()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                fds = os.listdir("/proc/%s/fd" % pid)
            except Exception:
                continue
            for fd in fds:
                try:
                    tgt = os.readlink("/proc/%s/fd/%s" % (pid, fd))
                except Exception:
                    continue
                if tgt.endswith(".wav"):
                    out.add(tgt)
    except Exception:
        pass
    return out


def wavcache_sweep(reason=""):
    """双闸清理。返回 (删掉几个, 释放多少字节)。异常由调用方吞，绝不影响主流程。

    三道保护，按优先级：
      ① 正被别的进程开着（/proc fd 命中）→ 不碰；
      ② mtime 在 WAVCACHE_PROTECT_S 保护窗内 → 不碰；
      ③ 天数闸（> MAX_AGE_D）或容量闸（还在 MAX_MB 之上）响了才会删，
         且一律按 mtime 从旧到新删，先删最不可能再命中的那个。
    """
    rows, total = wavcache_stat()
    if not rows:
        return 0, 0
    now = time.time()
    cap = int(WAVCACHE_MAX_MB) * 1024 * 1024
    age_cut = now - float(WAVCACHE_MAX_AGE_D) * 86400.0
    protect_cut = now - float(WAVCACHE_PROTECT_S)
    openp = wavcache_open_paths()

    rows.sort(key=lambda r: r[2])          # mtime 从旧到新
    live = total
    freed = 0
    removed = 0
    for path, size, mtime in rows:
        if path in openp:                  # ① 别人正开着：在读或在写
            continue
        if mtime >= protect_cut:           # ② 保护窗内：很可能是刚落地的那首
            continue
        if not (mtime <= age_cut or live > cap):
            continue                       # ③ 双闸都没响：留着
        try:
            os.remove(path)
        except Exception as e:
            log("wavcache rm fail %s %r" % (os.path.basename(path), e))
            continue
        removed += 1
        freed += size
        live -= size
    return removed, freed


def wavcache_snapshot():
    """`/api/wavcache` 只读盘点：现在多大、几首、超没超闸、有几个正被打开。"""
    rows, total = wavcache_stat()
    openp = wavcache_open_paths()
    now = time.time()
    cap = int(WAVCACHE_MAX_MB) * 1024 * 1024
    return {
        "ok": True,
        "dir": WAVCACHE_DIR,
        "files": len(rows),
        "bytes": int(total),
        "mb": round(total / 1048576.0, 1),
        "max_mb": int(WAVCACHE_MAX_MB),
        "max_age_d": WAVCACHE_MAX_AGE_D,
        "protect_s": int(WAVCACHE_PROTECT_S),
        "over": bool(total > cap),
        "expired": int(sum(1 for _p, _s, m in rows
                           if m <= now - float(WAVCACHE_MAX_AGE_D) * 86400.0)),
        "protected": int(sum(1 for _p, _s, m in rows
                             if m >= now - float(WAVCACHE_PROTECT_S))),
        "open_now": int(sum(1 for p, _s, _m in rows if p in openp)),
        "oldest": int(min((m for _p, _s, m in rows), default=0)),
        "newest": int(max((m for _p, _s, m in rows), default=0)),
    }


def wavcache_after_write(reason=""):
    """「刚（可能）生成完一个 wav」时该调的唯一入口。异步 + 节流，调用方不会被拖住。

    真正的写盘动作在 xiaomusic 进程里，musictool 看不见，所以退一步挂在
    「一首歌刚被推给音箱」这个必然紧邻写盘的事件上：**一首选歌一次**，不是定时器轮询
    （没人听歌的时候完全零开销）。如果哪天要在 xiaomusic 侧埋点，直接调这个函数即可。
    """
    global _wavcache_next_ts
    with _wavcache_lock:                   # 只占一瞬：改个时间戳就走
        now = time.time()
        if now < _wavcache_next_ts:
            return False
        _wavcache_next_ts = now + float(WAVCACHE_MIN_GAP_S)
    threading.Thread(target=_wavcache_run, args=(reason,), daemon=True).start()
    return True


def _wavcache_run(reason):
    """真正的清理，跑在后台线程里；任何异常都在这里被吞掉并记一行日志。"""
    try:
        with _wavcache_lock:
            removed, freed = wavcache_sweep(reason)
        if removed:
            log("wavcache sweep(%s): removed=%d freed=%.1fMB"
                % (reason, removed, freed / 1048576.0))
    except Exception as e:
        try:
            log("wavcache sweep fail(%s) %r" % (reason, e))
        except Exception:
            pass


def _wavcache_boot():
    """开机补扫一次（一次性线程，不是常驻轮询）：把上次运行攒下的立刻收敛回去。"""
    try:
        time.sleep(WAVCACHE_BOOT_DELAY_S)
    except Exception:
        return
    _wavcache_run("boot")


# ============================================================ JY-PLCACHE
# 歌单/推荐「先下后播」缓存：推荐一出来就后台把每首在线歌的 音频+封面+歌词 落进
# PLAYLIST_CACHE（独立目录，不进 /sdcard/Music，不污染「本地曲库」视图），播放时改喂
# 本地文件（/api/plfile），音箱拿到已下好的 mp3 → 零缓冲立刻出声。
# 设计要点（与 JY-WAVCACHE 同思路，但目的不同）：
#   · 失败静默：预取是「锦上添花」，任何一步挂了都退回前台既有 stream/下载路径，绝不拖慢前台；
#   · 去重：按 sid 记索引，已下好的跳过；同一首出现在多张卡也只下一次；
#   · 无损不缓存：音箱放不了 FLAC/APE，留给前台走「下载→转 wav」；
#   · 双闸清理：容量(PL_MAX_MB) + 天数(PL_MAX_AGE_D=1 ≈ 第二天删除)，正被播着的受保护。
PL_INDEX_FILE = os.path.join(PLAYLIST_CACHE, ".index.json")
_pl_index = None
# RLock：调用方常在持有本锁的情况下再调 _pl_index_get（嵌套获取），必须用可重入锁，
# 否则 threading.Lock 非重入 → 死锁（会直接把 /api/play_online 的处理线程挂死）。
_pl_index_lock = threading.RLock()


def _pl_key(sid):
    """把任意在线 id 变成文件名安全的短串（gqb:123 -> gqb_123；wy:273.. -> wy_273..）。"""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", str(sid or ""))
    return s[:160] or "x"


def _pl_index_load():
    global _pl_index
    try:
        with open(PL_INDEX_FILE, encoding="utf-8") as f:
            _pl_index = json.load(f)
    except Exception:
        _pl_index = {}


def _pl_index_get():
    global _pl_index
    if _pl_index is None:
        with _pl_index_lock:
            if _pl_index is None:
                _pl_index_load()
    return _pl_index


def _pl_index_save():
    try:
        tmp = PL_INDEX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_pl_index_get(), f, ensure_ascii=False)
        os.replace(tmp, PL_INDEX_FILE)
    except Exception as e:
        log("pl_index save fail %r" % (e,))


def _pl_audio_path(sid):
    return os.path.join(PLAYLIST_CACHE, _pl_key(sid) + ".mp3")


def _pl_audio_ready(sid):
    p = _pl_audio_path(sid)
    return os.path.isfile(p) and os.path.getsize(p) > 10000


def _pl_cache_lyric(sid, song, artist):
    """把歌词写进 LRC_DIR（serve_lyric 只读这里，复用现有链路，客户端零改动）。
    已有文件不覆盖（别冲掉本地歌已匹配好的词）；失败静默。
    只走 gqb 详情直取（一次请求，不触发全量搜索），其余源留给客户端按需取。"""
    if not song:
        return
    name = (("%s - %s" % (artist, song)) if artist else song)
    p = os.path.join(LRC_DIR, sanitize_filename(name) + ".lrc")
    if os.path.isfile(p) and os.path.getsize(p) > 0:
        return
    src, mid = jy_src_of(sid)
    if src != "gqb" or not mid:
        return
    try:
        info = fetch_song(mid)
        lrc = info.get("lrc") or ""
        if lrc and not is_junk_lrc(lrc):
            os.makedirs(LRC_DIR, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(lrc)
            log("pl lrc cached: %s" % name)
    except Exception as e:
        log("pl lrc prefetch fail %s: %r" % (sid, e))


def _cache_one_song(sid, song="", artist=""):
    """缓存一首在线歌的 音频+封面+歌词 到 PLAYLIST_CACHE。单首失败静默。"""
    if not sid:
        return
    # 已下好的：只补一下歌词（音频够了）
    if _pl_audio_ready(sid):
        _pl_cache_lyric(sid, song, artist)
        return
    try:
        url, src, note, _s, _a = jy_resolve(sid, song, artist)
    except Exception as e:
        log("pl prefetch resolve fail %s: %r" % (sid, e))
        return
    if not url:
        return
    ext = _url_ext(url)
    if ext in _LOSSLESS_EXT:
        return                                   # 无损交给前台「下载→转 wav」
    dest = _pl_audio_path(sid)
    tmp = dest + ".part"
    try:
        hdrs = {"User-Agent": UA}
        _ref = ""  # 本仓库不提供在线源，Referer 留空
        if _ref:
            hdrs["Referer"] = _ref
        req = urllib.request.Request(url, headers=hdrs)
        total = 0
        with opener().open(req, timeout=90) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if total > PL_MAX_FILES:
                    break
        if total < 10000:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return
        os.replace(tmp, dest)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        log("pl prefetch audio fail %s: %r" % (sid, e))
        return
    # 封面（复用在线封面落盘缓存，幂等）
    # v4.35：原来这里是 `_, mid = jy_src_of(sid); online_cover_bytes(mid)` —— 传错了一格。
    # online_cover_bytes 要的是**整条 sid**（纯数字＝源A id），给拆出来的 mid 对源A
    # 恰好一样所以一直没暴露；kw/wy 进来就必然拉空。现在只对纯数字走这条。
    try:
        if str(sid or "").isdigit():
            online_cover_bytes(sid)
    except Exception:
        pass
    # 歌词（只 gqb 直取，写 LRC_DIR 复用 serve_lyric 链路）
    _pl_cache_lyric(sid, song or _s, artist or _a)
    # 记索引（dur 顺手存，play_online 直接取，省一次反查）
    try:
        with _pl_index_lock:
            idx = _pl_index_get()
            idx[sid] = {
                "path": os.path.basename(dest),
                "name": (("%s - %s" % (artist or _a, song or _s))
                         if (artist or _a) else (song or _s)),
                "dur": jy_src_dur(sid) or 0,
                "mtime": int(os.path.getmtime(dest)),
            }
            _pl_index_save()
    except Exception:
        pass
    log("pl cached: %s (%dKB)" % (sid, os.path.getsize(dest) // 1024))


def prefetch_playlist_cache(out):
    """推荐/歌单返回后后台预取：所有 online 歌的 音频+封面+歌词 落 PLAYLIST_CACHE。
    非阻塞：起 daemon 线程跑，不拖慢 /api/recommend 的响应。"""
    songs = []
    # 1) 推荐在线列表：out["online"] 是歌字典列表，每项带 online=True + id
    for c in (out.get("online") or []):
        if isinstance(c, dict) and c.get("online") and c.get("id"):
            songs.append((c["id"], c.get("song") or c.get("title") or "",
                          c.get("artist") or ""))
    # 2) 主题歌单：out["playlists"] 是 [{songs:[...]}]
    for pl in (out.get("playlists") or []):
        if not isinstance(pl, dict):
            continue
        for c in (pl.get("songs") or []):
            if isinstance(c, dict) and c.get("online") and c.get("id"):
                songs.append((c["id"], c.get("song") or c.get("title") or "",
                              c.get("artist") or ""))
    if not songs:
        return
    seen, uniq = set(), []
    for sid, s, a in songs:
        if sid in seen:
            continue
        seen.add(sid)
        uniq.append((sid, s, a))
    log("pl prefetch queued: %d songs" % len(uniq))

    def _run():
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=PL_PREFETCH_WORKERS) as ex:
                for sid, s, a in uniq:
                    ex.submit(_cache_one_song, sid, s, a)
        except Exception as e:
            log("pl prefetch pool err %r" % (e,))
        finally:
            # 预取跑完扫一次：把没下成的、过期的清掉（双闸）
            try:
                plcache_after_write("prefetch")
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def plcache_stat():
    rows, total = [], 0
    try:
        for fn in os.listdir(PLAYLIST_CACHE):
            if fn.startswith(".") or not fn.lower().endswith((".mp3", ".m4a", ".aac")):
                continue
            p = os.path.join(PLAYLIST_CACHE, fn)
            try:
                st = os.stat(p)
            except Exception:
                continue
            rows.append((p, int(st.st_size), float(st.st_mtime)))
            total += int(st.st_size)
    except Exception:
        pass
    return rows, total


def plcache_open_paths():
    """此刻正被**任意**进程打开着的 PLAYLIST_CACHE 音频（很可能正在播）。"""
    out = set()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                fds = os.listdir("/proc/%s/fd" % pid)
            except Exception:
                continue
            for fd in fds:
                try:
                    tgt = os.readlink("/proc/%s/fd/%s" % (pid, fd))
                except Exception:
                    continue
                if tgt.startswith(PLAYLIST_CACHE + os.sep):
                    out.add(tgt)
    except Exception:
        pass
    return out


def plcache_sweep(reason=""):
    """容量+天数双闸清理（见 WAVCACHE）。返回 (删掉几个, 释放多少字节)。"""
    rows, total = plcache_stat()
    if not rows:
        return 0, 0
    now = time.time()
    cap = int(PL_MAX_MB) * 1024 * 1024
    age_cut = now - float(PL_MAX_AGE_D) * 86400.0
    protect_cut = now - float(PL_PROTECT_S)
    openp = plcache_open_paths()
    rows.sort(key=lambda r: r[2])
    live = total
    freed = 0
    removed = 0
    for path, size, mtime in rows:
        if path in openp:                  # 正被播着：不碰
            continue
        if mtime >= protect_cut:           # 保护窗内：刚落地的那首
            continue
        if not (mtime <= age_cut or live > cap):
            continue
        try:
            os.remove(path)
        except Exception:
            continue
        removed += 1
        freed += size
        live -= size
    # 索引同步清掉已删的条目
    try:
        with _pl_index_lock:
            idx = _pl_index_get()
            dead = [k for k, v in idx.items()
                    if not os.path.isfile(os.path.join(PLAYLIST_CACHE, v.get("path", "")))]
            for k in dead:
                idx.pop(k, None)
            if dead:
                _pl_index_save()
    except Exception:
        pass
    return removed, freed


_pl_lock = threading.Lock()
_pl_next_ts = 0.0


def plcache_after_write(reason=""):
    """「刚（可能）落了一个文件」时该调的入口：异步 + 节流，调用方不被拖住。"""
    global _pl_next_ts
    with _pl_lock:
        now = time.time()
        if now < _pl_next_ts:
            return False
        _pl_next_ts = now + float(PL_MIN_GAP_S)
    threading.Thread(target=_plcache_run, args=(reason,), daemon=True).start()
    return True


def _plcache_run(reason):
    try:
        with _pl_lock:
            removed, freed = plcache_sweep(reason)
        if removed:
            log("plcache sweep(%s): removed=%d freed=%.1fMB"
                % (reason, removed, freed / 1048576.0))
    except Exception as e:
        try:
            log("plcache sweep fail(%s) %r" % (reason, e))
        except Exception:
            pass


def _plcache_boot():
    """开机补扫一次：把上次运行攒下的收敛回去。"""
    try:
        time.sleep(PL_BOOT_DELAY_S)
    except Exception:
        return
    _plcache_run("boot")


_opener = None


def opener():
    """带 cookie 的全局 opener"""
    global _opener
    if _opener is None:
        cj = http.cookiejar.CookieJar()
        _opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        _opener.addheaders = [
            ("User-Agent", UA),
            ("Accept-Language", "zh-CN,zh;q=0.9"),
        ]
    return _opener


def http_get(url, referer="", timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with opener().open(req, timeout=timeout) as r:
        return r.read()


def http_post(url, data, referer="", timeout=25):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    with opener().open(req, timeout=timeout) as r:
        return r.read()


def clean_query(name):
    """把本地文件名整理成搜索关键词：去扩展名、去 (Cover: xxx)、去多余标记"""
    s = name
    for ext in (".mp3", ".flac", ".m4a", ".wav", ".ape", ".ogg", ".aac", ".wma"):
        if s.lower().endswith(ext):
            s = s[: -len(ext)]
            break
    s = re.sub(r"[\(\[（【][^\)\]）】]*cover[^\)\]）】]*[\)\]）】]", "", s, flags=re.I)
    s = re.sub(r"[\(\[（【](官方|高音质|无损|完整版|原唱|伴奏|DJ版)[^\)\]）】]*[\)\]）】]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return s


def _in_brackets(s, idx):
    """idx 处是否在成对括号/方括号内：避免把括号内分隔符切开（如 KN0W_NAME 的下划线）。"""
    depth = 0
    for ch in s[:idx]:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
    return depth > 0


def split_title(name):
    """'歌手 - 歌名' -> (歌手, 歌名)。优先识别两侧带空格的标准分隔符；
    退到裸 '-'/'_' 时只在括号外切，避免斩断单词/标签内部（如 KN0W_NAME、'Harvest (KN0W_NAME …)'）。"""
    s = clean_query(name)
    for sep in (" - ", " – ", " _ "):
        if sep in s:
            a, b = s.split(sep, 1)
            if a.strip() and b.strip():
                return a.strip(), b.strip()
    for sep in ("-", "_"):
        idx = s.find(sep)
        while idx != -1:
            if 0 < idx < len(s) - 1 and s[idx - 1] not in "([" and s[idx + 1] not in ")]" \
                    and not _in_brackets(s, idx):
                a, b = s[:idx], s[idx + 1:]
                if a.strip() and b.strip():
                    return a.strip(), b.strip()
            idx = s.find(sep, idx + 1)
    return "", s


def sanitize_filename(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120] or "untitled"


def parse_appdata(page):
    m = re.search(r"window\.appData\s*=\s*JSON\.parse\('(.*?)'\);\s*", page, re.S)
    if not m:
        return None
    raw = m.group(1)
    try:
        # JSON.parse('...') 里的内容是被转义的 JS 字符串
        txt = raw.encode().decode("unicode_escape")
        return json.loads(txt)
    except Exception:
        try:
            txt = raw.replace("\\'", "'").replace('\\"', '"')
            return json.loads(txt)
        except Exception:
            return None


def parse_search(page):
    """源A搜索结果解析 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return []


def parse_lrc(page):
    m = re.search(r'id="content-lrc"[^>]*>(.*?)</div>', page, re.S)
    if not m:
        return ""
    body = m.group(1)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    body = html.unescape(body)
    lines = [ln.strip() for ln in body.split("\n")]
    keep = [ln for ln in lines if re.match(r"^\[\d{1,2}:\d{2}", ln)]
    return "\n".join(keep) if keep else ""


# ---------- 上游抖动熔断（v4.01）----------
# 实战起因（2026-10-30）：上游站点 全站返 HTTP 520 时，search() 每打一次白烧 2~4.5s，
# 而一次「每日推荐」要打好几次 → 本来秒级的东西变成 22~33 秒，用户感受是「一点开就卡」。
# 规则：同一个源**连续失败 3 次** → 冷却 120s；冷却期间直接当它不存在（返回空、不排队、
#       不联网、不占 _lock）。冷却到期只放**一个**探测请求过去：成功就全清恢复；再失败
#       按 120 → 300 → 600 退避（上限 600s）。
# 边界：只做「别把一次上游抽风放大成几十秒」这一件事 —— 不改 _lock 名额、不改排序、
#       不改 /api/recommend 的返回结构。
_BRK_TRIP = 3
_BRK_STEPS = (120.0, 300.0, 600.0)
_BRK = {}                      # 源名 -> {fails, until, step, probing, err}
_BRK_LOCK = threading.Lock()


def _brk_state(src):
    """只读快照，给 /api/recsources 用。"""
    now = time.time()
    with _BRK_LOCK:
        st = _BRK.get(src) or {}
        until = float(st.get("until") or 0.0)
        cooling = until > now
        return {"cooling": bool(cooling),
                "retry_in": int(round(until - now)) if cooling else 0,
                "cooldown_until": int(until) if until else 0,
                "cool_sec": int(st.get("cool_sec") or 0),
                "fails": int(st.get("fails") or 0),
                "step": int(st.get("step") or 0),
                "probing": bool(st.get("probing")),
                "last_error": str(st.get("err") or "")[:120]}


def _brk_allow(src):
    """这个源现在能不能打。

    冷却中 → False（彻底跳过，一次网络都不发）；冷却到期 → True，但只放**一个**探测
    （设置 probing，别人同时来问都拿到 False，免得一群人一起冲上去再被拒一遍）。
    """
    now = time.time()
    with _BRK_LOCK:
        st = _BRK.get(src)
        if not st:
            return True
        if now < float(st.get("until") or 0.0):
            return False
        if st.get("probing"):
            return False
        st["probing"] = True
        return True


def _brk_ok(src):
    """源活着：失败计数、退避档位、冷却状态全清。"""
    with _BRK_LOCK:
        _BRK.pop(src, None)


def _brk_done(src):
    """这次调用没形成「成功/失败」的判断（例如抢锁失败直接跳过）—— 只把探测标记放开。"""
    with _BRK_LOCK:
        st = _BRK.get(src)
        if st:
            st["probing"] = False


def _brk_fail(src, err=""):
    """源失败一次。攒够 3 次进冷却；探测失败（probing）立刻再冷却并升一档退避。"""
    now = time.time()
    with _BRK_LOCK:
        st = _BRK.setdefault(src, {"fails": 0, "until": 0.0, "step": 0, "probing": False})
        was_probe = bool(st.get("probing"))
        st["probing"] = False
        st["fails"] = int(st.get("fails") or 0) + 1
        if err:
            st["err"] = str(err)
        # 已经在冷却里收到的失败，多半是「熔断前就已经发出去的在飞请求」回来了 ——
        # 只记账、不重复升级档位，否则一次抖动就会被几路并发一起顶到 300/600s。
        _already_cooling = now < float(st.get("until") or 0.0)
        if was_probe or (not _already_cooling and st["fails"] >= _BRK_TRIP):
            step = int(st.get("step") or 0)
            dur = _BRK_STEPS[step] if step < len(_BRK_STEPS) else _BRK_STEPS[-1]
            st["until"] = now + dur
            st["step"] = step + 1
            st["fails"] = 0
            st["cool_sec"] = dur
            log("BRK: %s 冷却 %.0fs（第 %d 档）%s"
                % (src, dur, st["step"], str(err)[:80]))

# ---------------------------------------------------------------- 核心业务
_search_cache = {}   # 关键词 -> (时间戳, 结果)


def search(keyword, limit=20, wait=0.8):
    """源A搜索入口 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return []


def fetch_song(mid):
    """第三方站点歌曲详情 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    函数保留签名、恒返回空值，调用方按原逻辑会得到「没有结果」。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return {}


_ocov_cache = {}          # mid -> (ts, 图片字节)：搜索结果列表的封面，落一次详情页就有了
_ocov_neg = {}            # mid -> ts：取不到的记 5 分钟，别每行渲染都去打一次详情页
_ocov_lock = threading.Lock()

def _wy_pic_url(sid):
    """第三方音乐平台封面 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ""


def online_cover_bytes(mid):
    """按在线歌曲 id 取封面图。搜出来的歌还没下载，本地自然没有封面，
    /api/cover 只认本地所以全是 ♪ 占位 —— 这里去源站详情页拿真封面并缓存一周。
    注意：图片多在源C CDN，带 上游站点 的 Referer 会被 403，只发 UA。"""
    with _ocov_lock:
        hit = _ocov_cache.get(mid)
        nt = _ocov_neg.get(mid)
    if hit and time.time() - hit[0] < 86400 * 7:
        return hit[1]
    if nt and time.time() - nt < 300:
        raise RuntimeError("cover unavailable")
    pic = ""
    if str(mid).lower().startswith("wy:"):
        pic = _wy_pic_url(mid)
    if not pic:
        pic = (fetch_song(mid) or {}).get("cover") or ""
    data = b""
    if pic:
        try:
            req = urllib.request.Request(pic, headers={"User-Agent": UA})
            with opener().open(req, timeout=20) as r:
                data = r.read(4 * 1024 * 1024)
        except Exception:
            data = b""
    if not data:
        with _ocov_lock:
            _ocov_neg[mid] = time.time()
            if len(_ocov_neg) > 500:
                _ocov_neg.clear()
        raise RuntimeError("no cover")
    with _ocov_lock:
        if len(_ocov_cache) > 300:      # 内存缓存别无限长
            _ocov_cache.clear()
        _ocov_cache[mid] = (time.time(), data)
        _ocov_neg.pop(mid, None)
    return data


# ---------------- 歌单配图：按关键词找一张图（v3.92） ----------------
# 用户原话：「歌单没有图片的，想办法按关键词找一张换上去，不一定是歌曲封面。」
# 于是：关键词 → 必应图片（国内可达、无需 key）拿候选直链 → 逐个试，第一个能解成图的就用；
#      全失败再退回音乐源搜同名歌的专辑图。抓到的一律裁成方形 JPEG 落盘，
#      一张只抓一次（卡片是懒加载，同一个歌单会被反复请求）。
PLCOV_DIR = os.path.join(COVER_DIR, "pl")
_plcov_mem = {}
_plcov_neg = {}
_plcov_lock = threading.Lock()
PLCOV_WAIT = 4.0          # 候选图并发窗口：宁可 404 留音符底图，别把页面挂住
PLCOV_SIDE = 900          # 落盘尺寸：v3.92 起提到 900，卡片在宽屏/高分屏才不糊（原始图够大时）
PLCOV_MAX = 6.5           # JY-PERF：一次冷抓的**总**时限（列表页 + 候选图 + 音乐源兜底）
PLCOV_LIST_TO = 3.0       # JY-PERF：必应列表页超时。以前给 7s —— 光等列表页就 7 秒，
                          # 而我们要的只是里面的缩略图直链（几十 KB，抓起来很快）。
PLCOV_SF_WAIT = 7.0       # JY-PERF：同一个歌单已经在抓了，后来者最多等这么久
PLCOV_WARM_MAX = 8        # JY-PERF：启动预热最多真抓几张（磁盘上已有的不算）


def _plcov_img(data, px=PLCOV_SIDE):
    """把任意图裁成方形 JPEG。原图动辄几 MB、还可能是 webp/动图/长图。
    没装 PIL 就直接返回原图（前端照样能显示，只是重一点）。"""
    try:
        import io as _io
        from PIL import Image, ImageOps
    except Exception:
        return data if len(data) > 4096 else b""
    try:
        im = Image.open(_io.BytesIO(data))
        try:
            im.draft("RGB", (px * 2, px * 2))     # JPEG 专用：解码阶段就降采样，手机上省好几秒
        except Exception:
            pass
        im = ImageOps.exif_transpose(im)
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = im.convert("RGB")
        w, h = im.size
        if w < 72 or h < 72:            # 太小的（图标/占位图）不要
            return b""
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
        im = im.resize((px, px), Image.LANCZOS)
        out = _io.BytesIO()
        im.save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue()
    except Exception:
        return b""


def _plcov_get(url, cap=4 * 1024 * 1024, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*"})
    with opener().open(req, timeout=timeout) as r:
        return r.read(cap)


def _plcov_bing(kw, count=18, timeout=PLCOV_LIST_TO):
    """联网封面检索 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return []


def _plcov_music(kw, count=3):
    """第三方音乐平台封面候选 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return []


def _plcov_fetch_all(urls, budget, per_timeout=4.0):
    """并发抓候选图，按**候选顺序**返回第一个能解成方图的（见 _plcov_img）。

    ⚠ 上一版的坑（冷启动 12~22 秒就是从这儿来的）：用 `with ThreadPoolExecutor(...)`
    包住、as_completed 到点 break 之后，退出 with 会 `shutdown(wait=True)` ——
    等于又同步等剩下那几张各跑满 4 秒。预算形同虚设，请求还是被拖住。

    现在改成「daemon 线程 + 主线程轮询」：拿到第一张能用的**立刻**返回，
    剩下的线程不 join，它们自己超时后自然结束，既不拖慢这次请求，
    也不会拦住服务退出（daemon 线程不阻塞解释器退出）。
    """
    if not urls:
        return b""
    got = {}
    glock = threading.Lock()

    def one(u):
        try:
            d = _plcov_get(u, cap=1200 * 1024, timeout=per_timeout)
        except Exception:
            d = b""
        if d:
            with glock:
                got[u] = d

    ths = []
    for u in urls:
        t = threading.Thread(target=one, args=(u,), name="jy-plcov")
        t.daemon = True
        ths.append(t)
        t.start()

    end = time.time() + max(0.3, float(budget or 0.3))
    dec = {}          # url -> 解出来的图（b"" 表示这张解不出，别再试第二次）
    out = b""
    while time.time() < end:
        with glock:
            snap = dict(got)
        for u in urls:
            if u not in snap:
                continue
            img = dec.get(u)
            if img is None:
                img = _plcov_img(snap[u]) or b""
                dec[u] = img
            if img:
                out = img
                break
        if out:
            break
        if len(snap) >= len(urls):
            break                     # 全回来了（也全解不出图），再等也没用
        time.sleep(0.04)
    return out


# ---------------- JY-PERF：歌单配图单飞
# 卡片是懒加载的，PC 和 App 还会各要一次，冷启动时同一个歌单常常被打 3~5 次。
# 以前每次都各抓一遍必应（5 倍网络开销 + 互抢线程池），现在同一个 key 只抓一次。
_plcov_sf_lock = threading.Lock()
_plcov_runs = {}          # key -> {"ev": Event, "data": bytes, "err": str}


def _plcov_cached(key):
    """只读的三级命中判断：磁盘 → 内存 → 失败缓存。
    命中返回 bytes；确定最近抓不到就抛错（让前端直接用音符底图）。"""
    hid = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(PLCOV_DIR, hid + ".jpg")     # 名字稳定 → 图稳定，重启也不变
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                d = f.read()
            if len(d) > 2048:
                return d
        except Exception:
            pass
    with _plcov_lock:
        hit = _plcov_mem.get(hid)
        neg = _plcov_neg.get(hid)
    if hit:
        return hit
    if neg and time.time() - neg < 600:              # 失败只记 10 分钟，别把网络抖动当永久
        raise RuntimeError("cover unavailable")
    return b""


def _plcov_build(key):
    """取封面图：两个图源都是接入点（`_plcov_bing` / `_plcov_music`），
    在公开版本里恒无图，于是抛 no cover，由前端回落到本地封面 / 首字图。
    整个函数仍受 PLCOV_MAX 总时限约束 —— 配图是锦上添花，
    绝不能为了等一张图把整个请求（和整个页面）挂住。"""
    hid = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(PLCOV_DIR, hid + ".jpg")
    t0 = time.time()
    end = t0 + PLCOV_MAX
    data = _plcov_fetch_all(_plcov_bing(key), min(PLCOV_WAIT, max(0.3, end - time.time())))
    if not data and time.time() < end - 0.5:          # 图片源不行就换音乐源，别空手回去
        data = _plcov_fetch_all(_plcov_music(key), min(4.0, max(0.3, end - time.time())))
    if not data:
        with _plcov_lock:
            _plcov_neg[hid] = time.time()
            if len(_plcov_neg) > 300:
                _plcov_neg.clear()
        raise RuntimeError("no cover")
    try:
        os.makedirs(PLCOV_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except Exception:
        pass
    with _plcov_lock:
        if len(_plcov_mem) > 200:
            _plcov_mem.clear()
        _plcov_mem[hid] = data
        _plcov_neg.pop(hid, None)
    log("plcover: %s → %d KB in %.2fs" % (key, len(data) // 1024, time.time() - t0))
    return data


def _plcov_singleflight(key):
    """同一个歌单的冷抓只做一次，其余请求等这一份结果。
    等不到（超时 / 抓失败）就回失败让前端显示音符底图。"""
    with _plcov_sf_lock:
        run = _plcov_runs.get(key)
        if run is None:
            if len(_plcov_runs) > 40:
                _plcov_runs.clear()
            run = {"ev": threading.Event(), "data": b"", "err": ""}
            _plcov_runs[key] = run
            mine = True
        else:
            mine = False
        ev = run["ev"]
    if not mine:
        ev.wait(PLCOV_SF_WAIT)
        with _plcov_sf_lock:
            d = run["data"]
            e = run["err"]
        if d:
            return d
        raise RuntimeError(e or "cover unavailable")
    # 同样用 try/finally：抓失败也要唤醒等待者（否则他们会在 Event 上挂满 PLCOV_SF_WAIT）
    data, err = b"", "no cover"
    try:
        data = _plcov_build(key)
    except Exception as ex:
        err = str(ex) or repr(ex)
    finally:
        with _plcov_sf_lock:
            run["data"] = data
            run["err"] = err
            _plcov_runs.pop(key, None)
        ev.set()
    if not data:
        raise RuntimeError(err)
    return data


def plcover_bytes(key):
    """对外入口（/api/plcover）：命中缓存直接回，没命中走单飞。"""
    key = (key or "").strip()[:40]
    if not key:
        raise RuntimeError("no key")
    data = _plcov_cached(key)
    if data:
        return data
    return _plcov_singleflight(key)


def plcover_warmer(first_delay=30, max_fetch=PLCOV_WARM_MAX):
    """JY-PERF：启动后慢慢把「没有图的歌单」补上图。
    用户第一次打开推荐页才是冷启动，那时候要现抓（1~2 秒一张）；
    提前在后台补好，首屏就是满的。**只补磁盘上还没有的**，抓过的不再重复打扰必应。"""
    time.sleep(first_delay)
    try:
        with _data_lock:
            names = [str(p.get("name") or "") for p in (_jy.get("playlists") or [])]
    except Exception:
        names = []
    done = 0
    for nm in names:
        if done >= max_fetch:
            break
        nm = (nm or "").strip()
        if not nm or nm == u"所有歌曲":
            continue
        hid = hashlib.md5(nm.encode("utf-8")).hexdigest()[:16]
        if os.path.isfile(os.path.join(PLCOV_DIR, hid + ".jpg")):
            continue
        try:
            plcover_bytes(nm)
        except Exception:
            pass
        done += 1
        time.sleep(0.4)      # 别一口气把必应打八遍
    log("plcover warmer: 补了 %d 张" % done)


def resolve_download_url(mid):
    """下载直链解析 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ("", {})


def download_song(mid, prefer_name=None):
    """歌曲下载 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return {"ok": False, "msg": _JY_NO_SOURCE_MSG}


# ============================================================ JY-SRC 多音源
# 三源接力。每个源只干它最强的一件事，取不到就自动降级给下一个：
#
#   源A  —— 主力。索引最全，连 VIP 歌都在（返回的就是 源C-CDN 的 CDN 直链）
#   源B  —— 补充。排序准、有官方时长/封面，补外文歌；但 fee=1 的付费歌拿不到 url
#   源C    —— 兜底。它自己的搜索索引弱（没有陈奕迅版权，30 条全是翻唱），
#              但个别曲库只有它有，所以放最后
#
# 为什么不是「源B优先」：实测拿源B的付费歌去源C按歌名+歌手重找，
# 热门港台歌手全部失败（源C压根没版权）；而源A能直接给出同一首的 VIP 直链。
#
# 延迟：平板到这几个域名的网络抖动极大（源A 2.4~12s、源B 1~6s、源C 0.4~5.4s），
# 所以不能靠调超时，而是**并发发起 + 总时长封顶**，到点就返回已经拿到的部分。
#
# ID 约定（跨接口通用，纯数字保持旧行为 = 源A）：
#   "wy:2737753398"  源B    "kw:491280316"  源C    "6420"  源A

JY_SRC_NAME = {"gqb": "源A", "wy": "源B", "kw": "源C"}
JY_SRC_ORDER = ["gqb", "wy", "kw"]          # 搜索结果排列顺序 = 取链降级顺序
JY_SRC_CAP = {"gqb": 8, "wy": 6, "kw": 4}   # 每个源最多贡献几条，别让一家淹没列表
JY_SRC_WAIT = 3.2                            # 搜索总时长上限（秒），到点就交卷
# 为什么是 3.2：源A是主力（唯一能稳定给 VIP 直链的），典型 2.2~2.7 秒，
# 卡 2.5 会经常把它砍掉，列表首条变成要换源才能播的源B付费歌。
# 3.2 能覆盖它绝大多数情况；最坏也就比以前单源的 2.4 秒多 0.8 秒。

_jy_src_cache = {}          # (kind, key) -> (时间戳, 值)
_JY_SRC_TTL = 1800
_jy_src_cfg = {}            # 源开关，落盘 CACHE_DIR/jy_sources.json


def jy_src_cfg_path():
    return os.path.join(CACHE_DIR, "jy_sources.json")


def jy_src_load():
    """读源开关配置。文件不存在就全开（默认行为跟加多源之前一致）"""
    global _jy_src_cfg
    cfg = {k: True for k in JY_SRC_NAME}
    try:
        with open(jy_src_cfg_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        for k in JY_SRC_NAME:
            if isinstance(d.get(k), bool):
                cfg[k] = d[k]
    except Exception:
        pass
    _jy_src_cfg = cfg
    return cfg


def jy_src_save(patch):
    global _jy_src_cfg
    cfg = dict(_jy_src_cfg or jy_src_load())
    for k, v in (patch or {}).items():
        if k in JY_SRC_NAME and isinstance(v, bool):
            cfg[k] = v
    _jy_src_cfg = cfg
    try:
        with open(jy_src_cfg_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("jy_src save fail %r" % (e,))
    return cfg


def jy_src_on(src):
    if not _jy_src_cfg:
        jy_src_load()
    return bool(_jy_src_cfg.get(src, True))


def _jy_cache_get(k):
    h = _jy_src_cache.get(k)
    return h[1] if h and time.time() - h[0] < _JY_SRC_TTL else None


def _jy_cache_put(k, v):
    _jy_src_cache[k] = (time.time(), v)
    if len(_jy_src_cache) > 400:                     # 别无限长
        for kk in sorted(_jy_src_cache, key=lambda x: _jy_src_cache[x][0])[:80]:
            _jy_src_cache.pop(kk, None)
    return v


def _jy_get(url, headers=None, timeout=10, data=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    with opener().open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _jy_jload(txt):
    """源B偶尔在 JSON 前塞 "cm4..." 前缀，从第一个 { 开始截"""
    t = (txt or "").strip()
    if not t.startswith("{"):
        i = t.find("{")
        if i > 0:
            t = t[i:]
    return json.loads(t)


# ---- 歌名/歌手归一化与打分：用来判断「两个源里的是不是同一首歌」
_JY_BR = re.compile(r"[（(\[【].*?[)）\]】]")
_JY_JUNK = ("版", "片段", "dj", "伴奏", "翻唱", "铃声", "纯音乐", "女声", "男声",
            "钢琴", "抖音", "cover", "remix", "live", "instrumental", "童声", "合辑")


def jy_norm(s):
    s = (s or "").lower().replace("&nbsp;", " ")
    s = _JY_BR.sub("", s)
    return re.sub(r"[\s\-_·・.,，。!！?？'\"“”‘’/\\|]+", "", s).strip()


def jy_isjunk(t):
    tl = (t or "").lower()
    return any(m in tl for m in _JY_JUNK)


def jy_score(song, artist, want_song, want_artist):
    """同一首歌的匹配度 0~1。歌手不对时重罚，避免把翻唱当原唱"""
    ts, ta = jy_norm(song), jy_norm(artist)
    ws, wa = jy_norm(want_song), jy_norm(want_artist)
    if not ts or not ws:
        return 0.0
    if ts == ws:
        s = 1.0
    elif ws in ts or ts in ws:
        s = 0.82
    else:
        s = difflib.SequenceMatcher(None, ts, ws).ratio() * 0.85
    if wa:
        if ta == wa:
            s = s if s >= 0.82 else s * 0.92
        elif wa in ta or ta in wa:
            s = min(1.0, s + 0.04) if s >= 0.82 else s * 0.88
        else:
            s *= 0.55
    if jy_isjunk(song) and not jy_isjunk(want_song):
        s *= 0.6
    return round(s, 3)


def jy_src_of(sid):
    """拆 ID → (源, 纯 key)。纯数字 = 源A（向后兼容旧调用）"""
    s = str(sid or "").strip()
    if ":" in s:
        a, b = s.split(":", 1)
        if a in JY_SRC_NAME:
            return a, b
    return "gqb", s


def _jy_mmss_secs(txt):
    """'04:16' / '4:16' / '1:04:16' → 秒；认不出来返回 0。

    源A详情页 appData 的 mp3_duration 就是这个格式（2026-11 实测
    "04:16" = 256 秒，跟源B的 255/256 对得上）。
    """
    t = (txt or "").strip()
    if not t:
        return 0
    try:
        parts = [int(p) for p in t.split(":")]
    except (TypeError, ValueError):
        return 0
    if not parts or any(p < 0 for p in parts):
        return 0
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def _jy_dur_secs(v):
    """缓存里的 dur → 秒。wy/kw 存的是 int 秒，gqb 详情页存的是 'mm:ss' 字符串。

    不管喂进来什么脏东西都返回 int，绝不抛 —— 点歌路径上不能因为一个时长字段 500。
    """
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else 0
    t = str(v).strip()
    if ":" in t:
        return _jy_mmss_secs(t)
    if not t:
        return 0
    try:
        return int(float(t))
    except (TypeError, ValueError):
        return 0


def jy_src_dur(sid):
    """反查这首歌的真实时长（秒），三级递减，查不到返回 0。

    一级 · 按 id 精确命中：扫 _jy_src_cache 找 rec["id"] == sid 的记录取 dur。
        跟 jy_src_lookup 同源。wy / kw 源的记录带真实时长，命中就直接用。

    二级 · 源A详情页缓存（"gqb_dur", mid）：
        gqb 源的搜索页 HTML 里没有时长字段（实测全页 0 处 duration/时长），
        但详情页 /music/<mid> 的 appData 里有 mp3_duration。jy_gqb_url() 本来
        就要抓这个页面（取 play_id），顺手把时长存进缓存 —— 零额外请求。
        stream 分支里 jy_resolve() 跑在 jy_src_dur() 之前，所以到这一步时
        详情页通常已经抓过了。

    三级 · 跨源兜底：gqb 记录本身 dur 恒 0，而 jy_search_all 是三源拿同一个
        关键词并发搜、结果全在同一份缓存里，所以缓存里通常同时躺着同一首歌的
        wy / kw 记录（带真实时长）。按 jy_norm 归一化后的**歌名 + 歌手严格相等**
        去借；两个字段都要完全相等才认，不用 jy_score / 不用包含匹配 —— 这是
        为了不把《孤勇者 萧忆情Alex》的时长借给《孤勇者 陈奕迅》。歌手为空的
        一律不兜底（同名不同歌风险太大）。多个候选取中位数：wy 和 kw 对同一首歌
        偶尔差 1~3 秒，取第一个会有随机性。

    三级都拿不到（缓存过期 / 服务刚重启 / 源本身没有）就返回 0，前端有降级：
    不显示总长、进度条按已播时间渐近、切歌靠设备回报的播放结束事件。
    全程不发任何网络请求。
    """
    # ---- 一级：按 id 精确命中 ----
    for v in _jy_src_cache.values():
        lst = v[1]
        if isinstance(lst, list):
            for rec in lst:
                if isinstance(rec, dict) and rec.get("id") == sid:
                    d = _jy_dur_secs(rec.get("dur"))
                    if d > 0:
                        return d

    # ---- 二级：源A详情页缓存（jy_gqb_url 顺手存的） ----
    src, key = jy_src_of(sid)
    if src == "gqb":
        for k in (key, sid):
            d = _jy_dur_secs(_jy_cache_get(("gqb_dur", k)))
            if d > 0:
                return d

    # ---- 三级：跨源兜底，归一化后歌名 + 歌手严格相等 ----
    song, artist = jy_src_lookup(sid)
    if not song or not artist:
        return 0
    ns, na = jy_norm(song), jy_norm(artist)
    if not ns or not na:
        return 0
    durs = []
    for v in _jy_src_cache.values():
        lst = v[1]
        if not isinstance(lst, list):
            continue
        for rec in lst:
            if not isinstance(rec, dict):
                continue
            if jy_norm(rec.get("song")) != ns or jy_norm(rec.get("artist")) != na:
                continue
            # jy_norm 会把括号里的内容抹掉（《孤勇者（片段）》→《孤勇者》），
            # 所以光靠严格相等仍会把 90 秒的片段版借给 255 秒的原唱。用项目里
            # 现成的 jy_isjunk 堵掉：候选是 片段/伴奏/翻唱/… 而目标不是，就不借
            # （跟 jy_search_all 对 kw 源的处理一致）。这一层只是加严，不是模糊匹配。
            if jy_isjunk(rec.get("song")) and not jy_isjunk(song):
                continue
            d = _jy_dur_secs(rec.get("dur"))
            if d > 0:
                durs.append(d)
    if not durs:
        return 0
    durs.sort()
    n = len(durs)
    # 奇数取正中；偶数取中间两个的整除均值（不引入浮点，保证确定可复现）
    return durs[n // 2] if n % 2 else (durs[n // 2 - 1] + durs[n // 2]) // 2


# ---------------------------------------------------------------- 源A
def jy_gqb_search(kw, limit=15):
    """源A（第三方站点）搜索 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return []


def jy_gqb_url(mid):
    """源A取下载直链 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ""


# ---------------------------------------------------------------- 源B
def jy_wy_search(kw, limit=10):
    """第三方音乐平台搜索（一） —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return []


def jy_wy_url(song_id, br=320000):
    """第三方音乐平台取直链（一） —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ""


def jy_wy_detail(song_id):
    """第三方音乐平台歌曲详情（一） —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ("", "")


# ---------------------------------------------------------------- 源C
def jy_kw_search(kw, limit=15):
    """第三方音乐平台搜索（二） —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return []


def jy_kw_url(rid):
    """第三方音乐平台取直链（二） —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ""


# ---------------------------------------------------------------- 合并搜索 / 取链
_JY_SEARCHERS = {"gqb": jy_gqb_search, "wy": jy_wy_search, "kw": jy_kw_search}


def jy_search_all(kw, limit=None):
    """多源聚合搜索 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return []


def jy_src_lookup(sid):
    """从搜索缓存里反查这首歌的歌名/歌手，这样下载接口不传 hints 也能换源"""
    for v in _jy_src_cache.values():
        lst = v[1]
        if isinstance(lst, list):
            for rec in lst:
                if isinstance(rec, dict) and rec.get("id") == sid:
                    return rec.get("song") or "", rec.get("artist") or ""
    return "", ""


def jy_best_match(cands, song, artist, floor=0.72):
    best, bs = None, 0.0
    for c in cands:
        s = jy_score(c["song"], c["artist"], song, artist)
        if s > bs:
            best, bs = c, s
    return (best if bs >= floor else None), bs


def jy_resolve(sid, song="", artist=""):
    """任意 ID → (直链, 实际来源, 说明, 歌名, 歌手)。逐级换源，这就是「源不行自动换源」的核心"""
    src, key = jy_src_of(sid)
    if not song:
        s2, a2 = jy_src_lookup(sid)
        song, artist = song or s2, artist or a2
    if not song and src == "wy":
        # 缓存里没有（比如服务刚重启、用户直接点了收藏里的歌）→ 按 ID 查详情
        song, artist = jy_wy_detail(key)
    tried = []

    def _direct(s, k):
        if s == "gqb":
            return jy_gqb_url(k)
        return jy_wy_url(k) if s == "wy" else jy_kw_url(k)

    # 1) 本源直链
    if jy_src_on(src):
        u = _direct(src, key)
        if u:
            return u, src, "%s直链" % JY_SRC_NAME[src], song, artist
        tried.append(JY_SRC_NAME[src])
    else:
        tried.append("%s(已关)" % JY_SRC_NAME[src])

    # 2) 换源：按「歌名 + 歌手」在其它源里重找，匹配度 ≥ 0.72 才认
    for s in JY_SRC_ORDER:
        if s == src or not jy_src_on(s):
            continue
        if not song:
            continue
        try:
            cands = _JY_SEARCHERS[s](("%s %s" % (song, artist)).strip() or song, 10)
            if not cands:
                cands = _JY_SEARCHERS[s](song, 10)
        except Exception:
            cands = []
        m, sc = jy_best_match(cands, song, artist)
        if not m:
            if sc:
                tried.append("%s(无匹配%.2f)" % (JY_SRC_NAME[s], sc))
            continue
        u = _direct(s, jy_src_of(m["id"])[1])
        if u:
            return u, s, "换源到%s（匹配 %.2f）" % (JY_SRC_NAME[s], sc), song, artist
        tried.append("%s(匹配到了但没直链)" % JY_SRC_NAME[s])
    return "", "", "所有源都取不到：" + " / ".join(tried), song, artist


def jy_download_url(url, name, got, song="", artist=""):
    """下载并落盘 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return {"ok": False, "msg": _JY_NO_SOURCE_MSG}


def jy_download(sid, prefer_name=None, song="", artist=""):
    """下载入口 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return {"ok": False, "msg": _JY_NO_SOURCE_MSG}


# ------------------------------------------------- JY-STREAM：在线歌「只播不存」
# 用户的要求（2026-10-30）：「本地没有的歌，点播放只播，不许落盘；只有点下载才落盘」。
# 落地方式：musictool 自己去上游取字节流、再原样吐给音箱（见 Handler._jy_stream）。
#
# ⚠ 为什么不能 302 到上游直链：源A走的是 源C-CDN 的 CDN，源C/源B都会校验
#   Referer，音箱拿着一条裸链去拉 → 403 或空流，用户看到的是「能播但一直静音」。
#   所以必须由服务端带上游需要的 Referer/UA 自己代理，给音箱一个「稳定的本地地址」。
#
# ⚠ 无损必须回落下载：语音助手音箱不认 FLAC/APE/AIFF（06-xiaomusic补丁/_patch_xmstream.py
#   里记着实测：push FLAC 会 status=2 + position 恒 0，看着在播其实没声）。所以
#   stream 模式一旦发现上游给的是无损，就要退回「下载 + xiaomusic 转 wav」那条路。
_LOSSLESS_EXT = (".flac", ".ape", ".aiff", ".aif", ".wav")
# 取流时要带的 Referer。
# ⚠ 源A的直链实际落在 源C/源C-CDN 上，而**源C CDN 见到 上游站点 的 Referer
#   会直接 403**（和 fetch_bytes() 里那句注释是同一件事，2026-10-30 实测复现：
#   不带 Referer 200 audio/mpeg 3.6MB，带 上游站点 Referer 403）。所以 gqb 这里留空。


def _url_ext(u):
    """从直链猜扩展名。有些 CDN 的扩展名在 query 里（?f=flac），两处都看。"""
    s = str(u or "").lower()
    m = re.search(r"\.(flac|ape|aiff|aif|wav|mp3|m4a|aac|ogg)(?:[?#&]|$)", s)
    if m:
        return "." + m.group(1)
    m = re.search(r"[?&](?:f|format|type)=([a-z0-9]+)", s)
    if m and m.group(1) in ("flac", "ape", "aiff", "aif", "wav"):
        return "." + m.group(1)
    return ""


def _lan_ip():
    """本机在内网里的地址：音箱要拿这个地址去拉流，不能用 127.0.0.1。
    不真的发包（connect 到 UDP 地址只做路由选择），断网时回落 127.0.0.1。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def jy_stream_base(host):
    """给音箱用的流地址前缀。Host 头是 127.0.0.1/localhost 时换成板子的内网 IP
    （浏览器从局域网打开时会自己带对，只有本机调试才需要这条兜底）。"""
    h = (host or "").split(":")[0].strip()
    if not h or h in ("127.0.0.1", "localhost", "::1", "[::1]"):
        h = _lan_ip()
    return "http://%s:%d" % (h, PORT)


def _g_playing(v):
    """组播轴的 playing 只认**真正的布尔**（含字符串 "true"/"false"/"1"/"0"）。

    返回 (ok, val)；ok=False 表示这一位不合法，调用方必须**整位忽略**。

    为什么不用上面的 `_flag()`：它把 "yes"/"on" 也当真，那对 URL 里的开关是好事，
    对一条会**直接让所有输出设备开始出声**的字段就是洞。2026-10-30 的事故就是这么来的：
    `test_api_robust.py` 发 `{"pos":"abc","playing":"yes"}` 想验「非法输入不崩」，
    `bool("yes")` 是 True → 组播被改成 playing=True → 电脑端被拉起来播、音箱跟着切歌，
    而用户根本没点过播放。**能让人听见声音的字段，宁可不认，也不能猜。**
    """
    if isinstance(v, bool):
        return True, v
    if isinstance(v, (int, float)) and v in (0, 1):
        return True, bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true"):
            return True, True
        if s in ("0", "false"):
            return True, False
    return False, None


def _flag(v):
    """把开关参数认成布尔。

    同一个接口既可能是 JSON（true/false）也可能是表单（"true"/"1"/"on"），
    而 bool("false") 是 True —— 直接 bool() 会把「前端明明关掉了」当成打开。
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _play_online_mode(payload):
    """决定 /api/play_online 走哪条路：返回 "stream" 或 "download"。

    ⚠ v4.00a 紧急改口径：**默认下载**，stream 变成显式 opt-in。
    原因（踩过，别改回去）：pc.html 与 remote.html 拿到返回值后只认 `name`
    （磁盘文件名），再拿去 POST /playmusic。默认要是给 {stream:true,url}，
    前端拿不到 name → 用户点一首本地没有的在线歌**完全没反应**，而 pc.html
    当时已经上线 = 活着的洞。所以：
      · 什么都不带（老客户端）→ download（老行为，返回 name）
      · {"download": true}      → download
      · {"stream": true}        → stream（不下载不 refresh，返回 jy_stream 地址）
    两端都改成显式传 stream 之后，这条默认值也保持不动 —— 默认安全比默认快重要。
    """
    p = payload or {}
    if _flag(p.get("download")):
        return "download"
    if _flag(p.get("stream")):
        return "stream"
    return "download"


def _dl_err(e):
    """把下载失败翻译成「人话」+ 机器可读原因。

    v4.01：以前下载分支直接把原始异常串吐给前端（`HTTP Error 520: <none>`），
    前端只能原样 toast 出来 —— 用户看到的是「点了没反应」。原始异常进日志，
    响应体只给人看得懂的句子，另带 reason 供前端/探针判断。
    """
    s = str(e or "")
    if "520" in s:
        return "上游音乐站暂时不可用（HTTP 520），稍后再试", "upstream_520"
    if "timed out" in s or "timeout" in s.lower():
        return "下载超时（上游太慢），稍后再试", "upstream_timeout"
    if "404" in s:
        return "这首歌在上游已经找不到了", "upstream_404"
    return "下载失败：上游没有给出可用的文件", "download_failed"


def _local_hit(probe, artist):
    """本地曲库里找「同一首」。返回 (文件名|None, 分数)。

    v4.00 的判定口径（以前 _best < 0.85 判不中 → 白下载）：
      · jy_score 的「包含关系」只给 0.82，「孤勇者」vs「孤勇者 (Live)」永远判不中；
        所以门槛从 0.85 降到 0.82 —— 0.82 就是「包含但歌手一致」那一档。
      · 另外用 _track_norm() 去掉扩展名/前导音轨号（"02 孤勇者.mp3"）后再精确比一次，
        归一后同名且歌手对得上，直接算命中。
      · 「包含关系且歌手一致」但分数被别的项拉低的，放宽到 0.80。
    """
    probe = probe or ""
    artist = artist or ""
    probe_n = jy_norm(probe)
    art_n = jy_norm(artist)
    best, hit, exact = 0.0, None, None
    for n in (list_local_songs() or []):
        try:
            a2, s2 = split_title(_track_norm(n))
        except Exception:
            a2, s2 = "", n
        try:
            sc = jy_score(s2, a2, probe, artist)
        except Exception:
            sc = 0.0
        if sc > best:
            hit, best = n, sc
        if exact is None and probe_n:
            s_n, a_n = jy_norm(s2), jy_norm(a2)
            if s_n and s_n == probe_n and (
                    not art_n or a_n == art_n or art_n in a_n or a_n in art_n):
                exact = n
    if exact:
        return exact, max(best, 0.90)
    if best >= 0.82:
        return hit, best
    if best >= 0.80 and art_n and hit:
        try:
            a3, s3 = split_title(_track_norm(hit))
            s_n = jy_norm(s3)
            if jy_norm(a3) == art_n and (s_n in probe_n or probe_n in s_n):
                return hit, best
        except Exception:
            pass
    return None, best


def jy_src_health():
    """各源探活：跑一条最小请求看通不通、多久"""
    out = {}
    for s in JY_SRC_ORDER:
        t0 = time.time()
        try:
            n = len(_JY_SEARCHERS[s]("测试", 1))
            out[s] = {"name": JY_SRC_NAME[s], "on": jy_src_on(s),
                      "ok": n > 0, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            out[s] = {"name": JY_SRC_NAME[s], "on": jy_src_on(s), "ok": False,
                      "err": str(e)[:80], "ms": int((time.time() - t0) * 1000)}
    return out


def fetch_bytes(url, referer=None, timeout=60):
    """抓取二进制资源。源C CDN 对 上游站点 的 Referer 会返回 403，所以默认不带 Referer。"""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with opener().open(req, timeout=timeout) as r:
        return r.read()


def cache_song_assets(name, info):
    """把抓到的封面/歌词写进本地缓存"""
    if info.get("cover"):
        try:
            p = os.path.join(COVER_DIR, sanitize_filename(name) + ".img")
            if not os.path.exists(p):
                data = fetch_bytes(info["cover"], timeout=25)
                if len(data) > 1024:
                    with open(p, "wb") as f:
                        f.write(data)
                    _index.setdefault(name, {})["cover"] = os.path.basename(p)
        except Exception as e:
            log("cover cache fail %r" % (e,))
    if info.get("lrc") and not is_junk_lrc(info["lrc"]):
        try:
            p = os.path.join(LRC_DIR, sanitize_filename(name) + ".lrc")
            with open(p, "w", encoding="utf-8") as f:
                f.write(info["lrc"])
            _index.setdefault(name, {})["lrc"] = os.path.basename(p)
        except Exception as e:
            log("lrc cache fail %r" % (e,))
        write_beside_lrc(name, info["lrc"])
    if name in _index:
        _index[name]["id"] = info.get("id")
        _index[name]["artist"] = info.get("artist")
        _index[name]["title"] = info.get("title")
        _index[name]["ts"] = int(time.time())
        globals()["_index_dirty"] = True


MISS_FILE = os.path.join(CACHE_DIR, "miss.json")


# JY-JUNK: 源站搜不到词时会给一行占位（"该歌曲暂无歌词"）。这种文本一旦落成
# .lrc，serve_lyric 读到文件就直接返回、不再联网，等于把这歌永久钉死在"无词"。
# 三个入口（写缓存 / 落旁置 / 读缓存）统一用 is_junk_lrc 过滤。
_JUNK_LRC = ("该歌曲暂无歌词", "歌曲暂无歌词", "暂无歌词", "纯音乐，请欣赏",
             "纯音乐,请欣赏", "此歌曲为没有填词的纯音乐", "歌词加载失败",
             "暂无歌词，请欣赏", "纯音乐请欣赏")


def is_junk_lrc(lrc):
    """不是有效歌词：空 / 太短 / 有效行不足 3 行 / 命中占位话术"""
    if not lrc or len(lrc.strip()) < 24:
        return True
    n = 0
    for line in lrc.splitlines():
        if re.search(r"\[\d{1,2}:\d{1,2}", line):
            if re.sub(r"\[[^\]]*\]", "", line).strip():
                n += 1
    if n < 3:
        return True
    return any(k in lrc for k in _JUNK_LRC)


def write_beside_lrc(name, lrc):
    """在线匹配到歌词后，在音频文件旁落一个同名 .lrc——
    歌词从此固化进曲库（/sdcard/Music），换播放器、重装 xiaomusic 都不会丢。
    文件名直接取自音频路径（splitext），天然和音频同名。"""
    if is_junk_lrc(lrc):
        return
    try:
        ap = audio_path_of(name)
        if not ap or not os.path.exists(ap):
            return
        lp = os.path.splitext(ap)[0] + ".lrc"
        if os.path.exists(lp):
            return                      # 不覆盖已固化的歌词
        tmp = lp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(lrc)
        os.replace(tmp, lp)
        log("lrc beside: %s" % os.path.basename(lp))
    except Exception as e:
        log("lrc beside fail %r: %s" % (name, e))


def backfill_beside_lrc():
    """一次性回填：把 LRC 缓存里已有的歌词补写到音频旁边（纯本地，不联网）"""
    n = 0
    for s in list_local_songs():
        p = os.path.join(LRC_DIR, sanitize_filename(s) + ".lrc")
        if not os.path.exists(p):
            continue
        try:
            ap = audio_path_of(s)
            if not ap or not os.path.exists(ap):
                continue
            lp = os.path.splitext(ap)[0] + ".lrc"
            if os.path.exists(lp):
                continue
            with open(p, encoding="utf-8") as f:
                write_beside_lrc(s, f.read())
            n += 1
        except Exception as e:
            log("backfill lrc fail %r: %s" % (s, e))
    log("backfill beside lrc done: %d" % n)
    return n


def _load_miss():
    try:
        with open(MISS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_miss = _load_miss()


def _add_miss(name):
    _miss[name] = int(time.time())
    try:
        with open(MISS_FILE, "w", encoding="utf-8") as f:
            json.dump(_miss, f, ensure_ascii=False)
    except Exception:
        pass


def candidate_queries(raw):
    """生成从严到宽的搜索关键词序列。
    JY-KEEPTAG：不再把歌名里的 (ver.)/[Remix]/feat 等括号整段剥掉——
    否则 Remix/版本/翻唱歌会被退化成原版去搜，错配到原版的歌词时间轴（系统性问题，
    见「04 ワールドイズマイン (かぐや&月見ヤチヨ ver.) [CPK! Remix]」时间轴错乱）。
    clean_query 已只剥掉 (Cover:)/(官方)/(DJ版) 这类无语义标记，保留版本/混音/feat 信息。"""
    s = clean_query(raw)
    artist, song = split_title(raw)
    cands = [s]
    if song and song not in cands:
        cands.append(song)
    if artist and song:
        c = "%s %s" % (song, artist)
        if c not in cands:
            cands.append(c)
    return [c for c in cands if c]


def score_result(r, artist_hint, song_hint):
    sc = 0
    rs = (r.get("song") or "").lower()
    rs_core = re.sub(r"[\(\[（【].*", "", rs).strip()
    sh = (song_hint or "").lower()
    sh_core = re.sub(r"[\(\[（【].*", "", sh).strip()
    if sh_core and rs_core == sh_core:
        sc += 100
    elif sh_core and rs_core.startswith(sh_core):
        sc += 70
    elif sh_core and sh_core in rs:
        sc += 50
    elif sh_core and rs and rs in sh:
        sc += 30
    ar = (r.get("artist") or "").lower()
    ah = (artist_hint or "").lower()
    if ah and ar:
        if ah == ar:
            sc += 60
        elif ah in ar or ar in ah:
            sc += 40
        else:
            # JY-SAMENAME: 歌名一样、歌手毫无关系 —— 这类多半是同名不同歌
            # （「燕池 - 昙花」配到方大同的昙花），扣到 0 以下由调用方弃用
            sc -= 120
    return sc


def match_assets(name):
    """给一首本地歌曲匹配封面+歌词"""
    raw = name
    artist_hint, song_hint = split_title(name)

    best, results_used = None, None
    for q in candidate_queries(raw):
        results = search(q, limit=10)
        if not results:
            continue
        results_used = results
        ranked = sorted(results, key=lambda r: -score_result(r, artist_hint, song_hint))
        cand = ranked[0]
        sc0 = score_result(cand, artist_hint, song_hint)
        log("match %r via %r -> %s (%s) score=%d"
            % (raw, q, cand["title"], cand["id"], sc0))
        if sc0 > 0:
            best = cand
            break
        log("match %r reject %r (score=%d, 歌手对不上)" % (raw, cand["title"], sc0))
    if best is None and results_used and not artist_hint:
        # 没有歌手可比时，歌名至少得像：完全相等或互相包含。
        # 以前直接取第一条 —— 「カオ」就是这么配到「ガオレンジャー」的。
        r0 = results_used[0]
        rs = re.sub(r"[\(\[（【].*", "", (r0.get("song") or "")).strip().lower()
        sh = (song_hint or "").strip().lower()
        if sh and rs and (rs == sh or sh in rs or rs in sh):
            best = r0
        elif sh:
            log("match %r reject first-hit %r（歌名对不上）" % (raw, r0.get("song")))

    info = {}
    cover_url = ""
    lrc = ""
    if best:
        try:
            info = fetch_song(best["id"])
            cover_url = info.get("cover") or ""
            lrc = info.get("lrc") or ""
        except Exception as e:
            log("fetch_song fail %r" % (e,))

    # 2.5) 上游站点 没歌词（这首没被收录）→ 源B兜底，含翻译
    if not lrc:
        try:
            lrc = nc_lyric(raw)
        except Exception as e:
            log("nc lyric err %r" % (e,))

    # 2.6) 有原文但没译文 → 借源B的译文挂到本地时间戳上
    if lrc:
        try:
            lrc = trans_of_lrc(raw, lrc)
        except Exception as e:
            log("trans err %r %r" % (raw, e))

    # 3) 落盘缓存
    if cover_url or lrc:
        cache_song_assets(raw, {**info, "cover": cover_url, "lrc": lrc})
        save_index()

    return {
        "name": raw,
        "artist": info.get("artist") or artist_hint,
        "title": info.get("title") or song_hint,
        "cover": cover_url,
        "lrc": lrc,
        "matched_id": best["id"] if best else None,
    }


def cover_cache_paths(name):
    """封面缓存的历史命名不统一（'X.img' / 'X.mp3.img' 都有），按候选依次找"""
    stem = re.sub(r"\.(mp3|flac|m4a|ogg|wav|ape|aac|aiff|wma)$", "", str(name or ""), flags=re.I)
    out = []
    for k in (stem, name):
        for ext in ("", ".mp3", ".flac", ".m4a", ".ogg", ".wav", ".ape"):
            p = os.path.join(COVER_DIR, sanitize_filename(str(k) + ext) + ".img")
            if p not in out:
                out.append(p)
    return out


def _img_side(b):
    """返回图片最长边像素；解析失败返回 0。用来比较两张封面图谁更清晰。"""
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(b)) as im:
            im.load()
            return max(im.size)
    except Exception:
        return 0


# JY-FIX (封面清晰度): 同一首歌，缓存文件名可能带音轨号前缀（"02 星降る海"）
# 也可能不带（"星降る海"）—— 两端只是显示口径不同，但封面按文件名落盘，
# 于是「裸名请求永远找不到带号名的高清封面 / 反之亦然」，播放页只能退到模糊的小图。
# 这里按「去音轨号 + 去扩展名」归一化建索引，裸名与带号名双向互通，且同类取文件最大的（最清晰）。
import threading as _thr
_cover_idx = None
_cover_idx_sig = None
_cover_idx_lock = _thr.Lock()


def _cover_dir_sig():
    """封面目录指纹（目录 mtime + size）。

    目录里增删文件会改目录自己的 mtime，所以「2 次 stat」就够判断要不要重建索引。
    这是 JY-FAST 的关键：以前是「查不到就重建一次」，代价见 _ensure_cover_index 注释。
    """
    sig = []
    for _d in (COVER_DIR, COVER_HI_DIR):
        try:
            _st = os.stat(_d)
            sig.append((_st.st_mtime_ns, _st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _ensure_cover_index(force=False):
    """按需惰性重建封面索引，且**只在封面目录真的变了**时才重建。

    JY-FAST（2026-10-30，App 冷启动 12s 的真凶）：
    老版 `_cover_best()` 每次查不到就 `_rebuild_cover_index()` 一次。而索引重建要
    listdir(COVER_DIR 1054) + listdir(COVER_HI_DIR 550)，并对 1600 个文件逐个
    getsize + 跑一遍 _track_norm 正则，全程还要持锁串行 —— 单次约 0.22s。
    `/api/local` 逐首算 `hicover`：581 首里有 48 首没有高清封面 → 一次请求重建
    48 遍 ≈ 10.6s（实测 11.1s 冷调用，与估算吻合）。
    改成「目录没变就复用上次的索引」后，整表只需 2 次 stat。
    """
    global _cover_idx, _cover_idx_sig
    with _cover_idx_lock:
        sig = _cover_dir_sig()
        if force or _cover_idx is None or sig != _cover_idx_sig:
            _rebuild_cover_index()
            _cover_idx_sig = sig
        return _cover_idx

def _track_norm(name):
    s = re.sub(r"\.(mp3|flac|m4a|ogg|wav|ape|aac|aiff|wma)$", "", str(name or ""), flags=re.I)
    s = re.sub(r"^\s*\d{1,3}[\s._\-]+\s*", "", s).strip()   # 去前导音轨号 "02 " / "3." / "04-" / "05_"
    return s

def _rebuild_cover_index():
    global _cover_idx
    idx = {}
    for d, kind in ((COVER_DIR, "normal"), (COVER_HI_DIR, "hi")):
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.lower().endswith(".img"):
                continue
            base = fn[:-4]
            key = _track_norm(base)
            if not key:
                continue
            e = idx.setdefault(key, {"normal": None, "normal_sz": -1, "hi": None, "hi_sz": -1})
            fp = os.path.join(d, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz > e[kind + "_sz"]:
                e[kind] = fp
                e[kind + "_sz"] = sz
    _cover_idx = idx

def _cover_best(name, kind):
    """返回 name 归一化后同类最清晰的封面文件路径；无则 None。按需惰性重建索引。"""
    norm = _track_norm(name)
    if not norm:
        return None
    e = (_ensure_cover_index() or {}).get(norm)
    hit = e.get(kind) if e else None
    if hit and os.path.exists(hit):
        return hit
    if not hit:
        # 索引里本来就没有 —— 不要在这里重建（就是这一步把 /api/local 拖成 11s 的）。
        # 真新增了封面时，目录 mtime 会变，下一次 _ensure_cover_index() 自然重建。
        return None
    # 索引里有、文件却不在了（被外部删掉）：强制重建一次把它剔掉，之后不会再重建。
    e = (_ensure_cover_index(force=True) or {}).get(norm)
    return e.get(kind) if e else None

def hi_cover_exists(name):
    return bool(_cover_best(name, "hi"))


def serve_cover(name):
    """优先本地缓存的封面图，其次按需抓取"""
    # JY-FIX(v4.02c)：**先按完整文件名精确找，再退回归一化索引**。
    # 归一化会去掉前导音轨号：本地「01 Remember.flac」和在线「Remember」都归一成
    # 「remember」，而封面索引一个 norm 只有一个槽 —— 后入索引的那张会把另一张顶掉，
    # 用户看到的就是「这首歌的封面突然换了，不匹配了」。
    for _p in cover_cache_paths(name):
        try:
            if os.path.exists(_p) and os.path.getsize(_p) > 0:
                with open(_p, "rb") as _f:
                    return _f.read()
        except Exception:
            pass
    # JY-FIX: 按归一化歌名（去音轨号/扩展名）在缓存里找最清晰的那张，裸名与带号名互通
    bp = _cover_best(name, "normal")
    if bp:
        try:
            if os.path.getsize(bp) > 0:
                with open(bp, "rb") as f:
                    return f.read()
        except Exception:
            pass
    cands = cover_cache_paths(name)
    """优先本地缓存的封面图，其次按需抓取"""
    cands = cover_cache_paths(name)
    for p in cands:
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()

    # JY-TAG: 只有高清图、没有 500 缓存时，就地把高清缩一份存成普通封面，
    # 列表缩略图/小组件/播放页三处共用，不用各自再联网一次
    hp = hi_cover_path(name)
    if os.path.exists(hp) and os.path.getsize(hp) > 3000:
        try:
            import io
            from PIL import Image
            with Image.open(hp) as im:
                im = im.convert("RGB")
                im.thumbnail((1000, 1000))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
            data = buf.getvalue()
            with open(cands[0], "wb") as f:
                f.write(data)
            return data
        except Exception as e:
            log("cover thumb fail %r %r" % (name, e))

    if name in _miss and time.time() - _miss[name] < 86400:
        return None

    res = match_assets(name)
    for p in cands:
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    if not res.get("cover"):
        _add_miss(name)
    return None


def _side_lrc(name):
    """曲库里跟音频同名的 .lrc（自己导入的专辑常带）—— 先看这个，别急着联网找"""
    try:
        for fn, rel, _sz in local_files():
            stem = os.path.splitext(fn)[0]
            if stem != name and clean_query(stem) != clean_query(name):
                continue
            lp = os.path.join(MUSIC_DIR, rel, stem + ".lrc") if rel else os.path.join(MUSIC_DIR, stem + ".lrc")
            if os.path.exists(lp):
                with open(lp, encoding="utf-8", errors="ignore") as f:
                    return f.read()
            return ""
    except Exception:
        pass
    return ""


def _side_lrc_path(name):
    """同名 .lrc 的实际路径（找不到返回空串）"""
    try:
        for fn, rel, _sz in local_files():
            stem = os.path.splitext(fn)[0]
            if stem != name and clean_query(stem) != clean_query(name):
                continue
            return os.path.join(MUSIC_DIR, rel, stem + ".lrc") if rel else os.path.join(MUSIC_DIR, stem + ".lrc")
    except Exception:
        pass
    return ""


def _patch_trans(name, lrc, path=None):
    """缓存/旁挂里的歌词只有原文时，顺手补一次译文并写回（补过就不再联网）"""
    try:
        patched = trans_of_lrc(name, lrc)
    except Exception as e:
        log("trans err %r %r" % (name, e))
        return lrc
    if not patched or patched == lrc:
        return lrc
    try:
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(patched)
        write_beside_lrc(name, patched)
    except Exception:
        pass
    return patched


def serve_lyric(name):
    # 纯音乐（无歌词）标记：标了就直接返回空词，跳过缓存/旁置/联网匹配（省一次联网）。
    # 保持返回值为两元组（lrc, artist）—— /api/widget(5381) 与 TV 歌词(4969) 也调它，改签名会带坏它们。
    if (instr_load().get("for") or {}).get(name):
        return "", (_index.get(name) or {}).get("artist", "")
    artist = (_index.get(name) or {}).get("artist", "")
    p = os.path.join(LRC_DIR, sanitize_filename(name) + ".lrc")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                cached = f.read()
        except Exception:
            cached = ""
        if not is_junk_lrc(cached):
            return _patch_trans(name, cached, p), artist
        try:                            # 占位/空壳缓存：删掉，否则这歌永远补不上词
            os.remove(p)
            log("lrc junk purge: %s" % name)
        except Exception:
            pass
    # JY-SIDELRC: 同名 .lrc 优先，且写进缓存，下次直接命中（新导入的歌全靠这条路）
    _side = _side_lrc(name)
    if _side and is_junk_lrc(_side):
        _sp = _side_lrc_path(name)
        if _sp:
            try:
                os.remove(_sp)
                log("lrc junk side purge: %s" % name)
            except Exception:
                pass
        _side = ""
    if _side:
        _side = _patch_trans(name, _side, p)
        try:
            os.makedirs(LRC_DIR, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(_side)
        except Exception:
            pass
        return _side, artist
    if name in _miss and time.time() - _miss[name] < 86400:
        return "", ""
    res = match_assets(name)
    lrc = res.get("lrc") or ""
    if is_junk_lrc(lrc):
        return "", (res.get("artist") or artist)
    return lrc, (res.get("artist") or artist)


def list_local_songs():
    return [r[0] for r in local_files()]


# ---------------------------------------------------------------- 自定义头像
def avatar_read():
    try:
        with open(AVATAR_FILE, "rb") as f:
            return f.read()
    except Exception:
        return b""


def avatar_ts():
    try:
        return int(os.path.getmtime(AVATAR_FILE) * 1000)
    except Exception:
        return 0


def avatar_save(data_url):
    """收 data:image/...;base64 的图，居中裁正方形缩放成 256x256 PNG。
    头像用在 PC 左上角、App 顶部，两边读同一份，换一次两端都变。"""
    m = re.match(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$",
                 str(data_url or ""), re.S)
    if not m:
        return False, "不是有效的图片"
    try:
        raw = base64.b64decode(m.group(1))
    except Exception:
        return False, "图片解码失败"
    if len(raw) < 64:
        return False, "图片是空的"
    if len(raw) > 6 * 1024 * 1024:
        return False, "图片超过 6MB"
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        side = min(im.size)
        l = (im.size[0] - side) // 2
        t = (im.size[1] - side) // 2
        im = im.crop((l, t, l + side, t + side)).resize((256, 256), Image.LANCZOS)
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        out = io.BytesIO()
        bg.convert("RGB").save(out, "PNG", optimize=True)
        raw = out.getvalue()
    except Exception as e:                 # 没装 Pillow 就原样存，别因为缩放失败拒收
        log("avatar resize skip: %r" % (e,))
    try:
        tmp = AVATAR_FILE + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, AVATAR_FILE)
        return True, ""
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- 歌词导入
_LRC_TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_LRC_META = re.compile(r"^\[(ti|ar|al|by|offset|re|ve|length|kana):", re.I)


def _lrc_sec(m):
    frac = m.group(3) or ""
    return int(m.group(1)) * 60 + int(m.group(2)) + (int((frac + "00")[:3]) / 1000.0)


def _lrc_fmt(t):
    t = max(0.0, float(t))
    m = int(t // 60)
    s = t - m * 60
    return "[%02d:%05.2f]" % (m, s)


def lrc_auto(text, dur=0.0):
    """把歌词排上时间轴。

    - 已经带 [mm:ss.xx] 的行当锚点，位置不动；
    - 没时间戳的行，夹在前后锚点之间、按字数权重分配；
    - 全篇都没时间戳时，用整首歌时长铺开（留 3 秒引子、4 秒尾奏）。
    这样「复制一段纯文字歌词」也能直接唱。"""
    rows = []
    for raw in str(text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _LRC_META.match(line) and not _LRC_TS.match(line):
            continue                       # [ti:] [ar:] [by:] 这类整行元信息
        stamps = _LRC_TS.findall(line)
        body = _LRC_TS.sub("", line).strip()
        if not body:
            continue
        rows.append([_lrc_sec(_LRC_TS.search(line)) if stamps else None, body])
    if not rows:
        return ""
    timed = [r for r in rows if r[0] is not None]
    dur = float(dur or 0)
    if dur <= 0:
        dur = max(180.0, (timed[-1][0] if timed else 0) + 30.0)
    total_w = sum(max(6, len(r[1])) for r in rows) or 1

    if not timed:
        start, end = 3.0, max(dur - 4.0, 12.0)
        span = end - start
        pos, acc = start, 0.0
        for r in rows:
            r[0] = pos
            acc += max(6, len(r[1]))
            pos = start + span * acc / total_w
    else:
        # 前置一段没时间戳的：按字数往前推
        i = 0
        while i < len(rows) and rows[i][0] is None:
            i += 1
        if i > 0 and i < len(rows):
            t0 = rows[i][0]
            w = sum(max(6, len(rows[k][1])) for k in range(i)) or 1
            back = min(t0 - 1.0, max(2.0, w * 0.28))
            pos, acc = max(0.0, t0 - back), 0.0
            for k in range(i):
                rows[k][0] = pos
                acc += max(6, len(rows[k][1]))
                pos = max(0.0, t0 - back) + (back - 0.4) * acc / w
        # 中间每两个锚点之间的一段
        j = 0
        while j < len(rows):
            if rows[j][0] is None:
                a = j - 1
                b = j
                while b < len(rows) and rows[b][0] is None:
                    b += 1
                ta = rows[a][0] if a >= 0 else 0.0
                tb = rows[b][0] if b < len(rows) else min(dur - 3.0, ta + 3.0 * (b - a))
                if tb <= ta:
                    tb = ta + 2.0 * (b - a)
                gap = (b - a)
                w = sum(max(6, len(rows[k][1])) for k in range(j, b)) or 1
                # 锚点后面紧跟的第一句要留一句话的起步量，否则会跟锚点撞在同一秒
                base = ta + (0.0 if a < 0 else min(1.4, max(0.5, (tb - ta) * 0.2)))
                if base >= tb:
                    base = ta + 0.5
                pos, acc = base, 0.0
                for k in range(j, b):
                    pos = base + (tb - base) * acc / w
                    rows[k][0] = pos
                    acc += max(6, len(rows[k][1]))
                j = b
            else:
                j += 1
    out, prev = [], -1.0
    for t, body in rows:
        if t is None:
            t = prev + 2.0
        if t <= prev:
            t = prev + 0.15           # 不能出现倒序/同时刻，播放器会漏句
        prev = t
        out.append(_lrc_fmt(t) + body)
    return "\n".join(out)


def lyric_save(name, lrc):
    """手写/导入的歌词落盘：缓存 .lrc（优先命中）+ 音频旁的同名 .lrc（换播放器也在）。"""
    p = os.path.join(LRC_DIR, sanitize_filename(name) + ".lrc")
    try:
        os.makedirs(LRC_DIR, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(lrc)
        os.replace(tmp, p)
    except Exception as e:
        return False, str(e)
    try:
        ap = audio_path_of(name)
        if ap and os.path.exists(ap):
            lp = os.path.splitext(ap)[0] + ".lrc"
            with open(lp + ".tmp", "w", encoding="utf-8") as f:
                f.write(lrc)
            os.replace(lp + ".tmp", lp)
    except Exception as e:
        log("lyric sidecar fail %r" % (e,))
    try:
        _miss.pop(name, None)          # 之前「查不到」的结论作废
    except Exception:
        pass
    return True, ""


_warm = {"running": False, "done": 0, "total": 0, "hit": 0, "started": 0}
_last_user = [0.0]


# ---------------------------------------------------------------- 歌词报错收集
_lrlock = threading.Lock()


def lrc_report_load():
    try:
        with open(LRCREPORT_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            d.setdefault("seq", len(d["items"]))
            return d
    except Exception:
        pass
    return {"seq": 0, "items": []}


def lrc_report_save(d):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = LRCREPORT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LRCREPORT_FILE)


def lrc_report_add(p):
    """记一条歌词错误。同一首歌 + 同一类型 + 同一时间点视为同一条，只累加次数。"""
    name = str(p.get("name") or "").strip()
    if not name:
        return None, "缺少 name"
    kind = str(p.get("kind") or "other").strip().lower()
    if kind not in LRC_KINDS:
        kind = "other"
    try:
        t = round(float(p.get("t") or 0), 3)
    except Exception:
        t = 0.0
    try:
        delta = round(float(p.get("delta") or 0), 2)
    except Exception:
        delta = 0.0
    delta = max(-60.0, min(60.0, delta))
    text = str(p.get("text") or "")[:200]
    fix = str(p.get("fix") or "")[:200]
    note = str(p.get("note") or "")[:200]
    by = str(p.get("by") or "")[:16]
    tail = bool(p.get("tail"))

    with _lrlock:
        d = lrc_report_load()
        now = int(time.time())
        hit = None
        for it in d["items"]:
            # 时间差 0.2s 内算同一行：同一句被反复标记时前端算出的 t 会有小抖动
            if (it.get("name") == name and it.get("kind") == kind
                    and abs(float(it.get("t") or 0) - t) <= 0.2):
                hit = it
                break
        if hit is not None:
            hit["count"] = int(hit.get("count") or 0) + 1
            hit["ts"] = now
            if delta:
                hit["delta"] = delta
            if tail:
                hit["tail"] = True
            if fix:
                hit["fix"] = fix
            if note:
                hit["note"] = note
            hit["text"] = text or hit.get("text") or ""
            if hit.get("state") == "done":      # 已修过又被标记 ⇒ 重新打开
                hit["state"] = "new"
        else:
            d["seq"] = int(d.get("seq") or 0) + 1
            hit = {"id": "r%d" % d["seq"], "name": name, "t": t, "text": text,
                   "kind": kind, "delta": delta, "tail": tail, "fix": fix,
                   "note": note, "by": by, "ts": now, "count": 1, "state": "new"}
            d["items"].append(hit)
        lrc_report_save(d)
    log("lrc report %s %s t=%.1f %s" % (hit["id"], name, t, kind))
    return hit, ""


def lrc_report_done(ids, name="", state="done"):
    with _lrlock:
        d = lrc_report_load()
        n = 0
        ids = set(str(x) for x in (ids or []))
        for it in d["items"]:
            if it.get("state") == state:
                continue
            if ids and it.get("id") in ids:
                it["state"] = state
                it["done_ts"] = int(time.time())
                n += 1
            elif (not ids) and name and it.get("name") == name:
                it["state"] = state
                it["done_ts"] = int(time.time())
                n += 1
        if n:
            lrc_report_save(d)
    return n


def touch_user():
    """记录用户主动请求的时间，预热任务据此让路"""
    _last_user[0] = time.time()


def warm_worker(delay=1.5):
    try:
        songs = list_local_songs()
        _warm.update(running=True, done=0, total=len(songs), hit=0,
                     started=int(time.time()))
        for s in songs:
            name = clean_query(s)
            cp = os.path.join(COVER_DIR, sanitize_filename(s) + ".img")
            lp = os.path.join(LRC_DIR, sanitize_filename(s) + ".lrc")
            if os.path.exists(cp) and os.path.exists(lp):
                _warm["hit"] += 1
                _warm["done"] += 1
                continue
            if name in _miss and time.time() - _miss[name] < 86400:
                _warm["done"] += 1
                continue
            # 用户在操作时先让路，避免点歌卡住
            while time.time() - _last_user[0] < 15:
                time.sleep(3)
            try:
                match_assets(s)
            except Exception as e:
                log("warm fail %r" % (e,))
            _warm["done"] += 1
            time.sleep(delay)
    finally:
        _warm["running"] = False
        save_index()


def refresh_backend():
    """让 xiaomusic 重新扫描歌曲目录（JY-NOSTOP）
    走内部端点 /jygenlist（= 下载完成后的同一刷新路径），不经过命令通道，
    因此不会触发 do_check_cmd 的 group_force_stop_xiaoai()，正在播的歌不会被打断。
    """
    try:
        with urllib.request.urlopen(BACKEND_API + "/jygenlist", timeout=30) as r:
            j = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        if str(j.get("ret") or "") != "OK":
            log("refresh(jygenlist) ret=%r" % (j.get("ret"),))
            return False
        return True
    except Exception as e:
        log("refresh fail %r" % (e,))
        return False


# ---------------------------------------------------------------- 歌单/推荐/历史
# 自定义歌单 = MUSIC_DIR 下的子目录（xiaomusic 的 music_library 按目录生成播放列表），
# 歌单内用软链引用真实歌曲文件，删歌单/移出歌曲都不会动到原文件。
DATA_FILE = os.path.join(CACHE_DIR, "jy_data.json")
PY_OK = True
try:
    from pypinyin import lazy_pinyin, Style
except Exception:  # pragma: no cover
    PY_OK = False

_data_lock = threading.Lock()
_jy = {"playlists": [], "playlog": {}, "tags": []}   # playlog: {歌名: {count, last}}; tags: 用户感兴趣的推荐标签


def load_data():
    global _jy
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            _jy = json.load(f)
    except Exception:
        _jy = {"playlists": [], "playlog": {}, "tags": []}
    _jy.setdefault("playlists", [])
    _jy.setdefault("playlog", {})
    _jy.setdefault("tags", [])


def save_data():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_jy, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        log("save_data failed: %r" % (e,))


def is_custom_playlist(name):
    """是否用户创建的歌单（区别于随便放进来的其他目录）"""
    with _data_lock:
        return any(p.get("name") == name for p in _jy["playlists"])


def playlist_dir(name):
    return os.path.join(MUSIC_DIR, sanitize_filename(name))


def playlist_songs(name):
    """歌单内的歌曲文件名列表（软链也算文件）"""
    d = playlist_dir(name)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if fn.lower().endswith((".mp3", ".flac", ".m4a", ".wav", ".ape", ".ogg", ".aac")):
            out.append(fn)
    return sorted(out)


def find_song_file(name):
    """在 MUSIC_DIR（含子目录）里找歌曲文件，返回相对 MUSIC_DIR 的路径"""
    for root, dirs, files in os.walk(MUSIC_DIR):
        dirs[:] = [d for d in dirs if d not in ("tmp", "cache", "download", ".thumbnails")]
        if name in files:
            return os.path.relpath(os.path.join(root, name), MUSIC_DIR)
    return None


def find_song_file_fuzzy(name):
    """按 xiaomusic 展示名模糊匹配 MUSIC_DIR 里的真实文件（展示名与文件名歌手/歌名顺序可能相反）"""
    rel = find_song_file(name)
    if rel:
        return rel
    import re as _re
    _EXT = _re.compile(r"\.(mp3|flac|m4a|wav|ape|ogg|aac)$", _re.I)

    def stem(s):
        return _EXT.sub("", str(s))

    def norm(s):
        return _re.sub(r"[\s_\-·・（）()【】\[\]'\"]+", "", stem(s).lower())

    def toks(s):
        return set(_re.findall(r"[a-z0-9\u4e00-\u9fff]+", stem(s).lower()))

    target = norm(name)
    t_toks = toks(name)
    if not target:
        return None
    best, best_score = None, 0.0
    for root, dirs, files in os.walk(MUSIC_DIR):
        dirs[:] = [d for d in dirs if d not in ("tmp", "cache", "download", ".thumbnails")]
        for fn in files:
            if not _EXT.search(fn):
                continue
            n = norm(fn)
            n_toks = toks(fn)
            if not n or not n_toks:
                continue
            if n == target:
                score = 100.0
            elif t_toks and t_toks == n_toks:
                score = 90.0
            elif t_toks and (t_toks <= n_toks or n_toks <= t_toks):
                score = 72.0
            elif t_toks and n_toks:
                score = (len(t_toks & n_toks) / max(1, len(t_toks | n_toks))) * 55.0
            else:
                score = 0.0
            if score > best_score:
                best_score, best = score, os.path.relpath(os.path.join(root, fn), MUSIC_DIR)
    return best if best_score >= 55.0 else None


def add_to_playlist(pl_name, song):
    """把歌曲复制进歌单（/sdcard 是 FUSE，不支持软链；复制时保留 mtime 便于识别新旧）"""
    rel = find_song_file(song)
    if not rel:
        return {"ok": False, "msg": "找不到歌曲文件"}
    d = playlist_dir(pl_name)
    if not os.path.isdir(d):
        return {"ok": False, "msg": "歌单不存在"}
    src = os.path.join(MUSIC_DIR, rel)
    dst = os.path.join(d, os.path.basename(rel))
    try:
        if os.path.exists(dst):
            # 源文件更新过（重新下载）就同步一份
            if os.path.getsize(src) != os.path.getsize(dst) or \
               int(os.path.getmtime(src)) != int(os.path.getmtime(dst)):
                shutil.copy2(src, dst)
                return {"ok": True, "updated": True}
            return {"ok": True, "existed": True}
        shutil.copy2(src, dst)
    except Exception as e:
        return {"ok": False, "msg": "添加失败：%r" % (e,)}
    return {"ok": True}


def remove_from_playlist(pl_name, song):
    """只删歌单里的软链，不动真实文件"""
    d = playlist_dir(pl_name)
    p = os.path.join(d, sanitize_filename(song))
    if os.path.islink(p) or os.path.isfile(p):
        try:
            os.remove(p)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": "移除失败：%r" % (e,)}
    return {"ok": True, "existed": False}


# ---------------------------------------------------------------- 双端状态同步
# PC 客户端和平板 App 各存着一份「输出设备」选择。用户在任意一端启用/关闭音箱，
# 另一端要跟着变，否则两边显示打架、下一首歌不知道往哪儿放。
# 只同步「音箱是否启用」这一个开关：PC 的「本机」是电脑出声，不是手机，
# 硬把 phone/tv 映射过去只会误伤。
STATE_FILE = "/sdcard/.jystate.json"
_state_lock = threading.Lock()
_mem_state = {"box": True, "rev": 0, "by": "", "ts": 0}


def read_state():
    """读当前共享状态；文件损坏/不可读时退回内存里的那份"""
    d = dict(_mem_state)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if "box" in raw:
                d["box"] = bool(raw.get("box"))
            for k in ("rev", "ts"):
                try:
                    d[k] = int(raw.get(k) or 0)
                except Exception:
                    pass
            d["by"] = str(raw.get("by") or "")
    except Exception:
        pass
    return d


def write_state(box, by=""):
    """写入开关（值没变就不涨 rev，避免两端来回刷）；返回写后的完整状态"""
    global _mem_state
    with _state_lock:
        cur = read_state()
        if box is not None:
            box = bool(box)
            if box != cur["box"]:
                cur["rev"] = int(cur.get("rev") or 0) + 1
                cur["box"] = box
                cur["by"] = (str(by or "") or "?")[:16]
                cur["ts"] = int(time.time())
            _mem_state = dict(cur)
            try:
                tmp = STATE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False)
                os.replace(tmp, STATE_FILE)
            except Exception:
                pass          # 目录只读时只保留内存态，不因此报错
        return cur


# ------------------------------------------------- 音画补偿（App / PC 共用一份）
# 每首歌的「音画补偿」以前各端各存各的 localStorage，结果：
#   · 手机上调过的歌，电脑上还是默认值，来回不一致；
#   · 没在放歌时点 −/+ 会静默改写「全局默认」（PC 上被改成 0.9 就是这么来的），
#     于是所有没单独调过的歌全跟着偏离出厂默认的 1.5s。
# 现在把值放到服务端：两端读写同一份，各自不再保留本机副本。
# 独立成一个文件 + 独立版本号，不塞进 /api/group —— 那条 share rev 会触发各端
# 跟轴/跳转，为一个调音值去动它风险不划算。
AV_FILE = "/sdcard/.jyav.json"
AV_DEF = 0.0          # 出厂默认：不偏移（0s）。v3.95 起以 0 作为 [-1.5, 1.5] 的中间值（用户要求把之前的 -1.5s 调回 0）
AV_MAX = 1.5          # 音画补偿允许范围 [-AV_MAX, AV_MAX] = [-1.5, 1.5]（v3.95：从 ±3.0 收窄到 ±1.5）
AV_MAX_SONGS = 400    # 记这么多首就够了，再老的名字丢掉，别让文件无限涨
_av_lock = threading.Lock()


def av_load():
    """读原始档：{"rev":int, "ts":int, "lag":float|None, "for":{歌名:{"v":float,"ts":int}}}"""
    d = {"rev": 0, "ts": 0, "lag": None, "for": {}}
    try:
        with open(AV_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in ("rev", "ts"):
                try:
                    d[k] = int(raw.get(k) or 0)
                except Exception:
                    pass
            try:
                lag = raw.get("lag")
                d["lag"] = None if lag is None else max(-AV_MAX, min(AV_MAX, round(float(lag), 2)))
            except Exception:
                d["lag"] = None
            fo = raw.get("for")
            if isinstance(fo, dict):
                out = {}
                for nm, row in fo.items():
                    v, ts = None, 0
                    if isinstance(row, dict):
                        try:
                            v = float(row.get("v"))
                        except Exception:
                            v = None
                        try:
                            ts = int(row.get("ts") or 0)
                        except Exception:
                            ts = 0
                    else:                      # 老格式：直接就是数字
                        try:
                            v = float(row)
                        except Exception:
                            v = None
                    if v is None or v < -AV_MAX or v > AV_MAX:
                        continue
                    out[str(nm)] = {"v": round(v, 2), "ts": ts}
                d["for"] = out
    except Exception:
        pass
    return d


def av_save(d):
    try:
        tmp = AV_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, AV_FILE)
    except Exception:
        pass          # SD 卡只读时只留内存态，不因此把整次请求打挂


def av_snapshot(d=None):
    d = d or av_load()
    return {"ok": True, "rev": int(d.get("rev") or 0), "ts": int(d.get("ts") or 0),
            "lag": d.get("lag"), "def": AV_DEF,
            "for": {k: v["v"] for k, v in (d.get("for") or {}).items()}}


def av_write(payload):
    """局部更新：lag=数值|null(null 表示回到出厂默认)，for={歌名: 数值|null(null=删掉这首)}"""
    with _av_lock:
        d = av_load()
        dirty = False
        now = int(time.time())

        if "lag" in payload:
            want = payload.get("lag")
            if want is None or want == "":
                v = None                       # 没设过 → 各端用出厂默认
            else:
                try:
                    v = max(-AV_MAX, min(AV_MAX, round(float(want), 2)))
                except Exception:
                    v = None
            if d.get("lag") != v:
                d["lag"] = v
                dirty = True

        fo = payload.get("for")
        if isinstance(fo, dict):
            cur = dict(d.get("for") or {})
            for nm, val in fo.items():
                nm = str(nm)[:200]
                if val is None or val == "":
                    if nm in cur:
                        cur.pop(nm, None)
                        dirty = True
                    continue
                try:
                    val = max(-AV_MAX, min(AV_MAX, round(float(val), 2)))
                except Exception:
                    continue
                old = cur.get(nm)
                if not old or abs(float(old.get("v") or -1) - val) > 1e-6:
                    cur[nm] = {"v": val, "ts": now}
                    dirty = True
            if len(cur) > AV_MAX_SONGS:         # 只留最近改过的那些
                cur = dict(sorted(cur.items(),
                                  key=lambda kv: -(int(kv[1].get("ts") or 0)))[:AV_MAX_SONGS])
                dirty = True
            d["for"] = cur

        if dirty:
            d["rev"] = int(d.get("rev") or 0) + 1
            d["ts"] = now
            av_save(d)
            try:
                log("av rev=%s lag=%s n=%d" % (d["rev"], d["lag"], len(d["for"])))
            except Exception:
                pass
        return d


# ---------------------------------------------------------------- 纯音乐标记（按歌记一份，App / PC 共用）
# 标了纯音乐的歌：① 不弹「导入歌词」提醒；② PC 端桌面歌词小窗不显示。
# 存成独立小文件，跟音画补偿彻底分开，互不影响。
INSTR_FILE = os.path.join(os.path.dirname(AV_FILE), ".jyinstr.json")
INSTR_MAX_SONGS = 400
_instr_lock = threading.Lock()


def instr_load():
    d = {"rev": 0, "ts": 0, "for": {}}
    try:
        with open(INSTR_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in ("rev", "ts"):
                try:
                    d[k] = int(raw.get(k) or 0)
                except Exception:
                    pass
            fo = raw.get("for")
            if isinstance(fo, dict):
                out = {}
                for nm, v in fo.items():
                    try:
                        b = bool(int(v))
                    except Exception:
                        b = False
                    if b:
                        out[str(nm)] = 1
                d["for"] = out
    except Exception:
        pass
    return d


def instr_save(d):
    try:
        tmp = INSTR_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, INSTR_FILE)
    except Exception:
        pass


def instr_snapshot(d=None):
    d = d or instr_load()
    return {"ok": True, "rev": int(d.get("rev") or 0), "ts": int(d.get("ts") or 0),
            "for": {k: 1 for k in (d.get("for") or {})}}


def instr_write(payload):
    """局部更新：for={歌名: 0|1|null}；1=标纯音乐，0/null=取消。"""
    with _instr_lock:
        d = instr_load()
        dirty = False
        now = int(time.time())
        fo = payload.get("for")
        if isinstance(fo, dict):
            cur = dict(d.get("for") or {})
            for nm, val in fo.items():
                nm = str(nm)[:200]
                on = bool(val)
                if on:
                    if nm not in cur:
                        cur[nm] = 1
                        dirty = True
                else:
                    if nm in cur:
                        cur.pop(nm, None)
                        dirty = True
            if len(cur) > INSTR_MAX_SONGS:
                cur = dict(list(cur.items())[:INSTR_MAX_SONGS])
                dirty = True
            d["for"] = cur
        if dirty:
            d["rev"] = int(d.get("rev") or 0) + 1
            d["ts"] = now
            instr_save(d)
        return d


# ---- 逐行歌词时间微调（用户自己修，存平板，立即生效）----
# 仅存「某歌某行的偏移秒数」，由两端在加载歌词后自行应用到时间戳上；
# 不改动服务端 LRC 文件本身，避免和译文插入/缓存逻辑打架。
# 行 key = JSON([基准秒, 规范化文本])，由客户端用基准时间戳算，与服务端解耦。
LRCADJ_FILE = os.path.join(os.path.dirname(AV_FILE), ".jylrcadj.json")
_lrcadj_lock = threading.Lock()
LRCADJ_MAX_SONGS = 400
LRCADJ_MAX_LINES = 300
LRCADJ_CLAMP = 10.0     # 单行偏移上限 ±10s，够修「慢/快一拍」也防误拖飞

def lrcadj_load():
    try:
        with open(LRCADJ_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}

def lrcadj_save(d):
    try:
        os.makedirs(os.path.dirname(LRCADJ_FILE), exist_ok=True)
        tmp = LRCADJ_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=0)
        os.replace(tmp, LRCADJ_FILE)
    except Exception as e:
        log("lrcadj_save fail %r" % e)

def lrcadj_snapshot(d=None):
    d = d or lrcadj_load()
    return {"ok": True, "rev": int(d.get("rev") or 0), "ts": int(d.get("ts") or 0),
            "for": {k: (v if isinstance(v, dict) else {}) for k, v in (d.get("for") or {}).items()}}

def lrcadj_write(payload):
    """局部更新：for={歌名:{行key: 偏移秒}}；偏移 0/null 视为删除该行微调。"""
    with _lrcadj_lock:
        d = lrcadj_load()
        dirty = False
        now = int(time.time())
        fo = payload.get("for")
        if isinstance(fo, dict):
            cur = dict(d.get("for") or {})
            for nm, lines in fo.items():
                nm = str(nm)[:200]
                if not isinstance(lines, dict):
                    continue
                sub = dict(cur.get(nm) or {})
                for kk, val in lines.items():
                    kk = str(kk)[:300]
                    try:
                        vv = float(val)
                    except Exception:
                        vv = 0.0
                    if not vv or abs(vv) < 0.001:
                        if kk in sub:
                            sub.pop(kk, None); dirty = True
                    else:
                        vv = max(-LRCADJ_CLAMP, min(LRCADJ_CLAMP, round(vv, 3)))
                        if sub.get(kk) != vv:
                            sub[kk] = vv; dirty = True
                if not sub:
                    if nm in cur:
                        cur.pop(nm, None); dirty = True
                else:
                    cur[nm] = sub
            if len(cur) > LRCADJ_MAX_SONGS:
                cur = dict(list(cur.items())[:LRCADJ_MAX_SONGS]); dirty = True
            d["for"] = cur
        if dirty:
            d["rev"] = int(d.get("rev") or 0) + 1
            d["ts"] = now
            lrcadj_save(d)
        return d


# ---------------------------------------------------------------- 多设备同播（环绕声）
# 服务端只保存一条时间轴：谁在放、从哪一秒开始放、现在在不在放，
# 外加一张成员表（哪些设备参与）和每台的音量 / 延迟 / 声道偏置。
# 各端拿同一条轴各自算「现在该放到第几秒」，谁也不是绝对权威，
# 也就不存在一边慢半拍被另一边拖着走的问题。

GROUP_FILE = os.path.join(os.path.dirname(STATE_FILE) or "/sdcard", ".jygroup.json")
DEV_KEYS = ("box", "pc", "tv", "phone")

_g_lock = threading.Lock()
_g_cache = {"rev": 0, "name": "", "pos": 0.0, "ts": 0, "playing": False,
            "outs": ["box"], "vol": {}, "delay": {}, "pan": {}, "part": {},
            "eq": {"bass": 0.0, "treble": 0.0}, "by": ""}

# 音色（低音/高音）增益范围，dB。超出当非法值丢掉而不是硬夹到边界，
# 免得前端滑杆的临时越界值把别人刚调好的音色顶掉。
EQ_KEYS = ("bass", "treble")
EQ_LIMIT = 12.0


def group_ms():
    return int(time.time() * 1000)


def _g_live_pos(g):
    """按时间轴外推当前进度（秒）"""
    p = float(g.get("pos") or 0.0)
    ts = int(g.get("ts") or 0)
    if g.get("playing") and ts:
        p += max(0, group_ms() - ts) / 1000.0
    return max(0.0, round(p, 2))


def read_group():
    g = dict(_g_cache)
    # 浅拷贝会把 eq / part 这些 dict 一起共享出去，读的时候就地改就会污染缓存
    g["eq"] = dict(_g_cache.get("eq") or {})
    g["part"] = dict(_g_cache.get("part") or {})
    try:
        with open(GROUP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in ("rev", "ts"):
                try:
                    g[k] = int(raw.get(k) or 0)
                except Exception:
                    pass
            for k in ("pos",):
                try:
                    g[k] = float(raw.get(k) or 0)
                except Exception:
                    pass
            g["name"] = str(raw.get("name") or "")
            g["playing"] = bool(raw.get("playing"))
            g["by"] = str(raw.get("by") or "")
            if raw.get("mode") in ("seq", "all", "one", "rnd"):
                g["mode"] = str(raw.get("mode"))
            outs = [x for x in (raw.get("outs") or []) if x in DEV_KEYS]
            if outs:
                g["outs"] = sorted(set(outs), key=DEV_KEYS.index)
            for k in ("vol", "delay", "pan"):
                v = raw.get(k)
                if isinstance(v, dict):
                    g[k] = {kk: vv for kk, vv in v.items() if kk in DEV_KEYS}
            eq = raw.get("eq")
            if isinstance(eq, dict):
                for kk in EQ_KEYS:
                    try:
                        g["eq"][kk] = max(-EQ_LIMIT, min(EQ_LIMIT, float(eq.get(kk) or 0)))
                    except Exception:
                        pass
            part = raw.get("part")
            if isinstance(part, dict):
                for dk, dv in part.items():
                    if dk in DEV_KEYS and dv in PART_KEYS:
                        g["part"][dk] = dv
    except Exception:
        pass
    return g


def _g_save(g):
    try:
        tmp = GROUP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(g, f, ensure_ascii=False)
        os.replace(tmp, GROUP_FILE)
    except Exception:
        pass


def write_group(patch, by=""):
    """局部更新：只改传进来的字段。任一字段真变了才涨 rev，
    时间轴锚点（pos/ts）只在「换歌 / 跳进度 / 播放态变化」时移动。"""
    global _g_cache
    with _g_lock:
        g = read_group()
        now = group_ms()
        dirty = False

        if "outs" in patch and isinstance(patch.get("outs"), list):
            outs = [x for x in patch["outs"] if x in DEV_KEYS]
            outs = sorted(set(outs), key=DEV_KEYS.index)
            if outs != g.get("outs"):
                g["outs"] = outs
                dirty = True

        for k in ("vol", "delay", "pan"):
            if isinstance(patch.get(k), dict):
                cur = dict(g.get(k) or {})
                for dk, dv in patch[k].items():
                    if dk not in DEV_KEYS:
                        continue
                    try:
                        dv = float(dv)
                    except Exception:
                        continue
                    if k == "pan":                     # 声道偏置只有 -1(全左) ~ +1(全右)
                        dv = max(-1.0, min(1.0, dv))
                    if abs(float(cur.get(dk, 0) or 0) - dv) > 1e-6:
                        cur[dk] = dv
                        dirty = True
                g[k] = cur

        # 低音 / 高音：一条曲线两头各一个点，不按设备分（只有电脑那台能真加）。
        # 非法值（非数字 / 超范围）整条丢掉，别把别人调好的音色顶成 0。
        if isinstance(patch.get("eq"), dict):
            cur = dict(g.get("eq") or {})
            for ek, ev in patch["eq"].items():
                if ek not in EQ_KEYS:
                    continue
                try:
                    ev = float(ev)
                except Exception:
                    continue
                if not (-EQ_LIMIT <= ev <= EQ_LIMIT):
                    continue
                if abs(float(cur.get(ek, 0) or 0) - ev) > 1e-6:
                    cur[ek] = round(ev, 2)
                    dirty = True
            g["eq"] = cur

        # 声部：每台设备放「完整 / 人声 / 伴奏」哪一条。full 是默认，不占空间。
        if isinstance(patch.get("part"), dict):
            cur = dict(g.get("part") or {})
            for dk, dv in patch["part"].items():
                if dk not in DEV_KEYS:
                    continue
                dv = str(dv or "full").strip().lower()
                if dv not in PART_KEYS:
                    continue
                if str(cur.get(dk) or "full") != dv:
                    if dv == "full":
                        cur.pop(dk, None)
                    else:
                        cur[dk] = dv
                    dirty = True
            g["part"] = cur

        name = patch.get("name")
        if name is not None and str(name) != str(g.get("name") or ""):
            g["name"] = str(name)
            try:
                g["pos"] = max(0.0, float(patch.get("pos") or 0))
            except Exception:
                g["pos"] = 0.0
            g["ts"] = now
            # v4.00：带 name 但**没带 playing** 的 patch 以前被默认当成「正在播」，
            # 于是一端只报「换歌了」就把另一端刚按下的暂停顶回播放。
            # 缺省时保持原值（真换歌想开播，调用方就显式带 playing:true）。
            # v4.01：带了 playing 但**值不合法**（"yes" / "abc" / {} …）也当没带，
            # 不许用 bool() 猜 —— 这一位是「让所有输出设备开始出声」的开关。
            _okp, _vp = _g_playing(patch.get("playing"))
            g["playing"] = _vp if _okp else bool(g.get("playing", True))
            dirty = True
            # JY-WAVCACHE：换歌 = 上一首的转码 wav 已经落地，下一首很可能马上又要转一次。
            # 这是 musictool 能拿到的、离「生成完一个 wav」最近的那个事件（一次一首，异步）
            try:
                wavcache_after_write("songchange")
            except Exception:
                pass
        elif "pos" in patch:
            try:
                g["pos"] = max(0.0, float(patch.get("pos") or 0))
                # v4.00：暂停态下刷 ts 会让「暂停点」被后续心跳一路往后推（表现为
                # 暂停后过一会儿再播，进度条跳了一截）。只有真在播时才推 ts。
                if g.get("playing"):
                    g["ts"] = now
                dirty = True
            except Exception:
                pass

        if "playing" in patch:
            # v4.01：非法值（"yes"/"abc"/None/{}…）**整位忽略**，不猜成 True。
            # 见 _g_playing() 的注释：这里以前 bool("yes")==True，一条「非法输入不崩」
            # 的测试用例就能把线上组播改成正在播，电脑端和音箱一起被拉起来。
            _okp, want = _g_playing(patch["playing"])
            if _okp and want != bool(g.get("playing")):
                if not want:
                    g["pos"] = _g_live_pos(g)      # 暂停：先把进度钉住再停表
                g["ts"] = now
                g["playing"] = want
                dirty = True

        # 播放模式（v4.22）：音箱出声时两端必须同模式；独自输出各管各的
        # （由前端决定传不传）。合法值 seq/all/one/rnd，非法整条忽略。
        if "mode" in patch:
            mv = str(patch.get("mode") or "").strip().lower()
            if mv in ("seq", "all", "one", "rnd") and mv != str(g.get("mode") or ""):
                g["mode"] = mv
                dirty = True

        if dirty:
            g["rev"] = int(g.get("rev") or 0) + 1
            g["by"] = (str(by or "") or "?")[:8]
            _g_cache = dict(g)
            _g_save(g)
            # 一行一条，方便事后查「这次暂停是谁给按回去的」。
            # 只记写请求（前端 1~2 秒才发一条），量很小。
            # JY-GROUPLOG：补 name，否则事后翻日志分不清这次 rev 是哪首歌的。
            try:
                keys = ",".join(sorted(patch.keys()))
                log("group rev=%s by=%s name=%s playing=%s pos=%.2f [%s]"
                    % (g["rev"], g["by"], (g.get("name") or ""),
                       g["playing"], float(g.get("pos") or 0), keys))
            except Exception:
                pass
        return g


def group_snapshot(g=None):
    g = g or read_group()
    snap = {
        "ok": True,
        "rev": int(g.get("rev") or 0),
        "name": g.get("name") or "",
        "playing": bool(g.get("playing")),
        "anchorPos": round(float(g.get("pos") or 0), 3),
        "anchorTs": int(g.get("ts") or 0),
        "pos": _g_live_pos(g),
        "outs": list(g.get("outs") or []),
        "vol": dict(g.get("vol") or {}),
        "delay": dict(g.get("delay") or {}),
        "pan": dict(g.get("pan") or {}),
        "part": dict(g.get("part") or {}),
        "eq": dict(g.get("eq") or {}),
        "by": g.get("by") or "",
        "mode": g.get("mode") or "",
        "now": group_ms(),
        # 轴自动重锚的状态：d 是「音箱真实位置 − 轴」，rt 是读一次音箱的往返
        "axis": {"d": AXIS["d"], "n": AXIS["n"], "rt": AXIS["rt"],
                 "err": AXIS["err"], "tol": AXIS["tol"], "soft": AXIS["soft"]},
        # 电视歌词屏的跟轴情况（前端只读，用来显示「差 0.2s」这类角标）
        "tvf": {"on": bool(TVF["on"]), "est": round(TVF["est"], 2),
                "d": round(TVF["d"], 2), "video": bool(TVF["video"]),
                "seeks": TVF["seeks"]},
    }
    return snap


# ---------------------------------------------------------------- 组播轴自动重锚
# 轴是「锚点 + 外推」：pos 是某一刻的进度，之后按墙钟往前推。锚点只在有人写
# pos（换歌 / 跳进度 / 校准）时才移动 —— 中间没有任何东西校准它。于是：
#   · 读位置用 /getplayerstatus 要 270~350ms，锚下去的 pos 天然就是「过去时」，
#     整条轴从此比真值慢 0.3 秒，而且再也不会自我修正；
#   · 两台机器的时钟差、音箱自己缓冲一下，也全都会留在轴上。
# 电脑 / 手机 / 电视全都拿这条轴当对齐基准（电脑实测因此稳定落后音箱 0.34 秒，
# 它自己的 gpDrift 又只对着轴纠，所以永远看不出来）。
# 由服务端每 1.2 秒拿音箱的真实 offset 校一次 —— 它就在内网，
# /playingmusic 往返只有 15ms，offset 还是微秒级浮点。
#
# 三条硬规矩：
#   1) 只重锚**小偏差**（≤ soft）。偏差大说明音箱真的走偏了（缓冲 / 单曲循环 /
#      被谁拖过进度），这时候把轴拽过去等于让所有设备跟着一起错 ——
#      该由前端的 surBoxSync 把音箱 seek 回轴上来。
#   2) 重锚**不涨 rev**。涨了前端会当成「换歌 / 跳进度」去 gpFollow，那才会把
#      正在听的歌甩走。前端每 1.2 秒 gpPull 一次，会自然吃到新锚点。
#   3) 只在真的需要时才落盘，别每 1.2 秒写一次 SD 卡。
AXIS = {"soft": 0.45, "tol": 0.06, "n": 0, "d": 0.0, "ts": 0.0, "err": "", "rt": 0,
        "big_since": 0.0, "wait": 8.0}
AXIS_TICK = 1.2


def axis_box_pos(name):
    """问音箱此刻真实进度。返回 (pos_now, rt_ms)；问不到就给 (None, 0)。

    pos_now 已经把「请求发出到收到」这半个往返补回去了 —— 也就是把 offset
    从「那一刻」挪到「现在」。不补的话每个锚点都天生慢半拍。
    """
    did = xm_mi_did()
    if not did:
        return None, 0
    t0 = time.time()
    try:
        d = json.loads(_http_get(BACKEND_API + "/playingmusic?did="
                                 + urllib.parse.quote(did), timeout=3))
    except Exception:
        return None, 0
    rt = int((time.time() - t0) * 1000)
    if not isinstance(d, dict) or d.get("ret") != "OK" or not d.get("is_playing"):
        return None, 0
    # 换歌途中它报的还是上一首，拿它锚等于把整条轴扔到上一首的位置上
    if name and (d.get("cur_music") or "") != name:
        return None, 0
    try:
        off = float(d.get("offset") or 0)
        dur = float(d.get("duration") or 0)
    except Exception:
        return None, 0
    if off <= 0 or (dur > 0 and off > dur + 3):
        return None, 0
    return off + rt / 2000.0, rt


def axis_step():
    """一拍：把轴按音箱真实位置校一次。"""
    global _g_cache
    g = read_group()
    if not g.get("playing"):
        return
    if "box" not in (g.get("outs") or []):
        return
    name = g.get("name") or ""
    if not name:
        return
    p, rt = axis_box_pos(name)
    if p is None:
        return
    AXIS["rt"] = rt
    cur = _g_live_pos(g)
    d = p - cur
    AXIS["d"] = round(d, 3)
    if abs(d) < AXIS["tol"]:
        AXIS["err"] = ""
        AXIS["big_since"] = 0.0
        return
    if abs(d) > AXIS["soft"]:
        # 大偏差：音箱自己跑偏了（缓冲 / 单曲循环 / 被谁拖过进度）。
        # 不立刻拽轴 —— 拽了等于让所有设备跟着一起错，该由前端的 surBoxSync
        # 把音箱 seek 回轴上来。但**要有耐心上限**：没人开电脑端的时候没人纠音箱，
        # 这时候轴再犟下去就永远错着了，所以等够 8 秒还没人管，就认音箱为准。
        now = time.time()
        if not AXIS["big_since"]:
            AXIS["big_since"] = now
        if now - AXIS["big_since"] < AXIS["wait"]:
            AXIS["err"] = "big %+.2f (%.0fs)" % (d, now - AXIS["big_since"])
            return
        AXIS["err"] = "big %+.2f 认了" % d
    else:
        AXIS["err"] = ""
    AXIS["big_since"] = 0.0
    with _g_lock:
        g2 = read_group()
        # 读音箱位置那一下要发一次 HTTP（10~300ms，超时能到 3 秒）。这段时间里
        # 用户很可能正好按了暂停 —— 原来这里无条件写 playing=True，等于把刚按下
        # 的暂停又按回「在播」，前端下一次 gpPull 就跟着续播 + seek 回轴：
        # 表现就是「暂停后进度条自己跳，还自己唱起来」。所以落盘前必须重新确认。
        if not g2.get("playing"):
            AXIS["err"] = "skip paused"
            log("axis skip: 读音箱位置期间被暂停，保持暂停")
            return
        if (g2.get("name") or "") != name:
            AXIS["err"] = "skip renamed"
            log("axis skip: 期间换歌 %s -> %s" % (name, g2.get("name")))
            return
        g2["pos"] = max(0.0, p)
        g2["ts"] = group_ms()
        g2["playing"] = True
        _g_cache = dict(g2)
        _g_save(g2)
    AXIS["n"] += 1
    AXIS["ts"] = time.time()
    AXIS["err"] = ""


def axis_worker():
    while True:
        try:
            axis_step()
        except Exception as e:
            AXIS["err"] = "exc %s" % e
        time.sleep(AXIS_TICK)


# ---------------------------------------------------------------- 电视跟轴（歌词屏）
# 电视是 DLNA 渲染器：投过去之后它自己从头拉流，协议里没有「同步」这个字段。
# 实测（某品牌电视 / hyperDLNA）「命令发出 → 真的出声」固定慢 3.5 秒左右，
# 而且它一旦和组播音乐各走各的，就永远差这 3.5 秒，越听越飘。
# 唯一能做的：反复量它的 RelativeTimePosition，跟组播时间轴比，偏了就 Seek 拉回来。
#
# 两个现实约束：
#   1) DLNA 的位置只报到「整秒」，单次读数天然有 ±1 秒噪声 → 多采几次取中位数，
#      再加 0.5 秒做无偏化（回报值是 floor，期望偏差是 -0.5）。
#   2) Seek 在 dlna.py 里是按整秒下发的，所以最终精度就是 ±0.5 秒。
#      歌词屏在乎的是「别落后」，所以目标定成「比轴早 0.25 秒」，宁可略领先。
#
# 电视只当歌词屏（用户要求），跟轴的同时把它的音量压到 0，撤的时候还回去。
TVF = {
    "on": False, "rid": "", "name": "", "video": False, "mute": True,
    "vol0": 0, "vol_ts": 0.0, "d": 0.0, "est": 0.0, "seeks": 0, "ts": 0.0,
    "pos": 0.0, "dur": 0.0, "playing": False, "err": "", "samples": [],
    "cast_ts": 0.0, "retries": 0,
}
# 电视音量被我们压到 0 的原值要落盘：服务重启/崩溃后不能把人家电视留在静音
TVF_FILE = os.path.join(CONF_DIR, "tvf.json")
TVF_TOL = 0.65      # 估计偏差超过它就纠（含整秒噪声的余量）
TVF_BIAS = 0.25     # 正数 = 让歌词比声音早一点，读起来更自然
TVF_LEAD = 0.45     # 二次读数 + 下发命令的补偿
TVF_COOL = 6.0      # 纠完歇 6 秒：DLNA seek 之后要重新缓冲，紧接着再纠只会越纠越乱
TVF_STUCK = 3.0     # 位置超过这么久没前进 = 在缓冲，别信读数
TVF_TICK = 0.6
_tvf_next_adopt = 0.0


def lan_ip():
    """本机在局域网里的地址（电视要能直接拉这个地址上的 mp4）"""
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("192.168.1.1", 9))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return "127.0.0.1"


def xm_post(path, obj, timeout=8):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(BACKEND_API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))


def tv_status(rid):
    """读一台 DLNA 渲染器的状态；拿不到返回 None"""
    try:
        d = json.loads(_http_get(BACKEND_API + "/dlna/status?renderer="
                                 + urllib.parse.quote(rid), timeout=4))
    except Exception as e:
        TVF["err"] = "status %r" % (e,)
        return None
    if not isinstance(d, dict) or d.get("ret") != "OK":
        TVF["err"] = "status %s" % ((d or {}).get("ret") if isinstance(d, dict) else "-")
        return None
    return d


def tv_renderers():
    """DLNA 渲染器列表（xiaomusic 内部缓存 60 秒，这里不会每次 SSDP）"""
    try:
        d = json.loads(_http_get(BACKEND_API + "/dlna/renderers", timeout=6))
        return d.get("list") or []
    except Exception:
        return []


def tvf_snapshot():
    g = read_group()
    return {
        "ok": True, "on": bool(TVF["on"]), "rid": TVF["rid"], "name": TVF["name"],
        "video": bool(TVF["video"]), "mute": bool(TVF["mute"]),
        "d": round(TVF["d"], 2), "est": round(TVF["est"], 2),
        "pos": round(TVF["pos"], 1), "dur": round(TVF["dur"], 1),
        "playing": bool(TVF["playing"]), "seeks": TVF["seeks"], "err": TVF["err"],
        "axis": _g_live_pos(g), "gn": g.get("name") or "",
        "tol": TVF_TOL, "bias": TVF_BIAS,
    }


def _tvf_saved():
    try:
        with open(TVF_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {"rid": str(d.get("rid") or ""), "vol": float(d.get("vol") or 0)}
    except Exception:
        return {"rid": "", "vol": 0.0}


def _tvf_save(rid, vol):
    try:
        tmp = TVF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rid": rid, "vol": vol}, f)
        os.replace(tmp, TVF_FILE)
    except Exception:
        pass


def _tvf_forget():
    try:
        os.remove(TVF_FILE)
    except Exception:
        pass


def tvf_mute():
    """跟轴期间把电视压到 0：它只负责画面，声音交给音箱/电脑"""
    rid = TVF["rid"]
    if not rid or not TVF["mute"]:
        return
    now = time.time()
    if now - TVF["vol_ts"] < 15:          # 15 秒最多压一次，别刷接口
        return
    TVF["vol_ts"] = now
    st = tv_status(rid) or {}
    try:
        cur = int(float(st.get("volume") or 0))
    except Exception:
        cur = -1
    if 0 < cur <= 100:
        TVF["vol0"] = cur                  # 记住电视原本的音量，撤的时候还回去
        _tvf_save(rid, cur)               # 落盘：服务重启也要能还回去
    if cur == 0:
        return
    try:
        r = xm_post("/dlna/cmd", {"action": "volume", "value": 0, "renderer": rid}, timeout=5)
        log("tvf mute rid=%s was=%s -> %s" % (rid, cur, (r or {}).get("ret")))
    except Exception as e:
        TVF["err"] = "mute %r" % (e,)


def tvf_unmute():
    rid, v = TVF["rid"], TVF["vol0"]
    if not (0 < v <= 100):
        sv = _tvf_saved()                 # 进程重启过，内存里没值了 → 用落盘那份
        if sv["vol"] > 0:
            rid, v = (rid or sv["rid"]), sv["vol"]
    if not rid or not (0 < v <= 100):
        return
    try:
        xm_post("/dlna/cmd", {"action": "volume", "value": int(v), "renderer": rid}, timeout=5)
        log("tvf unmute rid=%s -> %d" % (rid, int(v)))
        _tvf_forget()
    except Exception:
        pass


def tvf_set(on, rid="", name="", video=None, mute=True):
    was = TVF["on"]
    if on:
        TVF["on"] = True
        if rid:
            TVF["rid"] = rid
        if name:
            TVF["name"] = name
        if video is not None:
            TVF["video"] = bool(video)
        TVF["mute"] = bool(mute)
        TVF["samples"] = []
        TVF["ts"] = 0.0                    # 允许立刻纠一次
        TVF["vol_ts"] = 0.0
        TVF["_lp"], TVF["_lpt"] = -1.0, time.time()
        if not was:
            log("tvf on rid=%s name=%r video=%s" % (TVF["rid"], TVF["name"], TVF["video"]))
    else:
        TVF["on"] = False
        TVF["samples"] = []
        TVF["off_until"] = time.time() + 240      # 4 分钟内别再自动接管这台
        if was:
            log("tvf off")
        tvf_unmute()
    return tvf_snapshot()


def _tvf_ready_url(name):
    """当前这首歌歌词画面的地址（没烧好返回空）。缓存键里带歌词指纹，
    所以歌词一改这里就是新地址。"""
    st = lyric_studio()
    if st is None or not name or not st.ready(name):
        return ""
    try:
        return "http://%s:%d/lvideo/%s.mp4" % (lan_ip(), PORT, st.key(name))
    except Exception:
        return ""


def tvf_cast(name, rid):
    """把一首歌投到电视：有歌词画面就投画面，没有就投纯音频。返回是否成功"""
    url = _tvf_ready_url(name)
    has_video = bool(url)
    artist, title = display_meta(name)
    payload = {
        "name": name, "renderer": rid, "title": title, "artist": artist,
        "duration": song_dur_of(name) or 0,
    }
    if has_video:
        payload["video_url"] = url
    else:
        payload["cover"] = "http://%s:58090/dlna/cover?name=%s" % (lan_ip(), urllib.parse.quote(name))
    try:
        r = xm_post("/dlna/cast", payload, timeout=25)
    except Exception as e:
        TVF["err"] = "cast %r" % (e,)
        return False
    if not isinstance(r, dict) or r.get("ret") != "OK":
        TVF["err"] = "cast %s" % ((r or {}).get("ret") if isinstance(r, dict) else "-",)
        return False
    TVF.update({"video": has_video, "name": name, "samples": [], "ts": 0.0,
                "pos": 0.0, "dur": 0.0, "vol_ts": 0.0, "err": "", "url": url,
                "_lp": -1.0, "_lpt": time.time(),
                "cast_ts": time.time(), "retries": 0, "idle": 0})
    if has_video:
        tvf_unmute()                      # 画面版是静音的，之前压过的音量可以还回去了
    log("tvf cast %r rid=%s video=%s" % (name, rid, has_video))
    return True


def _tvf_try_upgrade():
    """投的时候歌词画面还没烧好（先投了纯音频）→ 烧好了就地换成画面版"""
    name = TVF["name"]
    st = lyric_studio()
    if st is None or not name:
        TVF["video"] = True                # 没有生成器，别再试
        return False
    if st.known_none(name):
        TVF["video"] = True                # 这首本来就没歌词
        return False
    if not st.ready(name):
        try:
            st.request(name, 3)
        except Exception:
            pass
        return False
    return tvf_cast(name, TVF["rid"])


def _tvf_adopt(g):
    """没人显式接管也干活：发现某台电视正在放组播这首 → 自动接管。
    只在「组播里还有别的输出（音箱/电脑）」时才接管，不然轴就是电视自己，纠它没意义。"""
    global _tvf_next_adopt
    name = g.get("name") or ""
    if not name or not g.get("playing"):
        return
    if not [x for x in (g.get("outs") or []) if x != "tv"]:
        return
    if _tvf_next_adopt > time.time():
        return
    if TVF.get("off_until", 0) > time.time():
        return                            # 刚被明确关掉，别自作主张再接回来
    _tvf_next_adopt = time.time() + 6
    gdur = song_dur_of(name) or 0
    for r in tv_renderers():
        rid = r.get("id") or ""
        if not rid:
            continue
        st = tv_status(rid)
        if not st or not st.get("playing"):
            continue
        sdur = float(st.get("dur") or 0)
        # 时长对得上才认（防止把用户正在看的电影当成歌词屏）
        if not (gdur > 20 and sdur > 20 and abs(sdur - gdur) <= 6):
            continue
        TVF.update({"on": True, "rid": rid, "name": name, "video": True,
                    "samples": [], "ts": 0.0})
        log("tvf adopt %r rid=%s dur=%.0f" % (r.get("name"), rid, sdur))
        return


def _tvf_ref(g):
    """对齐基准。返回 (pos, ok)。

    优先用音箱的**真实进度**：组播轴是「锚点 + 外推」算出来的，音箱自己停了、
    循环重放了、或者播放器进度和轴对不上时，轴会停在几分钟后不动（踩过好几次）。
    电视要跟的是「耳朵现在听到的那一声」，所以直接问音箱。
    音箱不在输出里（或没在放当前这首）才退回组播轴。
    """
    outs = g.get("outs") or []
    name = g.get("name") or ""
    if "box" in outs and name:
        did = xm_mi_did()
        if did:
            try:
                d = json.loads(_http_get(BACKEND_API + "/playingmusic?did="
                                         + urllib.parse.quote(did), timeout=6))
            except Exception:
                # 问不到（编码时 CPU 打满、接口超时）→ 这一拍跳过，别下结论
                return 0.0, False, False
            if not isinstance(d, dict) or d.get("ret") != "OK":
                return 0.0, False, False
            if d.get("is_playing") and (d.get("cur_music") or "") == name:
                off = float(d.get("offset") or 0)
                dur = float(d.get("duration") or 0)
                if 0 < off <= (dur + 3 if dur > 0 else 1e9):
                    return off, True, False
            return 0.0, False, True   # 明确：音箱没在放这首
    return _g_live_pos(g), True, False


def tvf_step():
    g = read_group()
    if not TVF["on"] or not TVF["rid"]:
        return _tvf_adopt(g)
    rid = TVF["rid"]

    # 只有「退化成纯音频」时才压电视音量（歌词视频本身是静音的，不用动它）
    if TVF["mute"] and not TVF["video"]:
        tvf_mute()

    st = tv_status(rid)
    if st is None:
        return
    TVF["pos"] = float(st.get("pos") or 0)
    TVF["dur"] = float(st.get("dur") or 0)
    TVF["playing"] = bool(st.get("playing"))

    if not g.get("playing") or not g.get("name"):
        # 组播停了：歌词屏也别一直滚下去。停够 20 秒就收工（同时把电视音量还原）
        TVF["idle"] = TVF.get("idle", 0) + 1
        if TVF["idle"] * TVF_TICK > 20:
            log("tvf: 组播停太久，收工")
            try:
                xm_post("/dlna/cmd", {"action": "stop", "renderer": rid}, timeout=6)
            except Exception:
                pass
            tvf_set(False)
        return
    TVF["idle"] = 0
    if not [x for x in (g.get("outs") or []) if x != "tv"]:
        return                             # 只有电视在放：轴就是它自己

    # 投出去但电视没起来（DLNA 偶发「命令回 OK、画面不动」）→ 12 秒后重投一次
    if TVF["name"] and TVF["name"] == g["name"] and TVF["pos"] <= 0 \
            and not TVF["playing"] and TVF.get("cast_ts"):
        waited = time.time() - TVF["cast_ts"]
        if waited > 12 and TVF.get("retries", 0) < 1:
            TVF["retries"] = TVF.get("retries", 0) + 1
            log("tvf retry: 投出去 %.0fs 还没起来，重投一次" % waited)
            tvf_cast(TVF["name"], rid)
        elif waited > 30:
            TVF["err"] = "电视没反应（投屏命令成功但画面没起来）"
        return

    # 换歌：电视还在放上一首 → 重投。但它要是已经被切去看别的东西，就别抢
    if TVF["name"] and TVF["name"] != g["name"]:
        pdur = song_dur_of(TVF["name"]) or 0
        if TVF["dur"] > 20 and pdur > 20 and abs(TVF["dur"] - pdur) > 8:
            TVF["on"] = False
            log("tvf drop: 电视上已经不是这首歌了")
            return
        tvf_cast(g["name"], rid)
        return

    # 投的是纯音频（画面没烧好）→ 烧好后直接换成画面版
    if not TVF["video"]:
        if _tvf_try_upgrade():
            return
    else:
        # 画面文件换了（歌词补了译文、排版改过 → 缓存键变了，歌名却没变）：
        # 电视还挂着旧的那份，重投一次
        u = _tvf_ready_url(g["name"])
        if u and TVF.get("url") and u != TVF["url"]:
            log("tvf: 歌词画面换过了，重投")
            tvf_cast(g["name"], rid)
            return

    if not TVF["playing"] or TVF["pos"] <= 0:
        return                              # 还没真的开始走，先等（别 seek 未起播的流）

    # 位置没前进 = 正在缓冲（seek 之后常见）。这段读数不可信，
    # 拿它去算偏差只会一路 seek 下去，电视被搅得反复卡顿。
    now_t = time.time()
    lp = TVF.get("_lp", -1.0)
    if TVF["pos"] != lp:
        TVF["_lp"], TVF["_lpt"] = TVF["pos"], now_t
    if now_t - TVF.get("_lpt", 0.0) > TVF_STUCK:
        TVF["samples"] = []
        TVF["err"] = "电视缓冲中"
        return

    t0 = time.time()
    ref, ok, definite = _tvf_ref(g)
    if not ok:
        if not definite:
            TVF["err"] = "读音箱进度失败（稍后重试）"
            return
        # 音箱明确没在放这首：组播那面「playing」旗标常年过期，不能只信它。
        # 连续 40 秒这样才收工（偶尔一次切歌/缓冲不算）。
        TVF["err"] = "音箱没在放这首"
        TVF["idle"] = TVF.get("idle", 0) + 1
        if TVF["idle"] * TVF_TICK > 40:
            log("tvf: 音箱一直没在放，收工")
            try:
                xm_post("/dlna/cmd", {"action": "stop", "renderer": rid}, timeout=6)
            except Exception:
                pass
            tvf_set(False)
        return
    TVF["idle"] = 0
    # 量测本身有延迟：电视位置是 t0 前后读到的，基准读到的是 t0+delta 的，
    # 两边各让一半，剩下的交给 DLNA 整秒精度的余量
    ref += (time.time() - t0) * 0.5

    d = TVF["pos"] - ref
    TVF["d"] = d

    samp = TVF["samples"]
    samp.append(d)
    if len(samp) > 5:
        del samp[0]
    if len(samp) < 2:
        return
    est = sorted(samp)[len(samp) // 2] + 0.5      # 回报是 floor，+0.5 无偏
    TVF["est"] = est
    # 面板上「电视 · 延迟」那个滑杆当微调旋钮用：数值越大电视越靠后（歌词越晚）。
    # 和默认的「歌词略早一点」抵消之后就是最终目标。
    try:
        bias = TVF_BIAS - float((g.get("delay") or {}).get("tv") or 0) / 1000.0
    except Exception:
        bias = TVF_BIAS
    bias = max(-1.5, min(1.5, bias))
    if abs(est - bias) <= TVF_TOL:
        return
    now = time.time()
    if now - TVF["ts"] < TVF_COOL:
        return
    TVF["ts"] = now
    TVF["seeks"] += 1
    TVF["samples"] = []
    # 目标：基准在此刻往后 lead 的位置（命令下发到电视真正跳过去需要时间）
    tgt = max(0.0, ref + (now - t0) + TVF_LEAD + bias)
    try:
        xm_post("/dlna/cmd", {"action": "seek", "value": round(tgt, 1), "renderer": rid}, timeout=6)
        log("tvf seek -> %.1f (est %.2f ref %.2f)" % (tgt, est, ref))
    except Exception as e:
        TVF["err"] = "seek %r" % (e,)


def _tvf_boot():
    """上一轮回合被 kill 的话，电视可能还停在音量 0 —— 起来先还回去"""
    time.sleep(12)
    try:
        sv = _tvf_saved()
        if sv["vol"] > 0 and not TVF["on"]:
            TVF["rid"] = TVF["rid"] or sv["rid"]
            TVF["vol0"] = sv["vol"]
            tvf_unmute()
            log("tvf boot: 还原电视音量 %d" % int(sv["vol"]))
    except Exception:
        pass


def tvf_worker():
    while True:
        try:
            tvf_step()
        except Exception as e:
            log("tvf step fail %r" % (e,))
        time.sleep(TVF_TICK if TVF["on"] else 3.0)


# ---------------------------------------------------------------- 分轨：人声 / 伴奏
# 纯 ffmpeg 矩阵，不依赖模型：
#   人声 = 中置(Mid)  (L+R)/2 再过 180Hz 高通（把鼓和贝斯这种居中低频削掉）
#   伴奏 = 侧声道(Side) (L−R) 再把 Mid 里 160Hz 以下的低频补回来（不然整首歌会飘）
# 一首 4 分钟的歌在 arm64 上约 8 秒，结果按歌名缓存，第二次直接命中。
# 两条轨和源文件等长（mp3 头尾 padding 只有几十毫秒），所以组播那条时间轴
# 不用改，现有的 delay / 起播对齐照旧能用。
STEM_DIR = "/sdcard/.jystem"
STEM_PARTS = ("vocal", "instr")
PART_KEYS = ("full", "vocal", "instr")
PART_LABEL = {"full": "完整", "vocal": "人声", "instr": "伴奏"}
STEM_BITRATE = "192k"
STEM_MAX_DIRS = 240          # 缓存上限（约 2.5GB），超了按最后访问时间淘汰

_STEM_FILTER = (
    "[0:a]aformat=channel_layouts=stereo,asplit=3[a][b][c];"
    "[a]pan=mono|c0=0.5*c0+0.5*c1,highpass=f=180,pan=stereo|c0=c0|c1=c0[voc];"
    "[b]pan=stereo|c0=0.5*c0-0.5*c1|c1=0.5*c1-0.5*c0[sd];"
    "[c]pan=mono|c0=0.5*c0+0.5*c1,lowpass=f=160[lw];"
    "[sd][lw]amix=inputs=2:normalize=0[ins]"
)

_stem_lock = threading.Lock()
_stem_queue = []             # [(name, key)]
_stem_wait = set()           # 排队中 + 正在跑
_stem_pct = {}               # key -> 0..100
_stem_err = {}
_stem_thread_on = False


def stem_key(name):
    return hashlib.md5(("stem1|" + str(name or "")).encode("utf-8")).hexdigest()[:12]


def stem_url(key, part, host):
    return "http://%s:58091/stem/%s/%s.mp3" % (host, key, part)


def stem_dir_of(key):
    # 只认自己生成的 hex key，挡掉 ../ 这类路径穿越
    if not re.match(r"^[0-9a-f]{12}$", str(key or "")):
        return None
    return os.path.join(STEM_DIR, key)


def stem_status(name):
    key = stem_key(name)
    d = os.path.join(STEM_DIR, key)
    info = {"ok": True, "name": name, "key": key, "ready": False,
            "parts": {}, "pct": int(_stem_pct.get(key) or 0),
            "building": key in _stem_wait, "err": _stem_err.get(key) or "", "dur": 0.0}
    meta = os.path.join(d, "ok.json")
    if os.path.exists(meta):
        try:
            with open(meta, encoding="utf-8") as f:
                info["dur"] = float((json.load(f) or {}).get("dur") or 0)
        except Exception:
            pass
    ok = 0
    for p in STEM_PARTS:
        f = os.path.join(d, p + ".mp3")
        got = os.path.exists(f) and os.path.getsize(f) > 2048
        info["parts"][p] = got
        if got:
            ok += 1
    info["ready"] = ok == len(STEM_PARTS)
    if info["ready"]:
        info["pct"] = 100
        try:
            os.utime(d, None)          # 命中就摸一下，给淘汰用
        except Exception:
            pass
    return info


def _stem_start():
    """起一条串行 worker：一次只跑一首，避免 8 核被打满、把正在放的歌卡住"""
    global _stem_thread_on
    with _stem_lock:
        if _stem_thread_on:
            return
        _stem_thread_on = True
    threading.Thread(target=_stem_worker, daemon=True).start()


def _stem_worker():
    while True:
        job = None
        with _stem_lock:
            if _stem_queue:
                job = _stem_queue.pop(0)
        if not job:
            time.sleep(0.6)
            continue
        try:
            _stem_build(job[0], job[1])
        except Exception as e:
            _stem_err[job[1]] = "分轨失败：%r" % (e,)
            sys.stderr.write("[stem] %r\n" % (e,))
        finally:
            with _stem_lock:
                _stem_wait.discard(job[1])
            _stem_gc()


def stem_ensure(name):
    """幂等：没生成过就排队；已经在跑就只回报进度"""
    info = stem_status(name)
    if info["ready"]:
        return info
    key = info["key"]
    with _stem_lock:
        if key not in _stem_wait:
            _stem_wait.add(key)
            _stem_queue.append((name, key))
            _stem_pct[key] = 1
    _stem_start()
    return stem_status(name)


def _probe_dur(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, timeout=30)
        return float((r.stdout or b"0").decode("utf-8", "ignore").strip() or 0)
    except Exception:
        return 0.0


def _stem_build(name, key):
    d = os.path.join(STEM_DIR, key)
    os.makedirs(d, exist_ok=True)
    src = audio_path_of(name)
    if not src or not os.path.exists(src):
        _stem_err[key] = "本地没找到这首歌的文件"
        _stem_pct[key] = 0
        return
    dur = float(song_dur_of(name) or 0) or _probe_dur(src)
    outs = {p: os.path.join(d, p + ".mp3") for p in STEM_PARTS}
    tmp = {p: outs[p] + ".part" for p in STEM_PARTS}
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", "-y", "-i", src,
           "-filter_complex", _STEM_FILTER,
           "-map", "[voc]", "-c:a", "libmp3lame", "-b:a", STEM_BITRATE, "-ar", "44100", "-f", "mp3", tmp["vocal"],
           "-map", "[ins]", "-c:a", "libmp3lame", "-b:a", STEM_BITRATE, "-ar", "44100", "-f", "mp3", tmp["instr"]]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for line in p.stdout:
            s = line.decode("utf-8", "ignore").strip()
            if s.startswith("out_time_us=") and dur > 0:
                try:
                    pct = int(int(s.split("=", 1)[1]) / 1e6 / dur * 100)
                except Exception:
                    continue
                _stem_pct[key] = max(1, min(99, pct))
        err = (p.stderr.read() or b"").decode("utf-8", "ignore")
    finally:
        try:
            p.stdout.close(); p.stderr.close()
        except Exception:
            pass
    rc = p.wait()
    if rc != 0 or not all(os.path.exists(t) for t in tmp.values()):
        _stem_err[key] = (err.strip()[-300:] or ("ffmpeg 退出码 %d" % rc))
        _stem_pct[key] = 0
        for t in tmp.values():
            try:
                os.remove(t)
            except Exception:
                pass
        return
    for k in STEM_PARTS:
        os.replace(tmp[k], outs[k])
    try:
        with open(os.path.join(d, "ok.json"), "w", encoding="utf-8") as f:
            json.dump({"name": name, "dur": dur, "src": os.path.basename(src),
                       "ts": int(time.time())}, f, ensure_ascii=False)
    except Exception:
        pass
    _stem_pct[key] = 100
    _stem_err.pop(key, None)


def _stem_gc():
    """缓存太大就按最后访问时间淘汰最旧的（正在排队的不动）"""
    try:
        dirs = [os.path.join(STEM_DIR, x) for x in os.listdir(STEM_DIR)]
        dirs = [x for x in dirs if os.path.isdir(x)]
        if len(dirs) <= STEM_MAX_DIRS:
            return
        dirs.sort(key=lambda x: os.path.getmtime(x))
        with _stem_lock:
            busy = set(_stem_wait)
        for x in dirs[:max(0, len(dirs) - STEM_MAX_DIRS)]:
            if os.path.basename(x) in busy:
                continue
            shutil.rmtree(x, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 删除歌曲（回收站）
# 删歌不直接销毁文件：先移进 /sdcard/.jytrash 下的独立目录并记一份 meta，
# 之后可以「恢复」，只有用户明确「彻底删除」才真正抹掉。
TRASH_DIR = "/sdcard/.jytrash"
_AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ape", ".ogg", ".aac")


def resolve_song_rel(name=None, rel=None):
    """把前端传的歌名/相对路径解析成 MUSIC_DIR 内的相对路径；不合法返回 (None, 原因)"""
    cand = (rel or "").strip() or None
    if not cand:
        found = find_song_file_fuzzy(name or "")
        if not found:
            return None, "曲库里找不到这首歌的文件"
        cand = found
    # 目录穿越/绝对路径一律拒绝
    cand = cand.replace("\\", "/").lstrip("/")
    parts = [p for p in cand.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None, "路径不合法"
    abs_path = os.path.join(MUSIC_DIR, *parts)
    try:
        real_base = os.path.realpath(MUSIC_DIR)
        real_path = os.path.realpath(abs_path)
    except Exception:
        return None, "路径解析失败"
    if real_path != real_base and not real_path.startswith(real_base + os.sep):
        return None, "路径超出曲库范围"
    if not os.path.isfile(abs_path):
        return None, "文件不存在（可能已被移动或删除）"
    if not os.path.basename(abs_path).lower().endswith(_AUDIO_EXTS):
        return None, "不是音频文件"
    return "/".join(parts), None


# ---------------------------------------------------------------- 歌曲元信息（时长 / 大小 / 专辑）
# 列表要显示「专辑 / 时长 / 大小」三列，文件名里没有这些信息，只能读文件头。
# mutagen 只读头部，几十毫秒一首；再按 (mtime,size) 缓存到 jy_meta.json，之后走内存。
META_FILE = os.path.join(CONF_DIR, "jy_meta.json")
_meta_cache = {}
_meta_lock = threading.Lock()


def load_meta():
    global _meta_cache
    try:
        with open(META_FILE, encoding="utf-8") as f:
            _meta_cache = json.load(f)
    except Exception:
        _meta_cache = {}


def save_meta():
    try:
        tmp = META_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_meta_cache, f, ensure_ascii=False)
        os.replace(tmp, META_FILE)
    except Exception as e:
        log("save_meta failed: %r" % (e,))


def read_tags(abs_path):
    """尽力而为地读标签；没装 mutagen / 文件损坏都安静地返回空"""
    out = {"dur": 0.0, "album": "", "artist": "", "title": "", "bit": 0}
    try:
        import mutagen
        m = mutagen.File(abs_path, easy=True)
        if m is None:
            return out
        info = getattr(m, "info", None)
        if info is not None:
            ln = getattr(info, "length", 0) or 0
            if ln:
                out["dur"] = round(float(ln), 1)
            br = getattr(info, "bitrate", 0) or 0
            if br:
                out["bit"] = int(br / 1000)

        def tag(k):
            try:
                v = m.get(k) or []
                return str(v[0]).strip() if v else ""
            except Exception:
                return ""
        out["album"] = tag("album")
        out["artist"] = tag("artist")
        out["title"] = tag("title")
    except Exception:
        pass
    return out


_file_index = {"ts": 0.0, "map": {}}
_meta_miss = {}


def music_file_index():
    """一次 walk 建「歌名 → 相对路径」映射，60 秒内复用。
    /sdcard 是 FUSE，一首歌一次 walk 会让批量取元信息直接卡死。"""
    now = time.time()
    if _file_index["map"] and now - _file_index["ts"] < 60:
        return _file_index["map"]
    m = {}
    for root, dirs, files in os.walk(MUSIC_DIR):
        dirs[:] = [d for d in dirs if d not in ("tmp", "cache", "download", ".thumbnails")]
        for fn in files:
            if not fn.lower().endswith(_AUDIO_EXTS):
                continue
            rel = os.path.relpath(os.path.join(root, fn), MUSIC_DIR).replace(os.sep, "/")
            if rel.split("/")[0] == os.path.basename(TRASH_DIR):
                continue
            m.setdefault(fn, rel)
            m.setdefault(os.path.splitext(fn)[0], rel)
    _file_index["map"] = m
    _file_index["ts"] = now
    return m


def lookup_rel(name):
    name = str(name or "").strip()
    if not name:
        return None
    idx = music_file_index()
    if name in idx:
        return idx[name]
    base = os.path.basename(name)
    if base in idx:
        return idx[base]
    for ext in _AUDIO_EXTS:
        if name + ext in idx:
            return idx[name + ext]
    hit = _meta_miss.get(name)
    if hit and time.time() - hit < 600:
        return None
    rel, _err = resolve_song_rel(name=name)   # 慢路径兜底（模糊匹配）
    if rel:
        return rel
    _meta_miss[name] = time.time()
    return None


def song_meta(name):
    rel = lookup_rel(name)
    if not rel:
        return None
    abs_path = os.path.join(MUSIC_DIR, *rel.split("/"))
    try:
        st = os.stat(abs_path)
    except Exception:
        return None
    with _meta_lock:
        hit = _meta_cache.get(name)
    if hit and hit.get("mtime") == int(st.st_mtime) and hit.get("size") == st.st_size:
        return hit
    row = {"name": name, "mtime": int(st.st_mtime), "size": st.st_size,
           "fmt": os.path.splitext(rel)[1].lstrip(".").upper()}
    row.update(read_tags(abs_path))
    with _meta_lock:
        _meta_cache[name] = row
    return row


def audio_path_of(name):
    """展示名 → 本地音频绝对路径（给歌词视频编码用）"""
    rel = lookup_rel(name)
    if not rel:
        return None
    return os.path.join(MUSIC_DIR, *rel.split("/"))


# ---------------------------------------------------------------- JY-TAG 歌名/歌手自动匹配 + 高清封面
# 文件名靠不住：'01 Remember.flac' 里没有歌手，'05 Ex-Otogibanashi' 会把 'Ex' 当歌手，
# 而以 '（Cover 五月天）' 结尾的名字又不是正式曲名。内嵌标签才是权威，其次才看文件名。
COVER_HI_DIR = os.path.join(CACHE_DIR, "cover_hi")
os.makedirs(COVER_HI_DIR, exist_ok=True)
_hi_miss = {}
_hi_lock = threading.Lock()


def strip_track_no(s):
    """'01 Remember' -> 'Remember'（整张专辑拖进来时文件名常带曲目号）"""
    return re.sub(r"^\s*\d{1,3}\s*[\.\、\-–_]\s*", "", str(s or "")).strip()


def tidy_title(t):
    t = strip_track_no(t)
    t = re.sub(r"[\(\[（【][^\)\]）】]*cover[^\)\]）】]*[\)\]）】]", "", t, flags=re.I)
    t = re.sub(r"[\(\[（【](官方|高音质|无损|完整版)[^\)\]）】]*[\)\]）】]", "", t)
    return re.sub(r"\s+", " ", t).strip(" -_")


def cached_tags(name):
    with _meta_lock:
        return _meta_cache.get(name) or {}


def display_meta(name):
    """展示用的 (歌手, 歌名)。内嵌标签优先，没有才退回文件名解析。"""
    t = cached_tags(name)
    artist = str(t.get("artist") or "").strip()
    title = tidy_title(t.get("title") or "")
    fa, ft = split_title(name)
    fa = strip_track_no(fa)
    if not title:
        title = tidy_title(ft) or tidy_title(clean_query(name)) or name
    if not artist:
        # 文件名前半段要像个歌手才用：'05 Ex' 这种带曲目号的直接丢
        if fa and not re.match(r"^\d", fa) and len(fa) <= 24:
            artist = fa
    return artist, title


def read_cover(path):
    """从音频里抠内嵌封面（FLAC / ID3 / MP4 三种容器都覆盖），返回 (bytes, w, h)"""
    try:
        import io
        import mutagen
        from PIL import Image
        m = mutagen.File(path)
        if m is None:
            return b"", 0, 0
        data = b""
        pics = getattr(m, "pictures", None)          # FLAC / Ogg
        if pics:
            data = bytes(pics[0].data or b"")
        if not data:
            tags = getattr(m, "tags", None)
            if tags is not None:
                try:
                    apic = tags.getall("APIC")       # MP3 ID3
                    if apic:
                        data = bytes(apic[0].data or b"")
                except Exception:
                    pass
                if not data:
                    try:
                        covr = tags.get("covr")      # M4A
                        if covr:
                            data = bytes(covr[0])
                    except Exception:
                        pass
        if len(data) < 2000:
            return b"", 0, 0
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        return data, w, h
    except Exception:
        return b"", 0, 0


def hi_cover_path(name):
    # key 里去掉扩展名：/musiclist 给的是「08 Reply」，/api/local 给的是「08 Reply.mp3」，
    # 两种写法要落到同一张图上，不然会存两份、还互相找不到
    stem = re.sub(r"\.(mp3|flac|m4a|ogg|wav|ape|aac|aiff|wma)$", "", str(name or ""), flags=re.I)
    return os.path.join(COVER_HI_DIR, sanitize_filename(stem) + ".img")


def _bump_cover_meta(name, src, w, ncid=None):
    with _meta_lock:
        row = _meta_cache.setdefault(name, {})
        row["hi"] = src
        row["hiw"] = w
        if ncid:
            row["ncid"] = ncid




def nc_get(url, timeout=12):
    """第三方接口 HTTP 访问 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return b""


def _nz(s):
    s = str(s or "").lower()
    s = re.sub(r"[\(\[（【][^\)\]）】]*[\)\]）】]", "", s)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", "", s)


def _simi(a, b):
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 78
    try:
        import difflib
        return int(difflib.SequenceMatcher(None, a, b).ratio() * 66)
    except Exception:
        return 0


def _nc_query(artist, title):
    """第三方歌词检索 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return (None, 0)


def _nc_pick(name):
    """第三方歌词选曲 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return None


def _lrc_tl_map(tlrc):
    """翻译歌词 → {时间戳: 译文}"""
    d = {}
    for line in (tlrc or "").splitlines():
        sline = line.strip()
        if not sline.startswith("["):
            continue
        i = sline.find("]")
        if i < 0:
            continue
        key, txt = sline[1:i], sline[i + 1:].strip()
        if re.match(r"^\d+:\d+", key) and txt:
            d[key] = txt
    return d


def _lrc_merge_trans(lrc, tlrc):
    """把译文按时间戳插到原文后面（同时间戳双行，客户端本来就按语种顺序配对）"""
    tmap = _lrc_tl_map(tlrc)
    out = []
    for line in lrc.splitlines():
        sline = line.strip()
        if not sline:
            continue
        if not sline.startswith("["):
            out.append(line)
            continue
        i = sline.find("]")
        if i < 0:
            out.append(line)
            continue
        key, txt = sline[1:i], sline[i + 1:].strip()
        if not txt:                 # 源B常给「有时间戳但没字」的空行（前奏/间奏），去掉
            continue
        out.append(line)
        if key in tmap:
            out.append("[%s]%s" % (key, tmap[key]))
    return "\n".join(out)


def nc_lyric(name):
    """第三方歌词获取 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ""


# ---------------------------------------------------------------- 歌词译文补全
# 主源 上游站点 只给原文，源B才有 tlyric，而源B原本只在「上游站点 完全没词」时
# 才被调用 —— 于是「有原文、没译文」的歌永远等不到翻译。
# 这里反过来：拿已在用的原文当底稿，把源B译文按**文本锚点**挂回本地时间戳上
# （两个源的歌词行切分常常不一样，不能直接按时间戳相等去合）。
_TR_TRIED = {}          # 歌名 -> 秒；失败的不反复联网
_TR_COOLDOWN = 6 * 3600


def _lrc_lang(s):
    s = str(s or "")
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", s):
        return "jp"
    if re.search(r"[\uac00-\ud7af]", s):
        return "kr"
    lat = len(re.findall(r"[A-Za-z]", s))
    han = len(re.findall(r"[\u4e00-\u9fff]", s))
    if lat and han:
        return "zh" if han >= lat else "mix"
    if lat:
        return "en"
    if han:
        return "zh"
    return "other"


def _lrc_parse(txt):
    """LRC → [(秒, 文本)]，丢掉空行与纯元信息行"""
    out = []
    for line in (txt or "").splitlines():
        m = re.match(r"^\s*\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]\s*(.*)$", line)
        if not m:
            continue
        frac = (int((m.group(3) + "00")[:3]) / 1000.0) if m.group(3) else 0.0
        t = int(m.group(1)) * 60 + int(m.group(2)) + frac
        s = (m.group(4) or "").strip()
        if s:
            out.append((t, s))
    out.sort(key=lambda x: x[0])
    return out


def _fmt_ts(t):
    t = max(0.0, float(t))
    return "[%02d:%05.2f]" % (int(t // 60), t - 60 * int(t // 60))


def _lrc_foreign_ratio(lines):
    """外语行占比 / 中文行占比"""
    if not lines:
        return 0.0, 0.0
    fo = sum(1 for _, s in lines if _lrc_lang(s) in ("jp", "kr", "en", "mix"))
    zh = sum(1 for _, s in lines if _lrc_lang(s) == "zh")
    return fo / len(lines), zh / len(lines)


def _lrc_pair_ratio(lines):
    """双语对占比：同一时间戳出现两行 = 一组「原文 + 译文」"""
    if not lines:
        return 0.0
    hit = {}
    for t, _ in lines:
        hit[round(t, 2)] = hit.get(round(t, 2), 0) + 1
    return sum(1 for v in hit.values() if v > 1) / len(lines)


def lrc_needs_trans(lrc):
    """这首歌该不该去找译文：外语为主 + 中文（译文）几乎没有"""
    lines = _lrc_parse(lrc)
    if len(lines) < 6:
        return False
    fo, zh = _lrc_foreign_ratio(lines)
    if fo < 0.45:
        return False                 # 本来就是中文歌，夹的英文不用翻
    if zh > 0.30:
        return False                 # 已经有中文了（多半是残缺译文，交给前端配对）
    return _lrc_pair_ratio(lines) < 0.15


def _nc_lyric_raw(sid):
    """第三方歌词原文/译文 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ("", "")


def _norm_line(s):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", "", str(s or "").lower())


def _same_song(ll, nl):
    """整首歌词文本拼起来比一次：分行不同但内容相同的，相似度会很高。
    这是「能不能按时间轴直接映射」的判据 —— 版本不同（TV size / remix）就会掉下去。"""
    import difflib
    a = "".join(_norm_line(s) for _, s in ll)[:4000]
    b = "".join(_norm_line(s) for _, s in nl)[:4000]
    if len(a) < 20 or len(b) < 20:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.70


def _map_by_time(ll, nl, tr_of, tol=2.5):
    """按时间把译文挂到最近的本地行。行切分不同（本地逐句 / 源B长句）时用这条"""
    out, used = [], set()
    for i, tr in tr_of.items():
        tn = nl[i][0]
        bi, bd = -1, 1e9
        for j, (tl, _s) in enumerate(ll):
            if j in used:
                continue
            d = abs(tl - tn)
            if d < bd:
                bd, bi = d, j
        if bi >= 0 and bd <= tol:
            used.add(bi)
            out.append((ll[bi][0], tr))
    out.sort()
    return out


def _trans_anchors(local_lrc, nc_lrc, nc_tlrc):
    """把源B译文映射到本地时间戳 → [(本地秒, 译文)]"""
    import difflib
    nl = _lrc_parse(nc_lrc)
    if not nl:
        return []
    nt = _lrc_parse(nc_tlrc)
    if not nt:
        return []
    # 源B「原文行 → 译文」：同一时间戳（±0.12s）
    tr_of = {}
    for i, (t, _s) in enumerate(nl):
        for tt, tr in nt:
            if abs(tt - t) <= 0.12:
                tr_of[i] = tr
                break
    if not tr_of:
        return []
    ll = _lrc_parse(local_lrc)
    lnorm = [_norm_line(s) for _, s in ll]
    out, offs, used = [], [], set()
    for i, tr in tr_of.items():
        tn, sn = nl[i]
        n = _norm_line(sn)
        if len(n) < 2:
            continue
        bi, bs = -1, 0.0
        for j, l in enumerate(lnorm):
            if not l or j in used:
                continue
            sc = difflib.SequenceMatcher(None, n, l).ratio()
            if sc > bs:
                bs, bi = sc, j
        if bi >= 0 and bs >= 0.72:
            used.add(bi)
            out.append((ll[bi][0], tr))
            offs.append(ll[bi][0] - tn)
    if not offs:
        return []
    # 锚点覆盖率低（本地逐句、源B长句，整行比不上去）但确认是同一首歌时，
    # 改用时间轴映射，否则会出现「开头一整段没有译文」
    if len(offs) < len(tr_of) * 0.55 and _same_song(ll, nl):
        mapped = _map_by_time(ll, nl, tr_of)
        if len(mapped) > len(out):
            return mapped
    # 锚点必须够密才敢用「中位偏移」去推剩下的译文：
    # 只有两三行锚点时（两个版本歌词根本不同）推出来的时间戳全是错的，
    # 插进去就是「译文乱飞」，比没有译文更糟。
    out.sort()
    if len(offs) < 4 or len(offs) < len(nl) * 0.25:
        return out
    offs.sort()
    mid = offs[len(offs) // 2]
    have = [round(t, 2) for t, _ in out]
    for i, tr in tr_of.items():
        if any(abs(nl[i][0] + mid - h) < 0.35 for h in have):
            continue
        cand = nl[i][0] + mid
        if cand >= 0:
            out.append((cand, tr))
            have.append(round(cand, 2))
    out.sort()
    return out


def _insert_trans(lrc, pairs, tol=0.10):
    """把译文行插到原文行后面（同时间戳双行，前端按语种顺序配对）"""
    out, used = [], set()
    for raw in (lrc or "").splitlines():
        out.append(raw)
        m = re.match(r"^\s*\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]", raw)
        if not m:
            continue
        frac = (int((m.group(3) + "00")[:3]) / 1000.0) if m.group(3) else 0.0
        t = int(m.group(1)) * 60 + int(m.group(2)) + frac
        body = re.sub(r"^(\s*\[\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?\])+", "", raw).strip()
        if not body:
            continue
        for k, (tt, tr) in enumerate(pairs):
            if k in used or abs(tt - t) > tol:
                continue
            used.add(k)
            out.append("%s%s" % (_fmt_ts(t), tr))
            break
    return "\n".join(out)


def _lrc_last_t(lrc):
    ts = []
    for line in (lrc or "").splitlines():
        m = re.match(r"^\s*\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]", line)
        if m:
            frac = (int((m.group(3) + "00")[:3]) / 1000.0) if m.group(3) else 0.0
            ts.append(int(m.group(1)) * 60 + int(m.group(2)) + frac)
    return max(ts) if ts else 0.0


def trans_of_lrc(name, lrc, allow_replace=True):
    """给已有歌词补译文。失败/无需补时原样返回。
    allow_replace: 本地歌词和源B完全对不上时，允许整份换成源B的（含译文）"""
    if not lrc or not lrc_needs_trans(lrc):
        return lrc
    # 纯音乐 / 伴奏没有「译文」这回事，别为它们反复联网
    if re.search(r"纯音乐|純音樂|instrumental|カラオケ|off\s*vocal|伴奏", str(name), re.I):
        return lrc
    key = str(name)
    last = _TR_TRIED.get(key, 0)
    if time.time() - last < _TR_COOLDOWN:
        return lrc
    _TR_TRIED[key] = time.time()

    best = _nc_pick(name)
    if not best:
        return lrc
    try:
        nc_lrc, nc_tl = _nc_lyric_raw(best.get("id"))
    except Exception as e:
        log("trans lyric fail %r %r" % (name, e))
        return lrc
    if not nc_tl:
        log("trans miss %r (源B没有译文)" % name)
        return lrc
    few = sum(1 for s in re.findall(r"\[\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?\]\s*\S", nc_tl) or [])
    if few < 2:
        return lrc

    pairs = _trans_anchors(lrc, nc_lrc, nc_tl)
    ll = _lrc_parse(lrc)
    if len(pairs) >= max(3, int(len(ll) * 0.20)):
        out = _insert_trans(lrc, pairs)
        log("trans merge %r sid=%s 原文%d行 译文%d行" % (name, best.get("id"), len(ll), len(pairs)))
        return out

    # 锚点几乎对不上 → 本地歌词多半配错了歌（『天球』被配成另一首『そら』）
    if allow_replace and len(ll) >= 6:
        nc_lines = _lrc_parse(nc_lrc)
        merged = _lrc_merge_trans(nc_lrc, nc_tl)
        # 换之前先量体裁衣：源B这版要是比本地音频长出一截（单曲剪辑版 vs 完整版），
        # 换上去整首都会错位 —— 宁可这首没译文，也别给错歌词。
        dur = song_dur_of(name)
        fits = False
        if dur > 5 and len(nc_lines) >= 6:
            lt = _lrc_last_t(merged)
            fits = bool(lt) and lt <= dur + 4 and lt >= dur * 0.40
        if fits:
            log("trans replace %r sid=%s (本地与源B无公共行，整份替换 %.0fs/%.0fs)"
                % (name, best.get("id"), _lrc_last_t(merged), dur))
            return merged
        log("trans noreplace %r sid=%s (源B版 %.0fs 对不上音频 %.0fs)"
            % (name, best.get("id"), _lrc_last_t(merged), dur))
    log("trans skip %r 锚点不足(%d/%d)" % (name, len(pairs), len(ll)))
    return lrc


def nc_match(name):
    """第三方曲目匹配 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return None


def ensure_hi_cover(name, allow_net=True):
    """高清封面：内嵌（≥600px）> 源B 1024 > 内嵌原图（小也比没有强）> 空
    内嵌小图落盘后标 'low'，后台/下次请求还会再试在线高清，别把它当终版。"""
    # JY-FIX: 先按归一化歌名在缓存里找任意已有的高清封面（裸名/带号名互通），
    # 找不到再落到规范化落盘路径（去音轨号），保证新抓的图也走统一命名。
    found = _cover_best(name, "hi")
    dest = found or hi_cover_path(name)
    have = bool(dest) and os.path.exists(dest) and os.path.getsize(dest) > 3000
    src = (_meta_cache.get(name) or {}).get("hi")
    if not src:
        src = (_meta_cache.get(_track_norm(name)) or {}).get("hi")
    if have and src in ("file", "net", "small"):
        if src != "small":
            return dest
    fallback = None
    p = audio_path_of(name)
    if p:
        data, w, h = read_cover(p)
        if data and max(w, h) >= 600:
            try:
                with open(dest + ".part", "wb") as f:
                    f.write(data)
                os.replace(dest + ".part", dest)
                _bump_cover_meta(name, "file", max(w, h))
                return dest
            except Exception:
                pass
        elif data:
            fallback = data          # 内嵌图偏小，藏着，等在线高清失败时再用

    def _use_fallback(tag):
        if not fallback:
            return dest if have else ""
        try:
            with open(dest + ".part", "wb") as f:
                f.write(fallback)
            os.replace(dest + ".part", dest)
            _bump_cover_meta(name, tag, 0)
            return dest
        except Exception:
            return dest if have else ""

    if not allow_net:
        return _use_fallback("small") if not have else dest
    with _hi_lock:
        last = _hi_miss.get(name) or 0
        if last and time.time() - last < 7 * 86400:
            return _use_fallback("small") if not have else dest
    res = nc_match(name)
    if not res:
        _hi_miss[name] = time.time()
        return _use_fallback("small")
    sid, url = res
    try:
        data = fetch_bytes(url, referer="", timeout=25)
    except Exception as e:
        log("nc cover dl fail %r %r" % (name, e))
        _hi_miss[name] = time.time()
        return _use_fallback("small")
    if len(data) < 6000:
        _hi_miss[name] = time.time()
        return _use_fallback("small")
    try:
        with open(dest + ".part", "wb") as f:
            f.write(data)
        os.replace(dest + ".part", dest)
    except Exception:
        return dest if have else ""
    _bump_cover_meta(name, "net", 1024, ncid=sid)
    return dest


_mw = {"running": False, "done": 0, "total": 0, "hi": 0, "net": 0, "err": 0,
       "cover_done": 0, "cover_total": 0}


def meta_worker(allow_net=True, delay=0.03, net_delay=1.3):
    """后台补齐：① 全库内嵌标签（artist/title）② 高清封面（内嵌优先，缺的走源B）"""
    if _mw["running"]:
        return
    _mw.update(running=True, done=0, total=0, hi=0, net=0, err=0,
               cover_done=0, cover_total=0)
    try:
        songs = list_local_songs()
        _mw["total"] = len(songs)
        for i, s in enumerate(songs):
            try:
                song_meta(s)
            except Exception:
                _mw["err"] += 1
            _mw["done"] = i + 1
            if i % 40 == 0:
                save_meta()
                _local_rows["key"] = None      # 标签变了，列表缓存要重出
            time.sleep(delay)
        save_meta()
        _local_rows["key"] = None

        # 封面阶段：已经是终版（内嵌大图 / 在线 1024）的跳过，剩下的才去处理
        todo = []
        for s in songs:
            src = (_meta_cache.get(s) or {}).get("hi")
            if hi_cover_exists(s) and src in ("file", "net"):
                continue
            todo.append(s)
        _mw["cover_total"] = len(todo)

        def _one(x):
            try:
                ok = bool(ensure_hi_cover(x, allow_net=allow_net))
            except Exception:
                ok = False
            time.sleep(0.35 + random.random() * 0.4)   # 抖一下，别整成一排同拍请求
            return ok

        if allow_net and len(todo) > 3:
            # 源B一首要两次请求（搜 + 详情取封面），串行能跑一小时，用几个线程摊开
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=3) as ex:
                for ok in ex.map(_one, todo):
                    _mw["cover_done"] += 1
                    if ok:
                        _mw["hi"] += 1
                        _mw["net"] += 1
        else:
            for x in todo:
                if _one(x):
                    _mw["hi"] += 1
                _mw["cover_done"] += 1

        # 收尾：有高清图但缺 500 缩略图的，先缩好放普通封面目录，
        # 列表就不用每行现场解码大图（一屏 60 张会把 /sdcard 拖垮，首屏全是占位）
        for s in songs:
            try:
                if any(os.path.exists(_p) for _p in cover_cache_paths(s)):
                    continue
                if hi_cover_exists(s):
                    serve_cover(s)
            except Exception:
                pass
    finally:
        save_meta()
        _local_rows["key"] = None
        _mw["running"] = False
        log("meta_worker done: hi=%d net=%d err=%d" % (_mw["hi"], _mw["net"], _mw["err"]))


def song_dur_of(name):
    try:
        m = song_meta(name) or {}
        return float(m.get("dur") or 0)
    except Exception:
        return 0.0


_lv = {"obj": None}


def lyric_studio():
    """歌词视频生成器（延迟初始化，缺 Pillow/ffmpeg 时返回 None）"""
    if _lv["obj"] is not None:
        return _lv["obj"]
    if lyrvid is None or not lyrvid.HAVE_PIL or not lyrvid._ffmpeg():
        return None
    _lv["obj"] = lyrvid.LyricStudio(LVID_DIR, serve_lyric, serve_cover,
                                    audio_path_of, song_dur_of, log=log)
    return _lv["obj"]


def strip_from_playlists(base_name):
    """删歌后把歌单里的同名副本一并清掉（歌单存的是复制件），返回受影响的歌单名"""
    hit = []
    try:
        with _data_lock:
            names = [p.get("name") for p in _jy["playlists"]]
    except Exception:
        names = []
    for pl in names:
        try:
            p = os.path.join(playlist_dir(pl), sanitize_filename(base_name))
            if os.path.isfile(p):
                os.remove(p)
                hit.append(pl)
        except Exception as e:
            log("strip playlist %s fail %r" % (pl, e))
    return hit


def trash_song(name=None, rel=None):
    """把歌曲移进回收站，返回结果（可从回收站恢复）"""
    r, err = resolve_song_rel(name=name, rel=rel)
    if not r:
        return {"ok": False, "msg": err}
    src = os.path.join(MUSIC_DIR, *r.split("/"))
    base = os.path.basename(src)
    try:
        size = os.path.getsize(src)
    except Exception:
        size = 0
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    dst_dir = os.path.join(TRASH_DIR, tid)
    try:
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, base)
        shutil.move(src, dst)
        with open(os.path.join(dst_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"tid": tid, "rel": r, "name": base, "size": size,
                       "time": int(time.time())}, f, ensure_ascii=False)
    except Exception as e:
        return {"ok": False, "msg": "删除失败：%r" % (e,)}
    # 清掉文件相关缓存（封面/索引里的同名项）
    try:
        for k in list(_index.keys()):
            if k == base or k == os.path.splitext(base)[0]:
                _index.pop(k, None)
    except Exception:
        pass
    pl_hit = strip_from_playlists(base)
    log("trash: %s -> %s (playlists: %s)" % (r, tid, ",".join(pl_hit) or "-"))
    return {"ok": True, "tid": tid, "name": base, "rel": r, "size": size, "playlists": pl_hit}


def list_trash():
    items = []
    if not os.path.isdir(TRASH_DIR):
        return items
    for tid in os.listdir(TRASH_DIR):
        d = os.path.join(TRASH_DIR, tid)
        if not os.path.isdir(d):
            continue
        meta = {}
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
        if not meta:
            files = [f for f in os.listdir(d) if f.lower().endswith(_AUDIO_EXTS)]
            if not files:
                continue
            meta = {"tid": tid, "name": files[0], "rel": files[0],
                    "size": os.path.getsize(os.path.join(d, files[0])),
                    "time": int(os.path.getmtime(os.path.join(d, files[0])))}
        items.append(meta)
    items.sort(key=lambda x: x.get("time", 0), reverse=True)
    return items


def restore_song(tid):
    tid = str(tid or "").strip()
    if not tid or "/" in tid or ".." in tid:
        return {"ok": False, "msg": "参数不合法"}
    d = os.path.join(TRASH_DIR, tid)
    if not os.path.isdir(d):
        return {"ok": False, "msg": "回收站里没有这一项"}
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return {"ok": False, "msg": "记录损坏，无法恢复"}
    rel = meta.get("rel") or meta.get("name")
    if not rel:
        return {"ok": False, "msg": "记录缺少原路径"}
    src = os.path.join(d, os.path.basename(rel))
    if not os.path.isfile(src):
        return {"ok": False, "msg": "回收站里的文件已丢失"}
    dst = os.path.join(MUSIC_DIR, *rel.split("/"))
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            stem, ext = os.path.splitext(dst)
            dst = stem + " (恢复)" + ext
        shutil.move(src, dst)
        os.remove(os.path.join(d, "meta.json"))
        os.rmdir(d)
    except Exception as e:
        return {"ok": False, "msg": "恢复失败：%r" % (e,)}
    log("restore: %s -> %s" % (tid, rel))
    return {"ok": True, "name": os.path.basename(dst)}


def purge_song(tid):
    tid = str(tid or "").strip()
    if not tid or "/" in tid or ".." in tid:
        return {"ok": False, "msg": "参数不合法"}
    d = os.path.join(TRASH_DIR, tid)
    if not os.path.isdir(d):
        return {"ok": False, "msg": "回收站里没有这一项"}
    try:
        for fn in os.listdir(d):
            os.remove(os.path.join(d, fn))
        os.rmdir(d)
    except Exception as e:
        return {"ok": False, "msg": "彻底删除失败：%r" % (e,)}
    log("purge: %s" % tid)
    return {"ok": True}


def clear_trash():
    n = 0
    for it in list_trash():
        if purge_song(it.get("tid")).get("ok"):
            n += 1
    return {"ok": True, "count": n}


_py_cache = {}


def py_keys(name):
    """歌名的拼音全拼串 + 首字母串（带缓存）"""
    if not PY_OK:
        return "", ""
    if name in _py_cache:
        return _py_cache[name]
    s = clean_query(name)
    try:
        words = lazy_pinyin(s, errors=lambda x: list(x))
        full = "".join(words)
        init = "".join(w[0] for w in words if w)
    except Exception:
        full, init = "", ""
    _py_cache[name] = (full, init)
    return full, init


def fuzzy_match(name, kw):
    """本地歌曲模糊匹配打分：子串 / 多词 AND / 拼音全拼 / 拼音首字母 / 相似度。
    没有任何命中就返回 0，避免英文歌被相似度带上来的噪声。"""
    base = clean_query(name).lower()
    kws = [t for t in re.split(r"[\s,，、/]+", kw.lower()) if t]
    if not kws:
        return 0.0
    full, init = py_keys(name)
    score = 0.0
    hit_any, hit_all = False, True
    for t in kws:
        t = t.strip(" -_")
        if not t:
            continue
        s = 0
        if t in base:
            s = 10
        elif full and t in full:
            s = 8
        elif init and t in init:
            s = 6
        elif len(t) >= 2 and all(t[i:i + 2] in base for i in range(0, len(t) - 1, 2)):
            s = 4  # 中文按 2 字片段 AND
        if s:
            hit_any = True
        else:
            hit_all = False
        score += s
    if not hit_any:
        return 0.0
    if hit_all:
        score += 12
    try:
        from difflib import SequenceMatcher
        score += SequenceMatcher(None, base, " ".join(kws)).ratio() * 2
    except Exception:
        pass
    return score



# --- JY-LOCALCACHE：曲库文件清单缓存（walk 一次 ~1.4s，不要每次搜索都走） ---
_files = {"ts": 0.0, "rows": []}


def local_files(force=False, ttl=120):
    now = time.time()
    if not force and _files["rows"] and now - _files["ts"] < ttl:
        return _files["rows"]
    rows = []
    for root, dirs, files in os.walk(MUSIC_DIR):
        dirs[:] = [d for d in dirs if d not in ("tmp", "cache", "download", ".thumbnails")]
        rel_root = os.path.relpath(root, MUSIC_DIR)
        rel_root = "" if rel_root == "." else rel_root
        for fn in files:
            if not fn.lower().endswith((".mp3", ".flac", ".m4a", ".wav", ".ape", ".ogg", ".aac")):
                continue
            try:
                sz = os.path.getsize(os.path.join(root, fn))
            except Exception:
                continue
            if sz < 20 * 1024:
                continue
            rows.append((fn, rel_root, sz))
    _files["rows"] = rows
    _files["ts"] = now
    return rows


def local_files_invalidate():
    """下载完 / 删歌后叫一声：立刻让本地曲库缓存失效（默认缓存 120 秒）。

    不叫这一声的话，「刚点下载 → 马上点播放」会被判成「本地没有」，
    又走回在线流 —— 用户会觉得「我明明下载了怎么还在拉网络」。
    """
    try:
        _files["ts"] = 0
    except Exception:
        pass


_local_rows = {"key": None, "rows": None}


def drop_local_files():
    _files["ts"] = 0.0
    _files["rows"] = []
    _daily_cache["key"] = ""   # 曲库变了，当日推荐也重出
    _daily_cache["all"] = None # 「加载更多」的全量池同样作废，别让人翻到已删的歌
# --- end JY-LOCALCACHE ---


def search_local(kw, limit=30):
    rows = []
    for fn, rel_root, _sz in local_files():
        sc = fuzzy_match(fn, kw)
        if sc <= 0:
            continue
        artist, song = split_title(fn)
        rows.append({
            "name": fn,
            "song": song or clean_query(fn),
            "artist": artist,
            "playlist": rel_root,
            "score": round(sc, 2),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


# 「常听」自动进「我喜欢」的口径（v4.20）：近 7 天内播放次数 > 10 才算。
# playlog 每条只保留近 7 天的播放时间戳 ts；旧条目没有 ts（v4.20 之前不记），周播放数视为 0。
_JY_WEEK_SECONDS = 7 * 86400


def _weekly(rec):
    """近 7 天内的播放次数。playlog 旧条目没有 ts，返回 0（即不自动进「我喜欢」）。"""
    ts = (rec or {}).get("ts") or []
    if not ts:
        return 0
    cutoff = int(time.time()) - _JY_WEEK_SECONDS
    return sum(1 for t in ts if t >= cutoff)


def record_play(name, dur=None, played=None):
    """记一次播放。dur/played（秒）可选。

    不带 played：只是「这首歌放过」的次数（起播打点，老调用点照旧）。
    带 played  ：这一趟播放的「结果」（总长 + 实际听了多少秒）。这时不再加次数 ——
                 起播那一拍已经加过了，再加就成了一首算两次。
    """
    has_result = played is not None
    if not has_result:
        with _data_lock:
            rec = _jy["playlog"].get(name) or {"count": 0, "last": 0, "ts": []}
            rec["count"] += 1
            now = int(time.time())
            rec["last"] = now
            ts = rec.get("ts") or []
            ts.append(now)
            cutoff = now - _JY_WEEK_SECONDS
            rec["ts"] = [t for t in ts if t >= cutoff]
            _jy["playlog"][name] = rec
            # 只留最近 500 首
            if len(_jy["playlog"]) > 500:
                for k in sorted(_jy["playlog"], key=lambda k: _jy["playlog"][k]["last"])[:100]:
                    _jy["playlog"].pop(k, None)
            save_data()
    play_event(name, dur, played)


def recent_played(n=20):
    with _data_lock:
        items = sorted(_jy["playlog"].items(), key=lambda kv: -kv[1]["last"])
    return [k for k, _ in items[:n]]


# ---- JY-WIDGET: 小组件状态（App 原生轮询用，不依赖页面是否打开）----
def _default_did():
    try:
        with open(os.path.join(CONF_DIR, "setting.json"), encoding="utf-8") as f:
            return str(json.load(f).get("mi_did") or "")
    except Exception:
        return ""


def _lrc_lines(text):
    out = []
    for m in re.finditer(r"\[(\d+):(\d+(?:[.:]\d+)?)\]([^\[]*)", text or ""):
        try:
            t = int(m.group(1)) * 60 + float(m.group(2).replace(":", "."))
        except Exception:
            continue
        s = (m.group(3) or "").strip()
        if s:
            out.append((t, s))
    out.sort(key=lambda x: x[0])
    return out


def _stop_if_current(name):
    """要删的歌正好是音箱在放的这首 -> 主动停掉，别让它放完才算数"""
    try:
        did = _default_did()
        with urllib.request.urlopen(
                BACKEND_API + "/playingmusic?did=" + urllib.parse.quote(did), timeout=4) as r:
            pm = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        cur = str(pm.get("cur_music") or "")
        if not cur:
            return False
        base = os.path.splitext(os.path.basename(name or ""))[0]
        if cur == name or os.path.splitext(cur)[0] == base:
            req = urllib.request.Request(
                BACKEND_API + "/device/stop",
                data=json.dumps({"did": did}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(req, timeout=8).read()
            log("deleted playing song, stopped: %s" % name)
            return True
    except Exception as e:
        log("stop_if_current fail %r" % (e,))
    return False


# JY-DEL2
# JY-WIDGETMETA：歌词里常夹着「作词/作曲/编曲/演唱…」这类元信息行（时间戳排在
# 歌词前面或夹在中间）。widget_state 按进度挑「当前这句」时一旦落在这些行上，桌面
# 小组件就会显示「曲：Jam」之类。这里把它们当成非歌词，挑行时跳过。
_META_LRC_PREFIX = (
    "作词", "作曲", "编曲", "填词", "谱曲", "词：", "曲：",
    "演唱", "演唱者", "主唱", "合唱", "独唱",
    "Lyricist", "Composer", "Arranger", "Producer", "Vocal", "Vocals",
    "制作", "监制", "出品", "发行", "OP", "SP",
    "Guitar", "Bass", "Drums", "钢琴", "吉他", "贝斯", "鼓", "弦乐",
    "混音", "母带", "录音", "编曲：", "制作：",
)


def _is_meta_lrc(s):
    s = (s or "").strip()
    if not s:
        return True
    for p in _META_LRC_PREFIX:
        if s.startswith(p):
            return True
    return False


def widget_state(did="", host="127.0.0.1"):
    did = did or _default_did()
    pm = {}
    try:
        with urllib.request.urlopen(
                BACKEND_API + "/playingmusic?did=" + urllib.parse.quote(did), timeout=4) as r:
            pm = json.loads(r.read().decode("utf-8"))
    except Exception:
        pm = {}
    if str(pm.get("ret") or "") != "OK":
        return {"ok": False}
    name = str(pm.get("cur_music") or "")
    pos = max(0.0, float(pm.get("offset") or 0))
    dur = max(0.0, float(pm.get("duration") or 0))
    playing = bool(pm.get("is_playing"))
    # JY-WIDGETPOS：小组件的歌词一直卡在同一句 / 跟页面对不上，就是这里的 pos
    # 只认 xiaomusic 的 offset —— 那条是它墙钟推算的，停着/假死时恒为 0。
    # 页面自己早就有一套兜底（v3.90 那条 _softDead：offset ≈ 0 就改信设备亲口
    # 报的 position），小组件没跟上，于是「页面在 21 秒、小组件在 0 秒」。
    # 这里照抄页面那条口径：offset 拿不到（≈0）时才问设备，且只认这一首的位置。
    if pos < 0.2:
        try:
            _st = _xm_status(did) or {}
            _det = _st.get("play_song_detail") or {}
            _dpos = int(_det.get("position") or 0) / 1000.0
            _ddur = int(_det.get("duration") or 0) / 1000.0
            # 设备的时长跟 xiaomusic 差 1 秒以上说明报的是另一首，不采信
            if _dpos > 0.2 and (not (dur > 0) or abs(_ddur - dur) <= 1.5):
                pos = _dpos
            if not (dur > 0) and _ddur > 0:
                dur = _ddur
        except Exception:
            pass
    # JY-WIDGETMETA2：后台小组件(/api/widget)的歌手/歌名改用标签真源 display_meta，
    # 与歌单(/api/local)一致，修「未知歌手」(C) 与「文件名 vs 标签」歌名不一致 (B)。
    # display_meta 内部已是「标签优先、文件名兜底」，空名时退回 ("", "")。
    artist, song = display_meta(name) if name else ("", "")
    lrc1 = lrc2 = ""
    if name:
        try:
            txt, art = serve_lyric(name)
            if art and not artist:
                artist = art
            lines = _lrc_lines(txt)
            # 跳过元信息行（作词/作曲/编曲/演唱…），否则进度落在上面时
            # 桌面小组件会显示「曲：Jam」之类（JY-WIDGETMETA）。
            idx = -1
            for k, (lt, s2) in enumerate(lines):
                if _is_meta_lrc(s2):
                    continue
                if lt <= pos + 0.3:
                    idx = k
                else:
                    break
            if idx >= 0:
                lrc1 = lines[idx][1]
                # 下一句也跳元信息，避免组件显示「演唱：xxx」
                j = idx + 1
                while j < len(lines) and _is_meta_lrc(lines[j][1]):
                    j += 1
                if j < len(lines):
                    lrc2 = lines[j][1]
            # JY-WIDGETPH：只含歌名的一行不算歌词（前奏/间奏的占位）。
            # 原来只跟 song 比，而歌词里那行常是「歌名 - 歌手」（真机实测
            # 「七月上 - Jam」），跟 song="七月上" 比不上 —— 标题行就被当成
            # 当前句显示出来了。这里把「歌名 / 歌手 / 歌名-歌手 / 歌手-歌名」
            # 都算占位，两种分隔（空格横杠、光横杠）都认。
            _ph = set()
            for _a in (song, artist), (artist, song):
                for _sep in (" - ", "-", " "):
                    _v = _sep.join([x for x in _a if x]).strip().lower()
                    if _v:
                        _ph.add(_v)
            for _v in (song, artist, name):
                if (_v or "").strip():
                    _ph.add((_v or "").strip().lower())
            if lrc1 and lrc1.strip().lower() in _ph:
                lrc1 = ""
            if lrc2 and lrc2.strip().lower() in _ph:
                lrc2 = ""
        except Exception:
            pass
    # 谁点的歌都记一笔：换了曲目就写进播放历史，「已播」不再只认 App 点过的
    if name:
        try:
            if recent_played(1)[:1] != [name]:
                record_play(name)
        except Exception:
            pass
    return {
        "ok": True, "has": bool(name), "playing": playing,
        "title": song or name, "artist": artist, "name": name,
        "pos": int(pos), "dur": int(dur),
        "lrc": lrc1, "lrc2": lrc2,
        "cover": ("http://%s:58091/api/cover?name=%s" % (host, urllib.parse.quote(name))) if name else "",
    }


def most_played(n=20):
    with _data_lock:
        items = sorted(_jy["playlog"].items(), key=lambda kv: -kv[1]["count"])
    return [k for k, _ in items[:n]]


_daily_cache = {"key": "", "out": None, "all": None, "slots": {}}
# all：这一批推荐背后的「全量排序池」（online / local 各最多 60 条）。
# 前端下拉「加载更多」时直接从它切片，不用重算 —— 重算一次要 3~10 秒（要重新搜索上游）。
# 注意 all 和 out 里的 dict 是同一批对象（out 是 all 的前 limit 条），补字段一次就够。


# ==================== JY-RECS 每日推荐：隐式反馈 + MMR 重排 ====================
# 以前推荐只会「拿最近播过的歌名/歌手去搜一遍」，谁被听完、谁被跳过，服务端一无所知，
# 所以「每日推荐」看着天天变、其实跟口味没关系。这一块补两件事：
#   1) 记「一首歌到底听了多少秒」，折成喜欢/不爱听的分数（完播加分、秒跳扣分）；
#   2) 用 MMR（最大边际相关）把候选重排：别一屏全是同一个歌手、同一个味。
# 候选池仍然只有两个来源（源A搜索 + 本地曲库）。重排只做「打分 + 排序 + 取子集」，
# 不会凭空造歌 —— 推荐里的每一首都能在候选池里找到。
#
# 数据文件：/root/xiaomusic_conf/cache/jy_play_events.json（与 jy_data.json 分开，
# 事件量大、写频繁，别跟歌单混在一个 json 里）

_EV_FILE = os.path.join(CACHE_DIR, "jy_play_events.json")
_EV_MAX = 1200            # 只留最近 1200 条「听歌结果」，够算口味了
_play_events = []         # [{n: 歌名, t: 时间戳, d: 总长秒, p: 实际听了多少秒}]
_ev_lock = threading.Lock()
_ev_ready = False

# 四种听歌结局的权重。完播最值钱；「一半以上」算正常收听；
# 15~60 秒跳走是明确的不喜欢；15 秒内就跳是最强的负信号。
_EV_W = {"done": 1.0, "listen": 0.45, "skip": -0.55, "hop": -1.2}
# 但「15 秒内就跳」最容易误伤（前奏还没进、点错了、别人抢了遥控器），
# 所以再乘一个降权系数：最终比 15~60 秒跳过更狠，但不至于一首判死。
_HOP_DAMP = 0.6
_EV_HALFLIFE = 14.0       # 反馈半衰期（天）：两周前的一次跳过不该跟昨天一样重


def _load_events():
    global _play_events
    try:
        with open(_EV_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        if isinstance(rows, list):
            _play_events = [r for r in rows if isinstance(r, dict) and r.get("n")][-_EV_MAX:]
    except Exception:
        _play_events = []


def _save_events():
    try:
        if not os.path.isdir(CACHE_DIR):
            return
        tmp = _EV_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_play_events[-_EV_MAX:], f, ensure_ascii=False)
        os.replace(tmp, _EV_FILE)
    except Exception as e:
        log("save play events: %r" % (e,))


def _events():
    """懒加载：第一次要的时候才读盘（服务刚起来时 CACHE_DIR 可能还没建）"""
    global _ev_ready
    if not _ev_ready:
        _ev_ready = True
        _load_events()
    return _play_events


def play_event(name, dur=None, played=None):
    """记一条「这首听得怎么样」。played<=0.5 秒的多半只是起播打点，不算数据。"""
    if not name:
        return
    try:
        d = max(0.0, float(dur or 0))
    except Exception:
        d = 0.0
    try:
        p = max(0.0, float(played or 0))
    except Exception:
        p = 0.0
    if p <= 0.5:
        return
    if d and p > d * 1.2:      # 报文里的时长不准时别让它离谱
        p = d * 1.2
    events = _events()         # 先确保磁盘上的老事件已经读进来，别被这一条覆盖掉
    with _ev_lock:
        events.append({"n": str(name), "t": int(time.time()),
                       "d": int(round(d)), "p": int(round(p))})
        if len(events) > _EV_MAX:
            del events[:len(events) - _EV_MAX]
        _save_events()


def _ev_kind(ev):
    """完播 / 正常听 / 中段跳 / 开头秒跳"""
    d = ev.get("d") or 0
    p = ev.get("p") or 0
    if d and p >= d * 0.85:
        return "done"
    if p < 15:
        return "hop"
    if p < 60:
        return "skip"
    return "listen"


def _ev_weight(ev):
    w = _EV_W.get(_ev_kind(ev), 0.0)
    if _ev_kind(ev) == "hop":
        w *= _HOP_DAMP
    age = max(0.0, time.time() - float(ev.get("t") or 0)) / 86400.0
    try:
        return w * (0.5 ** (age / _EV_HALFLIFE))
    except Exception:
        return w


def _norm_key(s):
    return (clean_query(s or "") or "").strip().lower()


def _squash(x):
    """把无界累加压到 (-1, 1)：听得再多也只逼近 ±1，不会一首歌吃掉整个口味"""
    return x / (1.0 + abs(x)) if x else 0.0


def taste_profile(likes=None):
    """把「听歌结果 + 收藏 + 红心」折成口味画像。

    song       : {归一歌名: 分数}   这首我听完了还是在 15 秒处跳了
    artist     : {归一歌手: 分数}   这个歌手整体喜不喜欢
    heard      : 归一歌名集合       听过的（用来判「冷门」）
    seen_song  : 归一歌名集合       听过的（同一首歌的不同写法都算，见 _add_song）
    artists    : 归一歌手集合       听过的歌手
    liked      : 归一歌名集合       红心 / 收进歌单
    liked_song : 红心歌曲的「歌名部分」—— 本地记的是「歌手 - 歌名.mp3」，
                 线上候选只有「歌名」，不拆开就对不上，红心等于白给。
    """
    prof = {"song": {}, "artist": {}, "heard": set(), "seen_song": set(),
            "artists": set(), "liked": set(), "liked_song": set()}

    def _add_song(name, w):
        """同一首歌两种写法都要记：整串「歌手 - 歌名」和拆出来的「歌名」。
        本地/历史上报的是带歌手前缀的文件名，线上候选只有歌名，只记一种会永远对不上。"""
        full = _norm_key(name)
        if not full:
            return
        prof["song"][full] = prof["song"].get(full, 0.0) + w
        prof["heard"].add(full)
        prof["seen_song"].add(full)
        a, s = split_title(name)
        sp = _norm_key(s)
        if sp and sp != full:
            prof["song"][sp] = prof["song"].get(sp, 0.0) + w
            prof["heard"].add(sp)
            prof["seen_song"].add(sp)
        if a:
            prof["artists"].add(_norm_key(a))

    for n in (likes or []):
        k = _norm_key(n)
        if not k:
            continue
        prof["liked"].add(k)
        sp = _norm_key(split_title(n)[1])
        if sp:
            prof["liked_song"].add(sp)
    with _data_lock:
        playlog = {k: dict(v) for k, v in (_jy.get("playlog") or {}).items() if isinstance(v, dict)}
    # 播放次数是弱信号：听过几次的歌，哪怕上次是跳过的也说明你对它有点兴趣
    for name, rec in playlog.items():
        n = int(rec.get("count") or 0)
        if n >= 2:
            bonus = min(0.5, 0.15 * (n - 1))
            _add_song(name, bonus)
            a = _norm_key(split_title(name)[0])
            if a:
                prof["artist"][a] = prof["artist"].get(a, 0.0) + bonus * 0.6
        else:
            _add_song(name, 0.0)      # 只登记「听过」，不加分
    # 听歌结果才是主信号：完播加分、秒跳扣分
    for ev in _events():
        name = ev.get("n") or ""
        if not name:
            continue
        w = _ev_weight(ev)
        _add_song(name, w)
        a = _norm_key(split_title(name)[0])
        if a:
            prof["artist"][a] = prof["artist"].get(a, 0.0) + w
    for k, v in list(prof["song"].items()):
        prof["song"][k] = _squash(v)
    for k, v in list(prof["artist"].items()):
        prof["artist"][k] = _squash(v)
    return prof


def daily_jitter(key, daily_seed=None, span=0.05):
    """对最终分数加一个 ±span 的确定性扰动。

    同一天 + 同一首歌 → 永远同一个值（当天刷多少次都一样）；
    换一天 → 值跟着换，分数咬得很紧的那几首自然换个先后。
    别用 random.random()：那样同一天刷两次结果都不一样，就不叫「每日推荐」了。
    """
    sd = str(daily_seed if daily_seed is not None else time.strftime("%Y%m%d"))
    h = hashlib.md5((sd + "|" + str(key or "")).encode("utf-8")).hexdigest()
    return (int(h[:8], 16) / float(0xFFFFFFFF) - 0.5) * 2.0 * span


_GENRE_HINT = (
    ("dj", "电子"), ("remix", "电子"), ("电音", "电子"), ("混音", "电子"),
    ("伴奏", "伴奏"), ("纯音乐", "纯音乐"), ("轻音乐", "纯音乐"),
    ("钢琴", "纯音乐"), ("吉他", "纯音乐"), ("古筝", "纯音乐"),
    ("live", "现场"), ("演唱会", "现场"), ("现场", "现场"),
    ("古风", "古风"), ("中国风", "古风"),
    ("儿歌", "童声"), ("童声", "童声"),
    ("摇滚", "摇滚"), ("rock", "摇滚"),
    ("说唱", "说唱"), ("rap", "说唱"), ("hiphop", "说唱"), ("嘻哈", "说唱"),
    ("民谣", "民谣"), ("爵士", "爵士"), ("jazz", "爵士"), ("古典", "古典"),
    ("acg", "ACG"), ("二次元", "ACG"), ("动漫", "ACG"),
)


def genre_of(c):
    """粗粒度曲风。

    候选池（源A搜索结果）只给「歌名 + 歌手」，没有曲风字段，所以只能从
    歌名/标题/所在目录名里认关键词。认不出来就返回空串 —— 空串不参与
    「同曲风最多 3 首」的计数（不然所有歌都落进同一个「未知」桶，反而把列表压死）。
    想要真正的曲风标签得上多源（Last.fm tags / 源B），那是阶段二的事。
    """
    t = ("%s %s %s" % (c.get("song") or c.get("name") or "",
                       c.get("title") or "",
                       c.get("playlist") or "")).lower()
    for k, g in _GENRE_HINT:
        if k in t:
            return g
    return ""


# v3.83：合唱/合作歌手拆解。
# 以前 `_cand_artist` 是整串归一，于是「张信哲」「张信哲&刘嘉玲」「张信哲、太一」
# 「李克勤&张信哲」被当成 **4 个不同歌手** —— 歌手上限（每歌手最多 N 首）就形同虚设：
# 实测一张「张信哲」卡 12 首里 9 首张信哲 + 3 首合唱，全是他一个人。
_ART_SPLIT = re.compile(
    r"\s*(?:&|＆|、|/|／|,|，|;|；|\+|×|\bx\b|\bvs\.?|\bfeat\.?|\bft\.?|\bwith\b)\s*",
    re.I)

# 这些不是某个人，别占「每歌手几首」的名额
_ART_JUNK = {"", "群星", "various artists", "various", "未知", "未知歌手", "unknown",
             "网络歌手", "佚名", "none", "n/a", "群星演唱"}
_ART_MEMO = {}


def _art_list(c):
    """一首候选的**全部**歌手（归一、去重、主歌手在前）。

    拆得太碎没关系 —— 判定「同一个人」用的是「有没有交集」，拆出两个名字的
    Simon & Garfunkel 两首歌照样算同一个歌手。
    """
    raw = str((c or {}).get("artist") or "")
    hit = _ART_MEMO.get(raw)
    if hit is not None:
        return hit
    out = []
    for p in _ART_SPLIT.split(raw):
        k = _norm_key(p)
        if not k or k in _ART_JUNK or k in out:
            continue
        out.append(k)
    if not out:
        k = _norm_key(raw)
        if k and k not in _ART_JUNK:
            out.append(k)
    if len(_ART_MEMO) > 800:      # _sim 是 O(n²) 调的，这个 memo 不是可有可无
        _ART_MEMO.clear()
    _ART_MEMO[raw] = out
    return out


def _cand_artist(c):
    """主歌手（第一个）。合唱按主歌手归属：口味匹配、相似度、每日扰动都用它。"""
    a = _art_list(c)
    return a[0] if a else ""


def _art_overlap(a, b):
    """两首候选的歌手集合有没有交集（合唱算「同一个人」）。"""
    sa, sb = _art_list(a), _art_list(b)
    if not sa or not sb:
        return False
    return any(x in sb for x in sa)


def _cand_song(c):
    return _norm_key(c.get("song") or c.get("name") or "")


def _sim(a, b):
    """两首候选有多像：0 = 无关，1 = 同一个人唱的同一首歌。

    只看歌手/曲风——候选池里只有这两个维度，硬凑「声学相似」就是拍脑袋。
    """
    if not a or not b:
        return 0.0
    if _art_overlap(a, b):
        return 1.0
    ga, gb = a.get("genre") or "", b.get("genre") or ""
    if ga and ga == gb:
        return 0.55
    if _cand_song(a) and _cand_song(a) == _cand_song(b):
        return 0.9          # 同歌不同版本（翻唱 / 伴奏 / 重制）
    return 0.0


def score_candidates(rows, prof=None, src_w=None):
    """给候选池打分：歌手口味为主、同名歌经验次之、来源关键词可信度再次之。

    只改 rows 里的 rel / genre / _cold 三个字段，不动歌单本身 —— 打分只影响排序。
    """
    prof = prof or {"song": {}, "artist": {}, "heard": set(), "artists": set(), "liked": set()}
    liked_artists = set()
    for k in prof.get("liked") or ():
        a = _norm_key(split_title(k)[0])
        if a:
            liked_artists.add(a)
    liked_songs = prof.get("liked_song") or set()
    for c in rows:
        a, s = _cand_artist(c), _cand_song(c)
        # 口味里的歌手名是**整串**（split_title 出来的「A&B」就存成 "a&b"），
        # 拆开后的单名可能对不上 → 整串 + 每个单名都比一遍，取最高的那个。
        aff_a = prof["artist"].get(a, 0.0) if a else 0.0
        for _x in _art_list(c):
            aff_a = max(aff_a, prof["artist"].get(_x, 0.0) or 0.0)
        aff_s = prof["song"].get(s, 0.0) if s else 0.0
        w = float(c.get("_w") if c.get("_w") is not None else (src_w if src_w is not None else 0.6))
        rel = 0.55 * aff_a + 0.25 * aff_s + 0.12 * w
        if s and (s in prof["liked"] or s in liked_songs):
            rel += 0.30                     # 红心过 / 收进歌单
        elif a and a in liked_artists:
            rel += 0.10                     # 这个歌手有你收藏过的歌
        if s and s in prof["heard"] and prof["song"].get(s, 0.0) < 0:
            rel -= 0.35                     # 上次是跳过的，别急着再推一遍
        c["genre"] = c.get("genre") or genre_of(c)
        # 「冷门」：没听过这个歌手、也没听过这首歌 —— 用来保证推荐里至少留一首新鲜的
        c["_cold"] = (not any(x in (prof["artists"] or ()) for x in _art_list(c))
                      and not (s and s in prof["heard"]))
        c["rel"] = round(rel, 4)
    return rows


def mmr_rerank(candidates, lambda_=0.7, daily_seed=None, limit=None,
               max_per_artist=2, max_per_genre=3, cold_min=1):
    """MMR 重排：分数 = λ·相关性 − (1−λ)·已选里最像的那首的相似度。

    λ=0.7 → 相关性占七成、多样性占三成（越小越多变，但不能太小，否则好歌全被挤掉）。

    除了 MMR 本身，还压了几条硬约束（用户要的：一屏别全是同一个人、同一个味）：
      · 同一歌手最多 max_per_artist 首
      · 同一曲风最多 max_per_genre 首（曲风认不出来的不计数，见 genre_of）
      · 前 5 首里不出现同一歌手的相邻两首
      · 至少留冷门候选 cold_min 首（池子里真没有就作罢，绝不为了凑数去池外找歌）
    约束按顺序硬挡，全被挡住时**逐条**放宽：先松「相邻」→ 歌手上限翻倍 → 最后才彻底
    放开（放开时也给重复的扣分）。宁可少几首，也不能做空、更不能整屏都是同一个人。
    """
    rows = [dict(c) for c in (candidates or []) if c]
    if not rows:
        return []
    n_out = int(limit or len(rows))
    lam = max(0.0, min(1.0, float(lambda_)))
    rels = [float(c.get("rel") or 0.0) for c in rows]
    lo, hi = min(rels), max(rels)
    for c in rows:
        r = float(c.get("rel") or 0.0)
        c["_r"] = 0.5 if (hi - lo) < 1e-9 else (r - lo) / (hi - lo)
        c["_as"] = _art_list(c)          # v3.83：合唱拆开后的全部歌手
        c["_a"] = c["_as"][0] if c["_as"] else ""
        c["_g"] = c.get("genre") or ""
    left = list(range(len(rows)))
    picked, per_a, per_g = [], {}, {}

    def _acount(c, cap):
        """这个人已经选了几首里，有没有已经到上限的（合唱的任一参与者都算）。"""
        return any(per_a.get(x, 0) >= cap for x in (c.get("_as") or ()))

    def _ok(i, relax):
        """relax：0 全约束 / 1 只松「相邻」 / 2 歌手上限翻倍 / 3 彻底不管。"""
        c = rows[i]
        a_cap = max_per_artist * 2 if relax >= 2 else max_per_artist
        g_cap = 10 ** 6 if relax >= 2 else max_per_genre
        if c["_as"] and _acount(c, a_cap):
            return False
        if c["_g"] and per_g.get(c["_g"], 0) >= g_cap:
            return False
        if relax == 0 and picked and len(picked) < 5 and c["_as"] and \
                any(x in (picked[-1].get("_as") or ()) for x in c["_as"]):
            return False
        return True

    while left and len(picked) < n_out:
        best, best_s = None, None
        for relax in (0, 1, 2, 3):
            # v3.83：多了一档 2（歌手上限翻倍），彻底不管退到 3。
            # 以前 2 就是「什么都不管」：池子窄的时候（比如全是一个人的专辑、或者
            # 一整屏同曲风的慢钢琴）会一路放行到 12 首里 4 首同一个人。
            for i in left:
                if relax < 3 and not _ok(i, relax):
                    continue
                c = rows[i]
                div = max([_sim(c, p) for p in picked] or [0.0])
                s = lam * c["_r"] - (1.0 - lam) * div
                if relax >= 3:
                    # 真的没得挑了：至少别在同一个歌手/曲风上继续叠，
                    # 每次都优先挑重复得最少的那个 —— 池子再窄也要尽量给点变化。
                    _rep = max([per_a.get(x, 0) for x in (c["_as"] or (""))] or [0])
                    s -= 0.15 * (_rep + per_g.get(c["_g"], 0))
                # 最终分数上的 ±0.05 确定性扰动：同一天同一首永远同一个值（刷多少次都一样），
                # 跨天自动换一个值，分数咬得紧的那几首第二天自然换个先后。
                # key 只用「歌本身」（id/歌手/歌名），跟池子大小无关，才叫「同一天稳定」。
                s += daily_jitter(str(c.get("id") or "") + "|" + c["_a"] + "|" +
                                  _cand_song(c), daily_seed)
                if best_s is None or s > best_s:
                    best, best_s = i, s
            if best is not None:
                break
        if best is None:
            break
        c = rows[best]
        left.remove(best)
        picked.append(c)
        for _x in (c["_as"] or ()):
            per_a[_x] = per_a.get(_x, 0) + 1
        if c["_g"]:
            per_g[c["_g"]] = per_g.get(c["_g"], 0) + 1

    # 冷门保底：MMR 天然偏爱高分（=你熟的）歌，不硬留一手，冷门永远进不来
    if cold_min > 0 and picked:
        have = sum(1 for c in picked if c.get("_cold"))
        if have < cold_min:
            for i in sorted([j for j in left if rows[j].get("_cold")],
                            key=lambda j: -rows[j]["_r"])[:cold_min - have]:
                # 从尾巴往前换：头几首是相关性最高的，别动
                for k in range(len(picked) - 1, 2, -1):
                    if picked[k].get("_cold"):
                        continue
                    c = rows[i]
                    if c["_as"] and _acount(c, max_per_artist + 1):
                        continue
                    for _x in (c["_as"] or ()):
                        per_a[_x] = per_a.get(_x, 0) + 1
                    picked[k] = c
                    if i in left:
                        left.remove(i)
                    break
    return picked[:n_out]


def _strip_priv(c):
    return {k: v for k, v in c.items() if not k.startswith("_")}




# ==================== JY-RECS 阶段二：多源召回融合 ====================
# 在阶段一的「源A搜索 + 本地曲库」两个候选源之外，再并上外部口味源：
#   · Last.fm      : track.getSimilar / artist.getSimilar / artist.getTopTags（真实曲风 + 相似歌）
#   · 源B        : /api/personalized/newsong（公开 feed，无需登录）+ /recommend/songs（个性化，需 MUSIC_U cookie，weapi 加密）
#   · ListenBrainz : /user/{user}/recommendations（真实「相似用户」推荐，带 track_name/artist_name）
#   · Embeat       : 可选声学相似 HTTP 端点（默认关，未验证）
# 这些源只负责「发现」：每条外部建议都要先解析成本地曲库/源A里真实存在的歌
# （按 歌名+歌手 匹配），解析不到直接丢。所以阶段二扩宽了发现口径，最终列表仍
# 100% 在候选池内——严守阶段一「严禁推荐池外歌曲」的底线。
# 任一外部 API 失败 / 超时 / 没配 key → 该源返回 []，降级为只用本地 + 源A，主流程不受影响。
#
# 配置：/root/xiaomusic_conf/jy_rec_sources.json（各源开关 + key；不存任何用户隐私）

_REC_SRC_FILE = os.path.join(CACHE_DIR, "jy_rec_sources.json")
_rec_src = {}
_rec_src_ready = False
_rec_cache = {"key": "", "out": None, "ts": 0}
_rec_cache_lock = threading.Lock()
_rec_stats = {"last": {}, "errors": {}}



def rec_sources_cfg(refresh=False):
    global _rec_src, _rec_src_ready
    if not _rec_src_ready or refresh:
        _rec_src_ready = True
        try:
            with open(_REC_SRC_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                _rec_src = d
        except Exception:
            _rec_src = {}
    return _rec_src


def _rs_get(*path, default=None):
    d = rec_sources_cfg()
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return d if d is not None else default


def _rs_enabled(name):
    c = rec_sources_cfg().get(name)
    return isinstance(c, dict) and bool(c.get("enabled"))


def _sug(artist, song="", genre="", source="", weight=0.5):
    return {"artist": artist or "", "song": song or "",
            "genre": genre or "", "source": source, "weight": float(weight)}


def _http_get_json(url, headers=None, timeout=5, data=None):
    req = urllib.request.Request(url, data=data,
                                 headers=headers or {"User-Agent": "jingyu-mic/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore") or "{}")


# ---------- Last.fm ----------
_LFM_GENRE_MAP = {
    "electronic": "电子", "edm": "电子", "dance": "电子", "house": "电子", "techno": "电子",
    "synthpop": "电子", "chinese": "华语", "mandopop": "华语", "c-pop": "华语", "cantopop": "华语",
    "j-pop": "日系", "anime": "ACG", "j-rock": "日系", "k-pop": "韩系", "korean": "韩系",
    "rock": "摇滚", "alternative": "摇滚", "metal": "摇滚", "punk": "摇滚",
    "pop": "流行", "indie": "独立", "folk": "民谣", "singer-songwriter": "民谣",
    "rap": "说唱", "hip hop": "说唱", "hip-hop": "说唱",
    "classical": "古典", "piano": "纯音乐", "instrumental": "纯音乐", "ambient": "纯音乐",
    "jazz": "爵士", "blues": "布鲁斯", "country": "乡村",
    "rnb": "节奏蓝调", "soul": "节奏蓝调", "funk": "节奏蓝调",
    "lofi": "舒缓", "chillout": "舒缓", "children": "童声", "disney": "童声",
    "ost": "影视", "soundtrack": "影视",
}


def _lfm_genre(tags):
    if not isinstance(tags, list):
        return ""
    best = ""
    for t in tags:
        name = (t.get("name") if isinstance(t, dict) else t) or ""
        name = name.lower()
        if name in _LFM_GENRE_MAP:
            return _LFM_GENRE_MAP[name]
        for k, g in _LFM_GENRE_MAP.items():
            if k in name:
                best = g
                break
    return best


def src_lastfm(prof):
    key = _rs_get("lastfm", "api_key")
    if not key:
        return [], {}
    out, genre_hints = [], {}
    def g(method, **params):
        params = {"method": method, "api_key": key, "format": "json",
                  "autocorrect": 1, **params}
        url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(params)
        return _http_get_json(url, timeout=5)
    try:
        arts = sorted(((a, w) for a, w in prof.get("artist", {}).items()),
                      key=lambda kv: -kv[1])[:6]
        for a, w in arts:
            if w <= 0:
                continue
            try:
                j = g("artist.getSimilar", artist=a, limit=8)
                for s in (j.get("similarartists", {}).get("artist") or [])[:8]:
                    out.append(_sug(s.get("name", ""), "", "", "lastfm",
                                    min(0.6, 0.2 + 0.08 * max(0.0, w))))
            except Exception:
                pass
            try:
                j = g("artist.getTopTags", artist=a)
                gt = _lfm_genre(j.get("toptags", {}).get("tag"))
                if gt:
                    genre_hints[_norm_key(a)] = gt
            except Exception:
                pass
        songs = sorted(((s, w) for s, w in prof.get("song", {}).items()),
                       key=lambda kv: -kv[1])[:6]
        for full, w in songs:
            a, s = split_title(full)
            if not s:
                a, s = "", full
            if not s:
                continue
            try:
                j = g("track.getSimilar", artist=a, track=s, limit=10)
                for t in (j.get("similartracks", {}).get("track") or [])[:10]:
                    na = (t.get("artist", {}).get("name", "")
                          if isinstance(t.get("artist"), dict) else t.get("artist", ""))
                    out.append(_sug(na, t.get("name", ""), "", "lastfm",
                                    min(0.72, 0.25 + 0.12 * max(0.0, w))))
            except Exception:
                pass
    except Exception:
        pass
    return out, {"genre_hints": genre_hints}


# ---------- 源B（公开 feed，无需登录） ----------


# ---------- 源B weapi 加密（纯 Python，零依赖） ----------
# 个性化推荐 /recommend/songs 走 weapi（AES-128-CBC × 2 + RSA encSecKey），需要登录 cookie。
# 公开 feed /api/personalized/newsong 仍是明文 JSON，无需加密（见 src_netease 分支 A）。
_NC_SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16
]


def _netease_weapi(url, body, cookie):
    """第三方加密接口封装 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return {}


def src_netease(prof):
    """第三方源适配 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    此函数保留签名、恒返回空值，调用方无需改动即可运行（该功能为空）。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ([], {})


def src_qqmusic(prof):
    """第三方音乐平台源适配 —— 接口占位（未实现）。

    本仓库只处理本地曲库，不含任何在线内容源的实现，也不提供这部分代码。
    若你需要检索 / 试听 / 下载 / 抓词，请自行接入你已获授权的数据源。
    函数保留签名、恒返回空值，调用方按原逻辑会得到「没有结果」。

    接入指引见仓库根目录 docs/OPTIONAL-ONLINE-SOURCES.md 。
    """
# [integration-point]
    return ([], {})


# ---------- ListenBrainz ----------
def src_listenbrainz(prof):
    user = _rs_get("listenbrainz", "user")
    if not user:
        return [], {}
    out = []
    try:
        j = _http_get_json("https://api.listenbrainz.org/1/user/%s/recommendations" %
                           urllib.parse.quote(user), timeout=3)
        for it in (j.get("recommendations") or [])[:20]:
            name = it.get("track_name") or it.get("title") or ""
            artist = it.get("artist_name") or it.get("artist") or ""
            if name:
                out.append(_sug(artist, name, "", "listenbrainz", 0.55))
    except Exception as e:
        _rec_stats["errors"]["listenbrainz"] = str(e)[:120]
    return out, {}


# ---------- Embeat（可选声学相似，默认关） ----------
def src_embeat(prof):
    url = _rs_get("embeat", "url")
    if not url:
        return [], {}
    out = []
    try:
        body = json.dumps({"songs": recent_played(30)}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "jingyu-mic/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            j = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        for it in (j.get("similar") or j.get("songs") or [])[:20]:
            if isinstance(it, dict) and it.get("song"):
                out.append(_sug(it.get("artist", ""), it["song"], "", "embeat", 0.5))
    except Exception as e:
        _rec_stats["errors"]["embeat"] = str(e)[:120]
    return out, {}


def _prof_sig(prof):
    """口味签名：取画像里权重最高的几个歌手/歌名，用于外部召回缓存的 key。"""
    arts = sorted(prof.get("artist", {}).items(), key=lambda kv: -kv[1])[:3]
    songs = sorted(prof.get("song", {}).items(), key=lambda kv: -kv[1])[:3]
    return "|".join([a for a, _ in arts] + [s for s, _ in songs])


def recall_multi(prof, bucket=""):
    """并发拉所有开启的外部源，合并建议 + 曲风提示。任一源失败不影响其余。"""
    timeout = float(_rs_get("timeout", default=4.0) or 4.0)
    srcs = []
    if _rs_enabled("lastfm"):
        srcs.append(("lastfm", lambda: src_lastfm(prof)))
    if _rs_enabled("netease"):
        srcs.append(("netease", lambda: src_netease(prof)))
    if _rs_enabled("listenbrainz"):
        srcs.append(("listenbrainz", lambda: src_listenbrainz(prof)))
    if _rs_enabled("embeat"):
        srcs.append(("embeat", lambda: src_embeat(prof)))
    if _rs_enabled("qqmusic"):
        srcs.append(("qqmusic", lambda: src_qqmusic(prof)))
    if not srcs:
        return [], {"genre_hints": {}}
    ck = "rc|%s|%s" % (bucket, timeout)
    with _rec_cache_lock:
        if _rec_cache["key"] == ck and _rec_cache["out"] and time.time() - _rec_cache["ts"] < 1800:
            return _rec_cache["out"]
    sug_all, genre_hints, stats = [], {}, {}
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ex = ThreadPoolExecutor(max_workers=4)
        try:
            # 熔断（v4.01）：冷却中的源**根本不提交**，一次网络都不发；
            # 冷却到期的只放一个探测（_brk_allow 里用 probing 标记挡住并发重复探测）。
            futs = {}
            for name, fn in srcs:
                if not _brk_allow(name):
                    _rec_stats["errors"][name] = "冷却中，已跳过"
                    stats[name] = 0
                    continue
                futs[ex.submit(fn)] = name
            try:
                for f in as_completed(futs, timeout=timeout + 1.5):
                    name = futs[f]
                    try:
                        res = f.result(timeout=timeout)
                    except Exception as e:
                        res = ([], {})
                        _rec_stats["errors"][name] = str(e)[:120]
                        _brk_fail(name, e)
                    else:
                        _brk_ok(name)
                    sugs, meta = res if isinstance(res, tuple) else (res, {})
                    stats[name] = len(sugs or [])
                    for s in (sugs or []):
                        if isinstance(s, dict) and s.get("artist"):
                            sug_all.append(s)
                    gh = (meta or {}).get("genre_hints") or {}
                    genre_hints.update(gh)
            except TimeoutError:
                # 没在时限内回来的源：算一次失败（同时把 probing 标记放开，
                # 否则那个源会卡在「探测中」再也放不进来）
                for f, name in futs.items():
                    if not f.done():
                        _brk_fail(name, "timeout")
        finally:
            _ex_cancel(ex)
    except Exception as e:
        _rec_stats["errors"]["recall"] = str(e)[:120]
    _rec_stats["last"] = stats
    out = (sug_all, {"genre_hints": genre_hints})
    with _rec_cache_lock:
        _rec_cache["key"], _rec_cache["out"], _rec_cache["ts"] = ck, out, time.time()
    return out


def _apply_genre_hints(rows, genre_hints):
    """用外部（Last.fm 等）给的真实曲风覆盖「标题关键词猜测」的曲风。"""
    for c in rows:
        a = _norm_key(c.get("artist") or "")
        g = genre_hints.get(a)
        if g:
            c["genre"] = g


def _safe_search(kw, limit=10, wait=8):
    """search() 的兜底包装：上游抽风/超时都只回 []，绝不让一个关键词的失败打断整池。"""
    try:
        return search(kw, limit=limit, wait=wait) or []
    except Exception:
        return []


def _resolve_external(sug_all, pool, seen, kw_w, cap=4):
    """外部「具体某首歌」建议的落地：已在池里的打来源标记，不在的定点解析一次；
    解析不到就丢（不造歌）。两种都保证最终列表 100% 在候选池内。

    cap（v4.00：14 → 4）：**这里才是每日推荐冷算 12 秒的最大来源**。
    每个「落地」都要一次 search()，而全局 上游站点 名额只有 2 个（`_lock`），
    单次上游 1~5 秒。14 个 × 平均 1.5 秒 ÷ 2 并发 ≈ 10 秒，全花在给外部建议
    「找落地」上 —— 可是每首只回 2 条结果，对最终 12 首的贡献极低（绝大多数
    外部建议本来就已经被主搜索捞进池子了，那一支只打标记、不联网）。
    降到 4 个，把锁留给真正决定结果的 6 个主关键词。
    """
    song_sugs = [s for s in sug_all if s.get("artist") and s.get("song")][:cap]
    if not song_sugs:
        return
    # 池里歌曲按 (歌手, 歌名) 归一建立索引，方便给「已被主搜索捞进来的外部建议」补来源标记
    idx = {}
    for c in pool:
        a = _norm_key(c.get("artist") or "")
        s = _norm_key(c.get("song") or c.get("name") or "")
        if a and s:
            idx[(a, s)] = c

    def _land(s):
        """返回落地后的候选（打标已有的 / 解析出来的 / None）"""
        ak = (_norm_key(s["artist"]), _norm_key(s["song"]))
        if ak in idx:
            c = idx[ak]
            c["source"] = s.get("source") or c.get("source") or ""
            c["_w"] = max(float(s.get("weight", 0.5)), float(c.get("_w") or 0.0))
            if s.get("genre"):
                c["genre"] = s["genre"]
            return None            # 已在池里，只补标记，不重复加
        q = "%s %s" % (s["artist"], s["song"])
        try:
            rs = search(q, limit=2, wait=2)
        except Exception:
            return None
        sn = _norm_key(s["song"])
        for r in (rs or []):
            rsong = _norm_key(r.get("song") or r.get("name") or "")
            rtitle = _norm_key(r.get("title") or "")
            if rsong == sn or sn in rtitle or rsong in sn:
                c = dict(r)
                c["_w"] = max(float(s.get("weight", 0.5)), float(c.get("_w") or 0.0))
                c["source"] = s.get("source") or c.get("source") or ""
                if s.get("genre"):
                    c["genre"] = s["genre"]
                return c
        return None

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ex = ThreadPoolExecutor(max_workers=max(1, min(4, cap)))
        try:
            futs = [ex.submit(_land, s) for s in song_sugs]
            try:
                for f in as_completed(futs, timeout=10):
                    try:
                        c = f.result()
                    except Exception:
                        c = None
                    if c and c.get("id") and c["id"] not in seen:
                        seen.add(c["id"])
                        pool.append(c)
            except TimeoutError:
                pass
        finally:
            _ex_cancel(ex)
    except Exception:
        pass


# ---- JY-THEMES: 多主题歌单（「很多不同主题的推荐卡片/歌单」） ----
# 组合「曲风聚类 + 固定场景 + 今日精选兜底」，产出 4~6 张主题歌单卡。
# 每张卡里的歌来自同一个候选池（源A搜索 + 本地曲库 + 外部召回），
# 只是按曲风/场景不同维度切片，所以仍 100% 在候选池内（严守不推池外歌的底线）。
def _lang_of(c):
    """粗粒度语种识别（书面，仅看歌名+歌手字面）：韩/日/华语/欧美。"""
    t = "%s %s" % (c.get("song") or c.get("name") or "", c.get("artist") or "")
    if re.search(r"[\uac00-\ud7a3]", t):
        return "韩"
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", t):
        return "日"
    if re.search(r"[\u4e00-\u9fff]", t):
        return "华语"
    return "欧美"


_THEME_SCENE = {
    "睡前": {"genres": {"纯音乐", "古典"}, "kw": ("睡", "眠", "夜", "梦", "安眠", "助眠", "晚安", "calm", "relax", "晚风", "哄睡")},
    "专注": {"genres": {"纯音乐"}, "kw": ("学习", "工作", "专注", "study", "focus", "轻音乐")},
    "运动": {"genres": {"电子", "摇滚", "说唱"}, "kw": ("运动", "健身", "跑步", "workout", "run", "gym", "party", "dance", "energy", "动感")},
    "治愈": {"genres": {"民谣", "纯音乐"}, "kw": ("治愈", "温暖", "comfort", "warm", "heal", "温柔", "晚安")},
    "怀旧": {"genres": {"民谣", "古典"}, "kw": ("怀旧", "青春", "回忆", "nostalg", "old", "classic", "老歌")},
    # 下面三个是「把卡数撑起来」补的场景分类（通勤 / 旅行这类通用场景）；
    # 命中不了就自动不出卡，不会留空壳。
    "通勤": {"genres": {"流行", "电子"}, "kw": ("通勤", "路上", "地铁", "公交", "开车", "drive", "commute", "radio", "电台")},
    "旅行": {"genres": {"民谣"}, "kw": ("旅行", "出发", "远方", "journey", "travel", "road", "旅途")},
    "独处": {"genres": {"纯音乐", "民谣"}, "kw": ("一个人", "独处", "安静", "alone", "quiet", "silence", "lonely")},
}


# QQT: 歌单标题库 —— 按「分类歌单」的口径起名（场景 + 语种 + 曲风）。
# QQ 的分类维度只有四类：**语种（国语/粤语/英语/日语/韩语）、场景（通勤/学习工作/
# 咖啡馆/运动/睡前/旅行/派对）、心情（治愈/伤感/励志/安静…）、流派（流行/电子/
# 民谣/说唱/摇滚/爵士/R&B…）**，卡面一律「主标题 + 一句副标题」
# （如「跑步音乐：运动必备活力节奏」「爵士咖啡茶」「轻音助眠：睡前解压」）。
# 所以这里把每张卡都做成「标题 + 副标题」两段式，并且**不再出现「X 循环」**那种机器味。
# 每个 key 给 2~3 个变体，按当天 seed 取一个：同一天刷多少次都长一样，隔天换个说法。
_JY_T = {
    "lang:华语": [("国语热歌", "今天最想听的那几首"), ("华语流行", "华语圈最近在听"),
                  ("华语金曲", "老歌新歌一起")],
    "lang:欧美": [("欧美热单", "从 Billboard 里挑的"), ("欧美流行", "英语歌里的常驻"),
                  ("西洋金曲", "从慢歌到舞曲")],
    "lang:日":   [("日系小调", "通勤和深夜都合适"), ("J-Pop 精选", "动漫与日剧的味道"),
                  ("日系治愈", "轻声细语那一类")],
    "lang:韩":   [("K-Pop 热单", "副歌一响就跟着走"), ("韩流精选", "打歌舞台上的那些")],
    "scene:睡前": [("睡前解压", "轻一点，别吵醒自己"), ("轻音助眠", "把音量调到 20"),
                   ("今晚早点睡", "慢歌陪你先躺下")],
    "scene:专注": [("学习工作", "不用听歌词的那种"), ("专注一小时", "背景音，不抢注意力"),
                   ("安静做事", "低干扰清单")],
    "scene:运动": [("跑步音乐", "运动必备活力节奏"), ("动感一小时", "踩点往前冲"),
                   ("健身燃脂", "BPM 拉满")],
    "scene:治愈": [("治愈一下", "温柔不算软弱"), ("抱抱自己", "听完这十首再说"),
                   ("慢一点也没关系", "情绪急救包")],
    "scene:怀旧": [("旧日时光", "那些年的单曲循环"), ("回忆放映厅", "一开口就是从前"),
                   ("老歌不老", "只是我们长大了")],
    "scene:通勤": [("通勤路上", "地铁公交的固定曲目"), ("赶路清单", "走得快一点也不慌"),
                   ("上班路上", "堵车也没那么烦")],
    "scene:旅行": [("旅行箱里的歌", "出发的时候放"), ("在路上", "窗外一直在变"),
                   ("远方的风", "适合长途")],
    "scene:独处": [("一个人听", "安安静静不打扰"), ("关灯之后", "只剩耳机和你"),
                   ("独处时刻", "不急着说话")],
    "genre:摇滚": [("摇滚现场", "音量调大一点"), ("吉他失真", "鼓点一直往前推")],
    "genre:民谣": [("民谣故事", "一把吉他讲完"), ("歌词比旋律更重", "都有个远方")],
    "genre:说唱": [("说唱现场", "flow 决定一切")],
    "genre:电子": [("电子脉冲", "合成器一层层叠上来")],
    "genre:爵士": [("爵士咖啡茶", "慵懒的午后"), ("深夜爵士", "一杯酒的时间")],
    "genre:古典": [("古典时光", "安静里也有力量")],
    "genre:纯音乐": [("无词纯音", "只留旋律")],
    "genre:现场": [("现场版", "观众一起唱的那几秒")],
    "genre:古风": [("国风一曲", "琵琶与笛子")],
    "genre:童声": [("童声", "干干净净的嗓子")],
    "genre:ACG": [("二次元", "番剧片头片尾")],
    "genre:伴奏": [("伴奏", "自己唱一遍")],
    "derive:today": [("今日私享", "按你的口味挑的")],
    "derive:discover": [("新发现", "刚挖到、还没听过的")],
    "derive:local": [("本地收藏", "来自你自己的曲库")],
    "derive:recent": [("最近常听", "播放次数最多的那几首")],
    "derive:random": [("随便听听", "不知道听什么就点这个")],
    "derive:mix": [("在线本地混着来", "一半新歌一半存货")],
    "derive:opposite": [("换个口味", "和你平时听的不太一样"), ("跳出循环", "全是没听过的歌手")],
    "genre:流行": [("流行速递", "副歌都能跟着哼"), ("口水歌单", "好记才传得开")],
    "genre:R&B": [("R&B 慢慢来", "拍子拖一点更舒服")],
}
# 歌手卡句式（轮换用）：QQ 的歌手向歌单也是「歌手 · 一句话」，不是「X 循环」
_JY_T_ARTIST = ["%s 精选", "%s 代表作", "%s 的歌单", "%s 电台"]


def _jy_hash(s):
    """稳定哈希（不依赖 PYTHONHASHSEED，跨进程同一天结果一致）。"""
    h = 0
    for ch in (s or ""):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def _pretty_artist(a):
    """歌手名排版：纯小写英文的按词首字母大写（文件名解析常给 "taylor swift"）。"""
    a = (a or "").strip()
    if a and re.fullmatch(r"[a-z0-9 .&'\-]+", a):
        return " ".join(w[:1].upper() + w[1:] for w in a.split(" "))
    return a


def _jy_title(key, seed, default=("", "")):
    """按 key + 当天 seed 从标题库里取一个变体；没有就走默认。"""
    vs = _JY_T.get(key)
    if not vs:
        return default
    return vs[_jy_hash((seed or "") + key) % len(vs)]


# ---------------- JY-TAGMAP: 前端标签 → 内部分类键 ----------------
# 前端「每日推荐」的 chip 是固定的 16 个中文词（RECTAGS）。它们必须真的影响结果，
# 所以在这里统一映射到 _JY_T 已经用惯的那套键名（lang: / genre: / scene: / derive:），
# 而不是另造一套 —— 两边各写一份，迟早对不上（中文标签改了，歌单标题却还在用旧键）。
#
# 口径（和前端约好，别改）：
#   · 只认 lang: / genre: 两类参与**歌曲层**加权（语种 + 曲风）；
#   · 老歌 / 热歌 没有对应的语种或曲风轴，就落到 scene:怀旧 / derive:today，
#     只在**歌单层**起作用（选中「老歌」时歌单里出现怀旧卡，而不是把歌名里带「老」的
#     歌全捞出来 —— 那样只会得到一堆乱码歌）。
_RECTAG_KEY = {
    "流行": "genre:流行", "华语": "lang:华语", "欧美": "lang:欧美",
    "日语": "lang:日", "韩语": "lang:韩", "粤语": "lang:粤语",
    "摇滚": "genre:摇滚", "民谣": "genre:民谣", "电子": "genre:电子",
    "古典": "genre:古典", "轻音乐": "genre:纯音乐", "爵士": "genre:爵士",
    "嘻哈": "genre:说唱", "ACG": "genre:ACG",
    "老歌": "scene:怀旧", "热歌": "derive:today",
}
# 粤语没法从歌名/歌手字面分辨（_lang_of 只会给 华语/欧美/日/韩），
# 按「粤语歌也是华语歌」处理：过滤歌单时当华语看，不然选中粤语会一张卡都不剩。
_LANG_ALIAS = {"粤语": "华语", "日": "日", "韩": "韩"}
# 动感曲风：选中它们时，年代/怀旧类的卡要让路（用户点名：「选中电子时不该出现老歌卡」）
_ENERGETIC_GENRES = ("电子", "摇滚", "说唱", "嘻哈")


def _tag_keys(tags):
    """中文标签 → 内部键（保序去重）。认不出来的忽略，不抛。"""
    keys = []
    for t in (tags or []):
        t = str(t).strip()
        if not t:
            continue
        k = _RECTAG_KEY.get(t)
        if not k and t.split(":", 1)[0] in ("lang", "genre", "scene", "derive"):
            k = t                      # 也接受内部键直传（测试/排障方便）
        if k and k not in keys:
            keys.append(k)
    return keys


def _tag_words(keys):
    """内部键 → 中文词（歌单层的关键词匹配用）。"""
    return [k.split(":", 1)[1] for k in (keys or []) if ":" in k]


def _cand_lang(c):
    return _lang_of(c)


def _tag_affinity(c, keys):
    """候选对选中标签的贴合度 0~1：命中几个标签轴算几个（语种轴 + 曲风轴各自独立）。

    v4.02：多一条「来源即证据」—— 这条候选本来就是**用这个标签当搜索词**捞回来的
    （`_by`），那它天然贴合，直接给满分。原因很实在：在线候选的 genre 字段几乎全是空，
    只靠 genre 判曲风等于永远判不中（实测选「摇滚」和「电子」返回的列表一模一样）。"""
    if not keys:
        return 0.0
    _by = str(c.get("_by") or "")
    if _by:
        for k in keys:
            if _by == k or _by == k.split(":", 1)[-1]:
                return 1.0
    hits = 0
    lang = "lang:" + _cand_lang(c)
    genre = c.get("genre") or ""
    for k in keys:
        if k.startswith("lang:"):
            want = _LANG_ALIAS.get(k.split(":", 1)[1], k.split(":", 1)[1])
            if want == lang.split(":", 1)[1]:
                hits += 1
        elif k.startswith("genre:"):
            g = k.split(":", 1)[1]
            if g and (g == genre or g in (c.get("genre") or "")):
                hits += 1
    return float(hits) / float(len(keys))


TAG_BONUS = 0.40      # 标签贴合度的加分上限。只在原排序上做加法，绝不推翻原有排序：
                      # 基础 rel 是 0.55·歌手口味 + 0.25·同名经验 + 0.12·来源可信度（0~1），
                      # 全命中加 0.40 足够把「对味的歌」提到前面，却挤不掉真正高相关的。


def _apply_tag_bonus(rows, tags):
    """把标签贴合度加进 rel。必须在 score_candidates + _apply_genre_hints 之后调用
    （不然曲风还没定下来，genre: 轴永远算不中）。只加分，不删歌、不降分。"""
    keys = _tag_keys(tags)
    if not keys:
        return rows
    for c in (rows or []):
        try:
            c["rel"] = round(float(c.get("rel") or 0.0) + TAG_BONUS * _tag_affinity(c, keys), 4)
        except Exception:
            pass
    return rows


def _theme_match(c, genres, kws, extra_kws=None):
    """主题命中：曲风优先，其次歌名/标题关键词。
    extra_kws = 标签词（选中「华语」「睡前」时让对应主题更容易出卡）。"""
    g = (c.get("genre") or "").lower()
    if g and g in genres:
        return True
    t = ((c.get("song") or c.get("name") or "") + " " + (c.get("title") or "")).lower()
    if any(k in t for k in kws):
        return True
    return any(k and k.lower() in t for k in (extra_kws or ()))


# 歌单卡 ↔ 标签的冲突表：选中这些标签时，这些卡明显跑题，直接不出。
_CARD_CONFLICT = {
    "genre:电子": ("scene:怀旧", "genre:古典", "genre:纯音乐", "scene:专注", "scene:睡前"),
    "genre:摇滚": ("scene:睡前", "genre:纯音乐"),
    "genre:说唱": ("scene:睡前", "genre:古典"),
    "genre:古典": ("scene:运动",),
    "lang:华语": ("lang:欧美", "lang:日", "lang:韩"),
    "lang:欧美": ("lang:华语", "lang:粤语", "lang:日", "lang:韩"),
    "lang:日": ("lang:欧美",),
    "lang:韩": ("lang:欧美",),
}


def _card_drops(card, tags):
    """这张卡在给定标签下该不该丢掉（歌单层的「标签真的有用」就靠它）。"""
    keys = _tag_keys(tags)
    if not keys:
        return False
    ck = str(card.get("key") or "")
    # ① 明确冲突的卡（电子 → 老歌/古典/睡前）
    for k in keys:
        if ck in _CARD_CONFLICT.get(k, ()):
            return True
    words = _tag_words(keys)
    energetic = [w for w in words if w in _ENERGETIC_GENRES]
    # ② 动感曲风 + 年代/怀旧/老歌主题卡 → 让路
    if energetic and (":era" in ck or "老歌" in str(card.get("title") or "")
                      or "怀旧" in ck):
        return True
    # ③ 选了语种：这个语种的歌不到一半的卡不要（主题卡尤其明显：
    #    选「华语」时不该出现一堆日语动漫 OST）
    langs = [k.split(":", 1)[1] for k in keys if k.startswith("lang:")]
    songs = card.get("songs") or []
    if langs and songs:
        want = set(_LANG_ALIAS.get(x, x) for x in langs)
        hit = sum(1 for c in songs if _cand_lang(c) in want)
        if hit * 2 < len(songs):
            return True
    # ④ 选了曲风：同轴（genre:）的其他曲风卡不要
    gsel = [k for k in keys if k.startswith("genre:")]
    if gsel and ck.startswith("genre:") and ck not in gsel:
        return True
    return False


def _filter_cards_by_tags(cards, tags):
    """按标签筛歌单卡：纯内存操作（不重搜上游），切换/取消标签都是毫秒级。"""
    if not cards:
        return cards or []
    if not _tag_keys(tags):
        return cards
    return [c for c in (cards or []) if not _card_drops(c, tags)]


# ==================== JY-TAGLEAD（v4.02）====================
# 「选中标签后列表/卡片不变」的收口。原来的做法只有两条路：
#   ① _apply_tag_bonus 加分 —— 但在线候选 genre 基本是空，亲和度恒 0，等于没加；
#   ② _filter_cards_by_tags 过滤卡 —— 它只判「冲突」，不要求「匹配」，
#      所以「万圣节」「毕业」在摇滚/电子下都活得好好的。
# 现在的原则：**只把对味的提到前面，绝不删歌**（标签料本来就不多，
# 硬砍会砍成空列表；用户要的是「前几首一定对味」）。
_TAG_ALIAS_WORDS = {
    "轻音乐": ("纯音乐", "轻音乐"),
    "嘻哈": ("说唱", "嘻哈"),
    "ACG": ("ACG", "动漫"),
    "国风": ("古风", "国风"),
    "古风": ("古风", "国风"),
}


def _tag_tokens(tags):
    """标签 → 字面比对词表（含别名/内部键尾段）。认不出来就是空集，不抛。"""
    toks = set()
    for t in (tags or []):
        t = str(t).strip()
        if not t:
            continue
        toks.add(t)
        k = _RECTAG_KEY.get(t) or ""
        if ":" in k:
            toks.add(k.split(":", 1)[1])
        for a in _TAG_ALIAS_WORDS.get(t, ()):
            toks.add(a)
    for k in _tag_keys(tags):
        if ":" in k:
            toks.add(k.split(":", 1)[1])
    return set(x for x in toks if x)


def _row_hits_tag(c, tags):
    """一条候选算不算「贴合标签」：按标签搜来的（_by）或 歌名/歌手/曲风字面命中。"""
    if not isinstance(c, dict):
        return False
    toks = _tag_tokens(tags)
    if not toks:
        return False
    by = str(c.get("_by") or "")
    if by and by in toks:
        return True
    blob = " ".join([str(c.get("song") or c.get("title") or c.get("name") or ""),
                     str(c.get("artist") or ""), str(c.get("genre") or "")])
    return any(t in blob for t in toks)


def _tag_lead(rows, tags):
    """贴合标签的候选**稳定前置**，其余原样跟后面。不删、不截断。"""
    if not rows or not _tag_keys(tags):
        return rows
    hit, rest = [], []
    for c in rows:
        (hit if _row_hits_tag(c, tags) else rest).append(c)
    if not hit:
        return rows
    return hit + rest


def _card_hits_tag(card, tags):
    """这张卡算不算贴合标签：标题/描述/键字面命中，或过半的歌命中。"""
    toks = _tag_tokens(tags)
    if not toks:
        return True
    blob = " ".join([str(card.get("title") or ""), str(card.get("desc") or ""),
                     str(card.get("key") or "")])
    if any(t in blob for t in toks):
        return True
    songs = card.get("songs") or []
    if songs:
        n = sum(1 for c in songs if _row_hits_tag(c, tags))
        return n * 2 >= len(songs)
    return False


def _card_lead(cards, tags):
    """卡片按标签前置。命中够 2 张就只给命中的（v4.02b：原来是 4，
    可主题卡让路后一回只剩 4~6 张，4 的门槛经常跨不过去，结果还是「补齐」糊满）；
    不够就「命中的在前、其余补齐」—— 不给空屏，也不拿一堆无关卡糊满。"""
    if not cards or not _tag_keys(tags):
        return cards
    hit, rest = [], []
    for c in cards or []:
        (hit if _card_hits_tag(c, tags) else rest).append(c)
    if not hit:
        return cards
    if len(hit) >= 2:
        return hit
    return hit + rest


def _local_tag_order(rows, tags):
    """本地段排序：贴合标签的在前，**曲风字段明确是别的曲风**的挪到最后。

    只在选了曲风标签时动手；没 genre 的歌一律当「中性」保持原序 ——
    本地曲库大多没有 genre，靠它排序会误伤一大片。"""
    if not rows or not _tag_keys(tags):
        return rows
    gsel = [k.split(":", 1)[1] for k in _tag_keys(tags) if k.startswith("genre:")]
    if not gsel:
        return _tag_lead(rows, tags)
    hit, neu, bad = [], [], []
    for c in rows:
        g = str(c.get("genre") or "")
        if g and not any(x and x in g for x in gsel):
            bad.append(c)
        elif _row_hits_tag(c, tags):
            hit.append(c)
        else:
            neu.append(c)
    if not bad:
        return hit + neu
    return hit + neu + bad


# ==jy_card_proof== 逐首主题校对（v4.30）：歌单卡里的歌必须真贴合主题
# 不贴的（靠标签词混进来的、曲风明显矛盾的）直接剔掉；校完不足 3 首的整卡丢，
# 别留一张「主题歌单」里塞两首跑题歌的残缺卡。derive 卡是「今日/新发现」这类
# 混合清单，本来就不该有单一主题，不校。
_SCENE_CALM = ("睡前", "专注", "治愈", "独处")   # 该静的卡：电子/摇滚/说唱/嘻哈明显跑题
_SCENE_ENER = ("运动",)                          # 该动的卡：纯音乐/古典/民谣明显跑题


def _card_song_fits(c, key):
    """逐首主题校对：c 该不该留在 key 这张卡里。"""
    if not isinstance(c, dict):
        return False
    if key.startswith("derive:"):
        return True
    if key.startswith("genre:"):
        return (c.get("genre") or "") == key.split(":", 1)[1]
    if key.startswith("lang:"):
        lg = key.split(":", 1)[1]
        return _lang_of(c) == _LANG_ALIAS.get(lg, lg)
    if key.startswith("artist:"):
        return _norm_key(c.get("artist") or "") == key.split(":", 1)[1]
    if key.startswith("scene:"):
        s = key.split(":", 1)[1]
        h = _THEME_SCENE.get(s)
        if not h:
            return True
        if not _theme_match(c, h["genres"], h["kw"]):
            return False
        g = (c.get("genre") or "").strip()
        if g:
            if s in _SCENE_CALM and g in _ENERGETIC_GENRES:
                return False
            if s in _SCENE_ENER and g in ("纯音乐", "古典", "民谣"):
                return False
        return True
    return True


def build_themed_playlists(pool, local_rows, prof=None, daily_seed="", want=12, tags=None):
    """从候选池产出主题歌单卡（默认最多 12 张）。返回 [{key,title,desc,songs:[...]}]。

    want 解释：前端左栏是 2 列网格，铺满 3 行要 6 张；多出来的收在「展开全部」后面。
    以前固定 6 张、前端只铺 4 张，左栏两行就到底、右栏二十多首还在往下长 ——
    左右高度差出两百多像素，看着就是「不对称」。现在给 12 张，前端铺 6 张正好齐平。

    tags：用户选中的标签（中文词）。歌单层要真的受它影响（用户点名
    「特别是歌单」）：命中标签的主题排在前面，跑题的卡直接不出。
    """
    tag_keys = _tag_keys(tags)
    tag_words = _tag_words(tag_keys)
    comb = []
    for c in (pool or []):
        cc = dict(c); cc["online"] = True; comb.append(cc)
    for c in (local_rows or []):
        cc = dict(c); cc["online"] = False; comb.append(cc)
    # 去重：同 歌手+歌名 只留分高的那份（在线/本地二选一）
    seen = {}
    for c in comb:
        k = (_norm_key(c.get("artist") or ""), _norm_key(c.get("song") or c.get("name") or ""))
        if not k[1]:
            continue
        if k not in seen or (c.get("rel") or 0) > (seen[k].get("rel") or 0):
            seen[k] = c
    items = list(seen.values())
    if not items:
        return []
    # 1) 曲风主题（识别出的曲风，≥3 首）
    by_genre = {}
    for c in items:
        g = c.get("genre") or ""
        if g:
            by_genre.setdefault(g, []).append(c)
    genre_themes = [(g, r) for g, r in sorted(by_genre.items(), key=lambda kv: -len(kv[1]))
                    if len(r) >= 3]
    # 2) 场景主题（睡前/专注/运动/治愈/怀旧，≥3 首）
    scene_themes = []
    for name, h in _THEME_SCENE.items():
        rows = [c for c in items if _theme_match(c, h["genres"], h["kw"], extra_kws=tag_words)]
        if len(rows) >= 3:
            scene_themes.append((name, rows))
    # 3) 语种主题（华语/欧美/日韩，≥3 首）
    lang_themes = []
    for lg in ("华语", "欧美", "韩", "日"):
        rows = [c for c in items if _lang_of(c) == lg]
        if len(rows) >= 3:
            lang_themes.append((lg, rows))
    # 4) 歌手主题（同一歌手 ≥3 首）
    by_artist = {}
    for c in items:
        a = _norm_key(c.get("artist") or "")
        if a:
            by_artist.setdefault(a, []).append(c)
    artist_themes = [(a, r) for a, r in sorted(by_artist.items(), key=lambda kv: -len(kv[1]))
                     if len(r) >= 3]
    # 5) 派生主题（保证最终 4~6 张）
    online_rows = [c for c in items if c.get("online")]
    local_only = [c for c in items if not c.get("online")]
    top_all = sorted(items, key=lambda c: -(c.get("rel") or 0))
    # 最近常听：播放记录里仍在候选池里的歌（真听过的，比「猜」有说服力）
    rec_rows = []
    try:
        rn = set()
        for h in recent_played(12):
            _a, _s = split_title(h)
            rn.add(_norm_key(_s or h))
        rec_rows = [c for c in items
                    if _norm_key(c.get("song") or c.get("name") or "") in rn]
        rec_rows.sort(key=lambda c: -(c.get("rel") or 0))
    except Exception:
        rec_rows = []

    # ---- 分维度建候选，每个维度都带上「标题 + 副标题」两段式 ----
    def _mk(key, rows, fallback_title, fallback_desc):
        t, d = _jy_title(key, daily_seed, (fallback_title, fallback_desc))
        return (key, t or fallback_title, t or fallback_title, d or fallback_desc, rows)

    scene_c = [_mk("scene:" + n, r, "%s场景" % n, "适合现在的状态") for n, r in scene_themes]
    lang_c = [_mk("lang:" + lg, r, "%s歌单" % lg, "按语种挑的") for lg, r in lang_themes]
    genre_c = [_mk("genre:" + g, r, g, "同一类曲风") for g, r in genre_themes]
    artist_c = []
    used_pad = set()
    for a, r in artist_themes[:6]:
        h0 = _jy_hash((daily_seed or "") + a)
        for k in range(len(_JY_T_ARTIST)):          # 撞了就顺延，别两张卡同一个句式
            pi = (h0 + k) % len(_JY_T_ARTIST)
            if pi not in used_pad:
                used_pad.add(pi)
                break
        pad = _JY_T_ARTIST[pi]
        an = _pretty_artist(a)
        artist_c.append(("artist:%s" % a, pad % an, pad % an,
                         "共 %d 首" % len(r), r))
    derive_c = []
    # 随便听听：按当天 seed 稳定打乱（同一天刷新顺序不变，隔天换一批）
    rnd_rows = sorted(items, key=lambda c: _jy_hash((daily_seed or "") + "r" +
                                                    str(c.get("id") or c.get("name") or "")))
    # 在线本地混着来：各取一半再交错，光听在线会漏掉自己曲库
    _cx = []
    _on = sorted(online_rows, key=lambda c: -(c.get("rel") or 0))[:6]
    _lo = sorted(local_only, key=lambda c: -(c.get("rel") or 0))[:6]
    for _i in range(max(len(_on), len(_lo))):
        if _i < len(_on):
            _cx.append(_on[_i])
        if _i < len(_lo):
            _cx.append(_lo[_i])
    # 换个口味：最近没听过的歌手，按分排（打破「天天同一个人」的信息茧房）
    _hist_a = set()
    try:
        for _h in recent_played(30):
            _a2, _s2 = split_title(_h)
            if _a2:
                _hist_a.update(_art_list({"artist": _a2}) or [_norm_key(_a2)])
    except Exception:
        pass
    # 合唱也算「听过这个人」：只要有一个人撞上就排掉，不然「A&B」会绕过整张排除表
    opp_rows = sorted([c for c in items
                       if not any(x in _hist_a for x in _art_list(c))],
                      key=lambda c: -(c.get("rel") or 0))[:12]
    for key, rows in (("derive:today", top_all[:12]),
                      ("derive:discover", sorted(online_rows, key=lambda c: -(c.get("rel") or 0))[:12]),
                      ("derive:local", sorted(local_only, key=lambda c: -(c.get("rel") or 0))[:12]),
                      ("derive:recent", rec_rows[:12]),
                      ("derive:mix", _cx[:12]),
                      ("derive:opposite", opp_rows[:12]),
                      ("derive:random", rnd_rows[:12])):
        t, d = _jy_title(key, daily_seed)
        if t and len(rows) >= 3:
            derive_c.append((key, t, t, d, rows))

    # ---- 挑卡：每个维度封顶，避免「三张都是歌手」「三张都是语种」那种单调 ----
    # 场景/语种/流派/歌手/派生 各自上限；都按歌数多的优先。
    # 上限随 want 等比放大。派生维度放宽（基准 4）——它是唯一能稳定出卡的维度：
    # 场景/流派都要歌名里有对应关键词，池子里本来就少；派生（今日/新发现/本地/最近/
    # 混搭/换口味/随便听听）什么都依赖，池子一有货就能出。
    buckets = [scene_c, lang_c, genre_c, artist_c, derive_c]
    _k = max(1.0, float(want) / 6.0)
    caps = [max(1, int(round(c * _k))) for c in (2, 3, 2, 2, 4)]
    # JY-TAGMAP: 选中语种 → 语种桶先挑；选中曲风 → 曲风桶先挑。
    # 只改「先挑谁」，不改封顶数，池子小的时候照样能被别的维度补满。
    if any(k.startswith("lang:") for k in tag_keys):
        _i = buckets.index(lang_c)
        buckets.insert(0, buckets.pop(_i))
        caps.insert(0, caps.pop(_i))
    elif any(k.startswith("genre:") for k in tag_keys):
        _i = buckets.index(genre_c)
        buckets.insert(0, buckets.pop(_i))
        caps.insert(0, caps.pop(_i))
    # 跑题的卡先剔掉（电子 → 老歌/古典/睡前 那类），别占着封顶名额
    if tag_keys:
        for _bi, _b in enumerate(buckets):
            buckets[_bi] = [it for it in _b
                            if not _card_drops({"key": it[0], "title": it[1],
                                                "songs": it[4]}, tags)]
    take, used, used_titles = [], set(), set()
    for bi, b in enumerate(buckets):
        got = []
        for it in sorted(b, key=lambda x: -len(x[4])):
            if len(got) >= caps[bi] or len(it[4]) < 3:
                continue
            if it[0] in used or it[1] in used_titles:
                continue
            used.add(it[0]); used_titles.add(it[1]); got.append(it)
        take.append(got)
    # 轮转交错取：不按维度整块排，卡片之间才有「错落」感
    picks = []
    while len(picks) < want and any(take):
        for b in take:
            if b and len(picks) < want:
                picks.append(b.pop(0))
    # 还不够 4 张就放宽：把所有没被标题占用的候选按歌数补齐
    if len(picks) < min(4, want):
        rest = [it for b in (derive_c, lang_c, artist_c, scene_c, genre_c)
                for it in b if it[0] not in used and len(it[4]) >= 3]
        for it in sorted(rest, key=lambda x: -len(x[4])):
            if len(picks) >= min(4, want):
                break
            used.add(it[0]); picks.append(it)
    out = []
    for key, name, title, desc, rows in picks:
        rows2 = _dedupe_titles(sorted(rows, key=lambda c: -(c.get("rel") or 0)), 10 ** 6)
        # 歌手卡放宽每歌手上限（本来就是要听这个人的歌）
        if key.startswith("artist:"):
            ranked = mmr_rerank(rows2, lambda_=0.6, daily_seed=daily_seed, limit=12,
                                max_per_artist=10, max_per_genre=10)
        else:
            ranked = mmr_rerank(rows2, lambda_=0.6, daily_seed=daily_seed, limit=12,
                                max_per_artist=2, max_per_genre=3)
        ranked = _dedupe_titles(ranked, 10)
        # ==jy_card_proof== 逐首主题校对：把跑题的歌剔出本卡
        ranked = [c for c in ranked if _card_song_fits(c, key)]
        songs = [_strip_priv(c) for c in ranked]
        if len(songs) >= 3:
            out.append({"key": key, "title": title, "name": name,
                        "desc": desc or "", "songs": songs})
    # 左栏是 2 列网格：奇数张会在最后一行留一张孤卡（右边空一块），砍到偶数。
    # 挑卡是轮转取的，最后一张优先级最低，砍它损失最小。
    if len(out) >= 4 and len(out) % 2:
        out = out[:-1]
    # 最后再按标签过一遍：挑卡阶段是先剔后挑，这里兜住「轮转补位」带进来的跑题卡。
    out = _filter_cards_by_tags(out, tags)
    if len(out) >= 4 and len(out) % 2:
        out = out[:-1]
    return out


# ==================== JY-TOPIC 每日主题歌单：按节日/热点直接搜歌 ====================
# 思路（2026-10-29 用户提出）：日推不必围着「你最近听了什么」聚类，那样池子永远被
# 最近那几个人占满。直接**按当天的节日 / 热点关键词去网上搜歌**，一个关键词汇总成
# 一张歌单就行 —— 搜「咒术回战」直接出 SPECIALZ / LOST IN PARADISE / 廻廻奇譚，
# 搜「抖音热歌」出孤勇者 / 一路生花。主题驱动的多样性是天然的。
#
# 硬限制（必须记住）：源A**没有热度/播放量字段**，「最近什么最火」没法自动感知。
# 所以「热点」这一段是**人工维护的关键词清单**（动漫/影视/游戏/榜单/场景/年代），
# 靠日期轮换，不是实时榜单。想上真榜单得另接数据源（见文档「未完成」）。

# 公历节日：(月,日) -> (搜索词, 副标题)
_JY_FEST = {
    (1, 1):   ("新年好", "跨年那一刻放的"),
    (2, 14):  ("情人节", "两个人一起听"),
    (3, 8):   ("三八妇女节", "送她的歌"),
    (4, 1):   ("愚人节", "开个玩笑"),
    (5, 1):   ("劳动节", "放假了"),
    (6, 1):   ("儿童节", "小时候听过的"),
    (9, 10):  ("教师节", "谢谢老师"),
    (10, 1):  ("国庆", "长假出门路上听"),
    (10, 31): ("万圣节", "万圣夜"),
    (11, 11): ("光棍节", "一个人也挺好"),
    (12, 24): ("平安夜", "圣诞前夜"),
    (12, 25): ("圣诞节", "铃儿响叮当那一类"),
    (12, 31): ("跨年", "今年最后一晚"),
}

# 农历节日：逐年补表（只列春节/元宵/端午/七夕/中秋这几个「能搜出歌」的）。
# 缺年份不影响运行 —— 那天就少一张节日卡，其余照常。
_JY_LUNAR = {
    2026: [(2, 17, "春节", "新年第一首"), (3, 3, "元宵节", "灯会与汤圆"),
           (6, 19, "端午节", "粽子"), (8, 19, "七夕", "牛郎织女"),
           (9, 25, "中秋节", "月亮与团圆")],
    2027: [(2, 6, "春节", "新年第一首"), (2, 20, "元宵节", "灯会与汤圆"),
           (6, 9, "端午节", "粽子"), (8, 8, "七夕", "牛郎织女"),
           (9, 15, "中秋节", "月亮与团圆")],
    2028: [(1, 26, "春节", "新年第一首"), (2, 9, "元宵节", "灯会与汤圆"),
           (5, 28, "端午节", "粽子"), (8, 26, "七夕", "牛郎织女"),
           (10, 3, "中秋节", "月亮与团圆")],
}

# 常青主题池：(分组, 搜索词, 副标题)。每组每天按日期哈希抽一个 → 同天稳定、隔天换。
# 挑选标准只有两条：① 这个词在源A能搜出真歌；② 词本身就是一张歌单的名字。
_JY_TOPIC_POOL = [
    # --- 动漫（用户点名的那类：「最近火的动漫」） ---
    ("anime", "咒术回战", "热门动漫主题曲"),
    ("anime", "鬼灭之刃", "红莲华那一类"),
    ("anime", "进击的巨人", "世界观很重的那些"),
    ("anime", "间谍过家家", "轻松一点"),
    ("anime", "我推的孩子", "YOASOBI 那首"),
    ("anime", "葬送的芙莉莲", "安静又辽阔"),
    ("anime", "孤独摇滚", "乐队番"),
    ("anime", "电锯人", "带点噪音"),
    ("anime", "JOJO的奇妙冒险", "替身使者"),
    ("anime", "名侦探柯南", "剧场版主题曲"),
    ("anime", "灌篮高手", "直到世界尽头"),
    ("anime", "排球少年", "热血运动番"),
    ("anime", "你的名字", "新海诚"),
    ("anime", "千与千寻", "久石让"),
    ("anime", "海贼王", "航海王"),
    ("anime", "新世纪福音战士", "残酷天使"),
    # --- 华语影视 ---
    ("drama", "琅琊榜", "影视原声"),
    ("drama", "甄嬛传", "宫里的那些曲子"),
    ("drama", "庆余年", "古装剧"),
    ("drama", "繁花", "九十年代上海"),
    ("drama", "漫长的季节", "东北往事"),
    ("drama", "隐秘的角落", "爬山那部"),
    ("drama", "想见你", "穿越循环"),
    ("drama", "三生三世十里桃花", "仙侠"),
    # --- 游戏 ---
    ("game", "原神", "提瓦特的风"),
    ("game", "崩坏星穹铁道", "银河列车"),
    ("game", "塞尔达传说", "海拉鲁"),
    ("game", "最终幻想", "FF 系列"),
    ("game", "仙剑奇侠传", "国产仙侠"),
    ("game", "魔兽世界", "艾泽拉斯"),
    # --- 榜单 / 热歌 ---
    # 「播放量最高的歌」没有榜单数据可拿，最稳的近似是**搜热门歌手**：搜「周杰伦」回来
    # 的就是他的代表作，天然是「大家都在听的那几首」，而且不会像「日系热歌」这种泛词
    # 那样搜回一堆小众投稿。少数几个概念词（抖音热歌/经典老歌）实测可用，留着换口味。
    ("hot", "周杰伦", "华语天王"),
    ("hot", "林俊杰", "情歌王子"),
    ("hot", "陈奕迅", "国粤双修"),
    ("hot", "邓紫棋", "高音现场"),
    ("hot", "五月天", "青春和热血"),
    ("hot", "李荣浩", "都会情歌"),
    ("hot", "薛之谦", "苦情"),
    ("hot", "米津玄师", "日系顶流"),
    ("hot", "YOASOBI", "小说改成歌"),
    ("hot", "宇多田光", "J-Pop 经典"),
    ("hot", "Aimer", "嗓音独特"),
    ("hot", "Taylor Swift", "欧美流行"),
    ("hot", "Ed Sheeran", "一把吉他"),
    ("hot", "Adele", "灵魂嗓"),
    ("hot", "Bruno Mars", "放克律动"),
    ("hot", "BTS", "K-Pop 顶点"),
    ("hot", "BLACKPINK", "K-Pop 女团"),
    ("hot", "抖音热歌", "最近被刷屏的"),
    ("hot", "经典老歌", "听不腻的"),
    ("hot", "粤语经典", "港乐黄金年代"),
    # --- 场景 ---
    ("scene", "雨天", "下雨天听"),
    ("scene", "深夜", "关灯之后"),
    ("scene", "开车", "长途路上"),
    ("scene", "跑步", "踩点往前冲"),
    ("scene", "咖啡馆", "下午三点"),
    ("scene", "失眠", "睡不着的时候"),
    ("scene", "毕业", "散伙饭那晚"),
    ("scene", "旅行", "窗外一直在变"),
    # --- 年代（用「年代代表歌手」代替「90年代金曲」这类泛词：
    #     泛词搜回来的是杂烩，歌手名搜回来才是那个年代的歌） ---
    ("era", "邓丽君", "永远的邓丽君"),
    ("era", "张国荣", "哥哥"),
    ("era", "beyond", "摇滚与理想"),
    ("era", "小虎队", "青春偶像"),
    ("era", "张信哲", "情歌"),
    ("era", "任贤齐", "九十年代末"),
    ("era", "周华健", "朋友"),
    ("era", "罗大佑", "民谣与时代"),
    ("era", "王菲", "另类天后"),
    ("era", "张学友", "歌神"),
    ("era", "梅艳芳", "百变"),
    ("era", "陈慧娴", "港乐"),
]

# 分组给标题加个后缀，看着更像歌单名（榜单/场景/年代本身就是歌单名，不用加）
_JY_TOPIC_SUF = {"anime": " OST", "drama": " 影视原声", "game": " 游戏原声"}

_topic_cache = {"day": "", "pls": None}
_topic_lock = threading.Lock()

# ---- 每日推荐落盘：跨重启/部署不冷启动，过当天自动失效 ----
_daily_disk_loaded = False
_DAILY_DISK = os.path.join(CACHE_DIR, "daily_rec.json")
def _disk_key_is_default(key):
    """落盘的 key 只保「默认槽」（无标签）。理由：
      · 首屏第一次请求永远是默认口径（用户还没点标签），它才值得跨重启复用；
      · 带标签的结果只对「恰好选了这几个标签」的那一次请求有用，留着重启后也没人问；
      · 更重要的：磁盘里只放一份，如果最新一份是带标签的，默认首屏反而会用不上。
    key 的分段：日期|kw|tags|limit|seed|ev|likes|tagmode
    """
    parts = str(key or "").split("|")
    return len(parts) >= 3 and parts[2] == ""


def _load_daily_disk():
    """启动/缓存冷时把磁盘里「当天」的每日推荐读进内存，首屏秒回；跨天则删除旧档。"""
    global _daily_disk_loaded
    _daily_disk_loaded = True
    try:
        if not os.path.exists(_DAILY_DISK): return
        with open(_DAILY_DISK, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") != time.strftime("%Y%m%d"):
            try: os.remove(_DAILY_DISK)
            except Exception: pass
            return
        if not _disk_key_is_default(d.get("key")):
            return                     # 旧档/带标签的档不用（见 _disk_key_is_default）
        with _data_lock:
            _daily_cache["key"] = d.get("key", "")
            _daily_cache["out"] = d.get("out")
            _daily_cache["all"] = d.get("all")
        # 落盘的结果同时放进多槽缓存：进程重启后「默认槽」也能秒回
        if d.get("key") and d.get("out"):
            try:
                _slot_put(d.get("key"), d.get("out"), d.get("all"))
            except Exception:
                pass
        if d.get("topic_day") == time.strftime("%Y%m%d") and d.get("topic_pls") is not None:
            with _topic_lock:
                _topic_cache["day"], _topic_cache["pls"] = d["topic_day"], d["topic_pls"]
    except Exception:
        pass
_DAILY_DISK_MIN_GAP = 30.0      # 两次落盘的最小间隔（秒）：冷算成功要立刻落盘，
                                # 但同一秒里几个 key 连着算完时没必要写 N 遍 JSON
_daily_disk_last = [0.0]


def _save_daily_disk(force=False):
    """把当日推荐 / 主题卡写盘（跨重启不用重算）。

    force=True：预热线程每 900 秒那一次，必须真写（哪怕刚写过）。
    默认：带 30 秒节流，给「冷算成功后立即落盘」用。
    只写「默认槽」：见 _disk_key_is_default（磁盘里只放一份）。
    """
    try:
        _now = time.time()
        if not force and (_now - (_daily_disk_last[0] or 0.0)) < _DAILY_DISK_MIN_GAP:
            return
        _daily_disk_last[0] = _now
        today = time.strftime("%Y%m%d")
        with _data_lock:
            _k = _daily_cache.get("key", "")
            if not _disk_key_is_default(_k):
                return
            payload = {"date": today, "key": _k,
                       "out": _daily_cache.get("out"), "all": _daily_cache.get("all")}
        with _topic_lock:
            if _topic_cache.get("pls") is not None:
                payload["topic_day"] = _topic_cache.get("day", "")
                payload["topic_pls"] = _topic_cache.get("pls")
        with open(_DAILY_DISK, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def _cross_dedupe_cards(cards):
    """跨卡去重：前一张卡里出现过的歌，后面的卡里摘掉（和 recommend() 里同一套口径）。
    摘到不足 4 首的卡整张丢掉。**返回新 dict**，不改动传进来的卡对象
    （主题卡是全天内共享的缓存对象，就地改会污染下一次请求）。"""
    seen, out = set(), []
    for p in (cards or []):
        ss = []
        for c in (p.get("songs") or []):
            try:
                tk = (jy_norm(c.get("song") or c.get("name") or ""),
                      jy_norm(c.get("artist") or ""))
            except Exception:
                continue
            if not tk[0] or tk in seen:
                continue
            seen.add(tk)
            ss.append(c)
        if len(ss) < 4:
            continue
        q = dict(p)
        q["songs"] = ss
        if re.search(r"·\s*\d+\s*首$", str(q.get("desc") or "")):
            q["desc"] = re.sub(r"·\s*\d+\s*首$", "· %d 首" % len(ss), q["desc"])
        out.append(q)
    return out


def _ensure_topics_in(out, tags=None):
    """缓存命中时补齐「主题卡」。

    为什么需要它：进程刚起（或跨天）时，推荐和主题卡是并行算的 ——
    推荐先算完、主题卡后算完，那时 `_merge_topic_into_cache` 找不到已缓存的结果
    （缓存还是空的），合并就被跳过了。结果：当天默认槽里一张主题卡都没有。
    所以在**命中路径**上补一次：纯内存操作，毫秒级；已经有的卡不会重复加。

    tags：用请求的 tags 过滤一遍 —— key 里带着 tags，命中的这份结果当初就是用
    这组标签构建的，过滤口径完全对得上（所以带标签的槽也不会被塞进跑题卡）。
    """
    # v4.02b：请求带了标签就**不要**再补主题卡。「万圣节」「毕业」「仙侠原声」
    # 这些跟曲风无关，补上去就是用户说的「和标签不符」。
    if tags:
        return out
    try:
        _pls = _topic_cache.get("pls")          # 列表整体替换，读到旧值最多多合并一次
        if not _pls or _topic_cache.get("day") != time.strftime("%Y%m%d"):
            return out
        cur = list(out.get("playlists") or [])
        have = set(str(p.get("key") or "") for p in cur)
        add = [p for p in _filter_cards_by_tags(_pls, tags)
               if str(p.get("key") or "") not in have]
        if not add:
            return out
        merged = _cross_dedupe_cards(add + cur)[:10]
        if len(merged) >= 4 and len(merged) % 2:
            merged = merged[:-1]
        out["playlists"] = merged
    except Exception:
        pass
    return out


def _merge_topic_into_cache(new_pls):
    """把新算出的主题卡合并进**所有**已缓存的推荐结果，key 一律保留。

    为什么不能整把作废：详见 _topic_build 里的注释（跨天连卡两次的根因）。
    合并完再统一做一次跨卡去重 —— 原来的口味卡里可能有和主题卡重复的歌。
    返回实际被合并的结果份数。
    """
    if not new_pls:
        return 0
    outs = []
    with _data_lock:
        _o = _daily_cache.get("out")
        if isinstance(_o, dict):
            outs.append(_o)
        for _e in (_slots().values() if isinstance(_daily_cache.get("slots"), dict) else ()):
            _o = (_e or {}).get("out")
            if isinstance(_o, dict) and not any(_o is x for x in outs):
                outs.append(_o)
    n = 0
    for o in outs:
        pl = list(o.get("playlists") or [])
        have = set(str(p.get("key") or "") for p in pl)
        add = [p for p in new_pls if str(p.get("key") or "") not in have]
        if not add:
            continue
        merged = _cross_dedupe_cards(add + pl)[:10]
        if len(merged) >= 4 and len(merged) % 2:
            merged = merged[:-1]
        o["playlists"] = merged
        n += 1
    return n

# 主题卡的质量闸。泛词（日系热歌 / 秋天）搜回来的东西很杂：PC 音效、营销标题
# （「免费|睡眠音乐…」）、八音盒版、几十秒的片段。这些混在一张歌单里，整页看着就像坏了。
# 只挡「明确不该出现」的，宁少勿滥 —— 少几首不影响成卡，脏一首毁整张。
_JY_TOPIC_BAD = ("效果音", "免费|", "音乐盒", "八音盒", "オルゴール", "铃声", "彩铃",
                 "试听", "片段", "广告", "白噪音", "asmr", "催眠曲", "合辑", "合集",
                 "抖音最火", "连播", "一小时", "独奏", "演奏曲", "翻自", "演唱《",
                 "演唱会", "串烧", "颁奖", "歌友会", "现场版", "dj版")


def _topic_junk(c):
    s = c.get("song") or ""
    t = (s + " " + (c.get("artist") or "") + " " + (c.get("title") or "")).lower()
    if any(m in t for m in _JY_TOPIC_BAD):
        return True
    # 标题党：歌名里塞一整句话（「周深演唱《千与千寻》主题曲：人生是一列有去无回的火车」）。
    # 正常歌名很少有 28 个字符以上，这条能挡掉绝大多数搬运/解说式的假歌。
    if len(s) > 28:
        return True
    try:
        d = float(c.get("dur") or 0)
    except Exception:
        d = 0
    return 0 < d < 60           # 不到一分钟的基本是片段


def _jy_topic_plan(today_str):
    """今天要用哪几个主题。返回 [(key, 搜索词, 标题, 副标题)]，同一天结果稳定。

    顺序就是优先级：节日（今天/明天）> 动漫 > 热歌 > 影视 > 场景 > 游戏 > 年代。
    后面的同组备选也会被搜，谁先出歌谁上 —— 前一个关键词搜不到不至于少一张卡。
    """
    import datetime as _dt
    try:
        d = _dt.datetime.strptime(today_str, "%Y%m%d").date()
    except Exception:
        d = _dt.date.today()
    plan, used = [], set()

    def _push(key, seed, title, sub):
        if seed in used:
            return
        used.add(seed)
        plan.append((key, seed, title, sub))

    # 1) 节日：今天优先，明天次之（提前一天推「明天就是中秋」更有用）
    for off in (0, 1):
        dd = d + _dt.timedelta(days=off)
        hit = _JY_FEST.get((dd.month, dd.day))
        if not hit:
            for row in _JY_LUNAR.get(dd.year, []):
                if (row[0], row[1]) == (dd.month, dd.day):
                    hit = (row[2], row[3])
                    break
        if hit:
            sub = hit[1] if off == 0 else ("明天就是" + hit[0])
            _push("fest:" + hit[0], hit[0], hit[0], sub)

    # 2) 季节 / 学年节点（只在特定月份出，避免天天重复）
    m = d.month
    if m in (3, 4, 5):
        _push("season:春", "春天", "春天", "风开始暖了")
    elif m in (6, 7, 8):
        _push("season:夏", "夏天", "夏天", "空调房里那几首")
    elif m in (9, 10, 11):
        _push("season:秋", "秋天", "秋天", "适合散步")
    else:
        _push("season:冬", "冬天", "冬天", "手冷的时候")
    if m == 6:
        _push("day:毕业季", "毕业", "毕业季", "散伙饭那晚")
    if m == 9:
        _push("day:开学季", "开学", "开学季", "新学期第一天")
    if m == 12:
        _push("day:年终", "年终总结", "年终盘点", "今年剩下的日子")
    if m == 1:
        _push("day:新年", "新年好", "新年愿望", "今年第一首")

    # 3) 常青池按分组轮换：每组抽一个（同一天稳定）
    groups = ["anime", "hot", "drama", "scene", "game", "era"]
    for g in groups:
        cand = [x for x in _JY_TOPIC_POOL if x[0] == g]
        if not cand:
            continue
        row = cand[_jy_hash(today_str + g) % len(cand)]
        _push("topic:" + g, row[1], row[1] + _JY_TOPIC_SUF.get(g, ""), row[2])

    # 4) 同组再多备两个：前一个词搜不出歌时顶上
    for g in groups:
        cand = [x for x in _JY_TOPIC_POOL if x[0] == g]
        if not cand:
            continue
        start = _jy_hash(today_str + g) % len(cand)
        for k in range(1, min(3, len(cand))):
            row = cand[(start + k) % len(cand)]
            _push("spare:" + g, row[1], row[1] + _JY_TOPIC_SUF.get(g, ""), row[2])
    return plan


def daily_topic_playlists(want=6, wait=20, blocking=False, tags=None):
    """按今天的主题直接搜歌，一个主题一张卡。结果按天缓存（同一天只搜一遍）。

    这批歌**不在口味候选池里** —— 这是刻意的：主题歌单要的是「今天该听什么」，
    不是「你上个月听了什么」。只要能播就行（search 回来的都带源A id）。

    blocking=False（默认，前台请求走这条）：缓存没有就**立刻返回空**，同时起一个后台
    线程去算。否则首屏要等 10~18 秒（要真搜上游，而 search 的上游锁是串行的一把）。
    预热线程会用 blocking=True 先把它算好，绝大多数请求直接命中缓存。

    tags：用户选中的标签。**只做内存过滤，不重搜上游** —— 缓存是按天、与标签无关的
    （主题卡本来就该天天一样），标签只决定「这几张里给用户看哪几张」。所以切换/取消
    标签都是毫秒级，不会又掉进 10~18 秒的重算里。

    「running」标记统一由本函数管理（以前 blocking 分支不置位，预热线程和前台请求
    会各起一份相同的主题搜索，把上游锁打满）。
    """
    today = time.strftime("%Y%m%d")
    with _topic_lock:
        if _topic_cache["day"] == today and _topic_cache["pls"] is not None:
            return _filter_cards_by_tags(_topic_cache["pls"], tags)
        mine = False
        if not _topic_cache.get("running"):
            _topic_cache["running"] = True
            mine = True
    if mine:
        if not blocking:
            # 前台请求：绝不在这里真搜（10~18 秒）—— 丢给后台线程，本次先不挂主题卡
            threading.Thread(target=_topic_build_bg, args=(want, wait),
                             name="jy-topic", daemon=True).start()
            log("TOPIC: 后台开始生成（本次请求先不挂主题卡）")
            return []
        try:
            pls = _topic_build(want, wait)
        finally:
            with _topic_lock:
                _topic_cache["running"] = False
        return _filter_cards_by_tags(pls, tags)
    if not blocking:
        return []
    # 别人正在算：blocking=True 就等它算完（最多 wait+5 秒），绝不重复起一份。
    deadline = time.time() + float(wait) + 5.0
    while time.time() < deadline:
        time.sleep(0.15)
        with _topic_lock:
            if _topic_cache["day"] == today and _topic_cache["pls"] is not None:
                pls = _topic_cache["pls"]
                break
            if not _topic_cache.get("running"):
                pls = None
                break
        pls = None
    else:
        pls = None
    return _filter_cards_by_tags(pls, tags) if pls else []


def _topic_build(want=6, wait=20):
    """真正去搜。用 `jy_search_all`（源A + 源B + 源C三源并发）而不是 `search`：

    ① `search` 只打源A，而「秋天 / 失眠 / 日系热歌」这种**概念词**在源A上
       基本搜不到（那边是按歌名匹配的），只有「咒术回战 / 琅琊榜」这类作品名才行；
    ② `search` 的上游锁只有一把，多线程一起抢的结果是 wait 超时全被 skip
       （见过 12 个词最后只出 2 张卡的现场）。

    `jy_search_all` 自己三源并发、总时长封顶 `JY_SRC_WAIT`(3.2s)，这里再 4 路并发，
    11 个词大约 10 秒跑完 —— 在后台线程里跑，不阻塞前台请求。
    """
    today = time.strftime("%Y%m%d")
    try:
        plan = _jy_topic_plan(today)
        if not plan:
            with _topic_lock:
                _topic_cache["day"], _topic_cache["pls"] = today, []
            return []
        # 主选 want 个 + 最多 3 个备选（前面的词搜不出歌时顶上）
        cand = plan[:want + 3]

        def _one(seed):
            try:
                return jy_search_all(seed, 12)
            except Exception as e:
                log("topic search %r: %r" % (seed, e))
                return []

        rows_map = {}
        try:
            from concurrent.futures import ThreadPoolExecutor
            # JY-FAST(v4.00)：4 → 2 路。`jy_search_all` 里源A那一路要抢 `_lock`
            # （全局只有 2 个名额），4 路并发一开就把名额吃满 —— 这时候前台的
            # /api/recommend 在同一个门口排队，实测冷启动那一次从 12s 涨到 21s。
            # 降到 2 路，主题卡慢一点（本来就在后台算），但前台永远有位置。
            with ThreadPoolExecutor(max_workers=2) as ex:
                for p, rows in zip(cand, ex.map(lambda q: _one(q[1]), cand)):
                    rows_map[p[1]] = rows
        except Exception as e:
            log("topic parallel: %r" % (e,))
            for p in cand:
                rows_map[p[1]] = _one(p[1])

        out, used_seed = [], set()
        for p in cand:
            if len(out) >= want:
                break
            key, seed, title, sub = p
            if seed in used_seed:
                continue
            songs = []
            for r in (rows_map.get(seed) or []):
                if not isinstance(r, dict) or not r.get("id"):
                    continue
                if _topic_junk(r):
                    continue
                c = dict(r)
                c["online"] = True
                # 封面三条来源，从「一定能用」往下排：
                #   ① 源A纯数字 id → /api/online_cover（500×500，服务端缓存一周）
                #   ② 源B搜索结果自带 album picUrl → 源站直链，浏览器能直接加载
                #      （以前这里一刀切成空串，源B来的卡就全退成色块了）
                #   ③ 源C不回封面字段 → 留空，前端退成「色块 + 首字」
                c["cover"] = ""
                if re.fullmatch(r"\d+", str(c["id"])):
                    c["cover"] = ("/api/online_cover?id=" +
                                  urllib.parse.quote(str(c["id"]), safe=""))
                elif str(r.get("cover") or "").startswith("http"):
                    c["cover"] = r["cover"]
                songs.append(c)
            # 同一主题里也可能有同名/翻唱重复，顺手去一遍；
            # 再让「有封面」的（源A id 走 /api/online_cover）排前面 ——
            # 卡片封面是拿里面第一首有图的歌当代表，前排全是源B/源C的话就会退成色块
            songs = _dedupe_titles(songs, 12)
            songs.sort(key=lambda c: 0 if c.get("cover") else 1)
            if len(songs) < 4:             # 少于 4 首不成歌单，交给后面的备选
                continue
            used_seed.add(seed)
            out.append({"key": key,
                        "title": title,
                        "name": title,
                        "desc": "%s · %d 首" % (sub, len(songs)),
                        "songs": [_strip_priv(c) for c in songs[:12]]})
        with _topic_lock:
            _topic_cache["day"] = today
            _topic_cache["pls"] = out
        try:
            log("TOPIC: %d 张 / 搜了 %d 个 / %s" %
                (len(out), len(cand), " | ".join([o["title"] for o in out])))
        except Exception:
            pass
        # JY-TAGMAP/A3：主题卡算出来了 → **合并**进已缓存的推荐结果，绝不作废缓存。
        # 以前这里是 `_daily_cache["key"] = ""`：跨天第一次请求走了「非阻塞返回空」分支，
        # 前台 12 秒算出的结果没有主题卡，后台 10~18 秒算完又把 key 清空 →
        # 下一个请求再算 12 秒。用户体感就是「新的一天连着卡两次」。
        try:
            n = _merge_topic_into_cache(out)
            if n:
                log("TOPIC: 合并进 %d 份已缓存推荐（key 保留，不重算）" % n)
                _save_daily_disk()
        except Exception as e:
            log("TOPIC merge: %r" % (e,))
        return out
    except Exception as e:
        log("_topic_build: %r" % (e,))
        with _topic_lock:
            _topic_cache["day"], _topic_cache["pls"] = today, []
        return []


def _topic_build_bg(want=6, wait=20):
    """后台版 _topic_build：只管收尾把 running 标记清掉（不清的话前台再也不会重试）。"""
    try:
        _topic_build(want, wait)
    except Exception as e:
        log("topic bg: %r" % (e,))
    finally:
        with _topic_lock:
            _topic_cache["running"] = False


# 八音盒 / 钢琴翻奏 / 伴奏这类「同名但不是原唱」的版本：同一首歌名撞车时让原唱优先



# 八音盒 / 钢琴翻奏 / 伴奏这类「同名但不是原唱」的版本：同一首歌名撞车时让原唱优先
_JY_COVER_RE = re.compile(
    r"(orgel|オルゴール|music\s*box|八音盒|piano|カバー|cover|tribute|karaoke|"
    r"instrumental|伴奏|纯音乐|演奏|翻奏|remix)", re.I)


def _is_cover(c):
    t = "%s %s" % (c.get("artist") or "", c.get("song") or c.get("name") or "")
    return bool(_JY_COVER_RE.search(t))


def _title_key(c):
    """歌名的「去版本」归一：走 jy_norm（会去掉括号里的版本说明 + 大小写 + 空格标点）。

    于是这些会归成同一个键，正是我们要的：
        Begin Again / BEGIN AGAIN                       → beginagain
        ポケベルが鳴らなくて / ポケベルが鳴らなくて (オルゴール)  → ポケベルが鳴らなくて
        祝福 (D版) / 祝福（1993香港叶倩文演唱会）              → 祝福
    """
    s = c.get("song") or ""
    if not s:
        try:
            s = split_title(c.get("name") or "")[1] or c.get("name") or ""
        except Exception:
            s = c.get("name") or ""
    return jy_norm(s)


def _dedupe_titles(rows, limit):
    """同一首歌名在整个列表里只留一条（rows 必须已按分数排序）。

    候选池是「关键词搜索 + 本地曲库」拼的，同一个歌名会以多种身份反复冒出来：
    不同歌手的同名单曲（Begin Again ×4）、八音盒版、现场版……一个 12 首的列表里
    连着四个「Begin Again」看着就像坏了。归一后同名的只保留排最前的那条；
    如果排前面的是八音盒/翻奏，而后面有原唱，就让原唱顶上来。
    """
    best, order = {}, []
    for c in (rows or []):
        k = _title_key(c)
        if not k:
            continue
        if k not in best:
            best[k] = c
            order.append(k)
        elif _is_cover(best[k]) and not _is_cover(c):
            best[k] = c
    out = []
    for k in order:
        out.append(best[k])
        if len(out) >= limit:
            break
    return out


def _spread_artists(rows, per=2, limit=None, drop_over=False):
    """把同一歌手的歌从列表**头部往后挪**：保证「前 limit 首里同一歌手最多 per 首，
    且尽量不挨着」。

    这是给「右侧那两段单曲列表」（为你推荐的在线歌曲 / 本地歌单里的好歌）用的。
    主题歌单卡不需要这个 —— 一张卡本来就可以是某个作者的作品合集，也可以在
    一个关键词下收好几首他的歌；但**右侧列表是给人「一首一首往下听」的**，
    连着四首 Brian Crain 那种才是「扎堆」。

    drop_over：超额的直接不要（列表可能变短）。在线那段用它 —— 池子里就这么点人，
    与其拿同一个歌手把 12 个位置填满，不如老实给 8 首；本地那段不用（那是你自己的歌，
    一整张专辑就该整张给你）。
    不带 drop_over 时只挪位置、不删歌：超额的排到后面，还能被「加载更多」翻到。
    """
    rest = [(c, _art_list(c)) for c in (rows or [])]
    n_out = len(rest) if limit is None else max(0, int(limit))
    out, cnt = [], {}
    while rest and len(out) < n_out:
        last = set(out and _art_list(out[-1]) or ())
        pick = None
        for i, (c, arts) in enumerate(rest):      # ① 不超额 + 不挨着
            if not any(cnt.get(a, 0) >= per for a in arts) and not (last & set(arts)):
                pick = i
                break
        if pick is None:
            for i, (c, arts) in enumerate(rest):  # ② 只保「不超额」，允许挨着
                if not any(cnt.get(a, 0) >= per for a in arts):
                    pick = i
                    break
        if pick is None:
            if drop_over:                         # 剩下全是超额的 → 列表到此为止
                break
            pick = min(range(len(rest)),           # ③ 兜底：挑重复最少的那首垫尾
                       key=lambda i: max([cnt.get(a, 0) for a in rest[i][1]] or [0]))
        c, arts = rest.pop(pick)
        out.append(c)
        for a in arts:
            cnt[a] = cnt.get(a, 0) + 1
    return out


def _unadjacent(head, per=2, pool=None):
    """把头部里「相邻同歌手」换开（只调顺序，不改名单、不改长度）。

    为什么还要这一步：`_spread_artists` 已经尽量「不超额 + 不挨着」，但池子里可用
    歌手不够时会走到放宽档，尾段就出现「同一个人连着两首」（实测 Two Steps From
    Hell 占了第 5/11/12 位，11 和 12 挨着）。超额有时躲不掉，相邻总能躲掉 ——
    换位置就行，用户看到的观感差别很大。
    """
    def clash(a, b):
        sa, sb = _art_list(a), _art_list(b)
        return bool(sa) and bool(sb) and bool(set(sa) & set(sb))

    n = len(head)
    if n < 2:
        return list(head or [])
    head = list(head)
    _in = set(id(c) for c in head)
    spare = [c for c in (pool or []) if id(c) not in _in]
    for i in range(1, n):
        if not clash(head[i - 1], head[i]):
            continue
        moved = False
        for j in range(i + 1, n):                      # ① 先在头部内部换位
            if clash(head[i - 1], head[j]):
                continue
            if j + 1 < n and clash(head[j], head[i + 1]):
                continue
            head[i], head[j] = head[j], head[i]
            moved = True
            break
        if moved:
            continue
        for k, c in enumerate(spare):                  # ② 再拿池子里没进头的换
            if clash(head[i - 1], c):
                continue
            if i + 1 < n and clash(c, head[i + 1]):
                continue
            head[i] = c
            spare.pop(k)
            break
    return head


def _head_spread(rows, limit=12, per=2):
    """右侧列表 = 「干净的头 limit 首」+「剩下的照原顺序接在后面」。

    只把**首屏这 12 首**做歌手去扎堆（同一歌手最多 per 首、尽量不挨着），
    超额的歌不删，挪到后面去 —— 前端点「加载更多」时还能翻到，`total` 也不缩水。
    头实在凑不满 limit 首（池子里就一个歌手）才放宽，绝不把这块做空。
    """
    rows = list(rows or [])
    if not rows:
        return []
    want = min(int(limit), len(rows))
    head = _spread_artists(rows, per=per, limit=limit, drop_over=True)
    if len(head) < want:
        # 严格档凑不满 → 只把**剩下的名额**用宽松档补，已经选好的头保持不动。
        # （以前是整体重跑 drop_over=False，等于把前面辛苦分散好的结果全丢了。）
        _in = set(id(c) for c in head)
        rest = [c for c in rows if id(c) not in _in]
        cnt = {}
        for c in head:
            for a in _art_list(c):
                cnt[a] = cnt.get(a, 0) + 1
        while rest and len(head) < want:
            pick = None
            for i, c in enumerate(rest):
                if not any(cnt.get(a, 0) >= per for a in _art_list(c)):
                    pick = i
                    break
            if pick is None:
                pick = min(range(len(rest)),
                           key=lambda i: max([cnt.get(a, 0) for a in _art_list(rest[i])] or [0]))
            c = rest.pop(pick)
            head.append(c)
            for a in _art_list(c):
                cnt[a] = cnt.get(a, 0) + 1
    _in = set(id(c) for c in head)
    tail = [c for c in rows if id(c) not in _in]
    head = _unadjacent(head, per=per, pool=tail)
    return head + tail


# ---------------- JY-SF：每日推荐单飞（single-flight）
# 冷启动时 recommend() 要 3~16 秒（6 路并发搜上游 + 本地整库模糊匹配）。
# PC 和 App 同时打开推荐页、或者前端超时重试一次，就变成 N 份一模一样的计算：
# 上游按 N 倍被打一遍，线程池也被撑满，结果每个请求都更慢
# （实测 4 并发时单个请求从 6 秒涨到 20 秒以上）。
# 现在改成「一个人算，其他人等同一份结果」。
#
# ⚠ 绝不能拿 _data_lock 包住整段 recommend()：它是 threading.Lock（不可重入），
#   而 recommend() 内部自己会多次 with _data_lock（读 tags / 读 _daily_cache /
#   算完写回），外层再包一层必然自己把自己锁死。
#   所以单飞只用下面这把 _rec_sf_lock，且只在「抢票 / 取结果 / 收尾」的瞬间持有，
#   真正的计算过程一把锁都不占。
REC_SF_WAIT = 30.0       # 等待者最多等 30 秒。冷算实测 8~32 秒（看上游脸色），
                         # 给 30s 是让绝大多数等待者拿到现成结果；真超了自己再算一遍兜底，
                         # 绝不让请求吊死，也绝不白屏。
REC_SF_MAX_RUNS = 8       # 同时在算的 key 上限（正常只有 1 个；防 key 抖动把 dict 撑爆）

_rec_sf_lock = threading.Lock()
_rec_sf_runs = {}         # key -> {"ev": Event, "out": dict|None, "err": str}

# ---------------- JY-SLOT：多槽缓存 ----------------
# 原来只有 _daily_cache 一格：用户点一下标签 chip → key 变了 → 这一格被覆盖 →
# 取消标签时「默认槽」早没了，又得冷算一次（12 秒）。用户说的「取消选择要恢复默认」
# 就卡在这里：不是算不对，是每次都要重算。
# 现在按 key 留最近几份结果，「默认槽」（tagmode=auto）永远最后才淘汰。
#
# 槽表放在 _daily_cache["slots"] 里（不另起一个全局 dict）：
#   ① 所有缓存状态只有一处，`_daily_cache = {...}` 这种整体重置能一次清干净
#     （排障 / 用例里都在这么干，另起一个全局会让「清过了还有缓存」这种鬼故事出现）；
#   ② 生命周期和 _daily_cache 一致，都是进程内、按天失效。
_REC_SLOT_MAX = 6


def _slot_day(key):
    """key 的第一段就是 YYYYMMDD。"""
    return str(key or "").split("|", 1)[0]


def _slot_is_auto(key):
    """默认槽：tagmode 段是 auto（无标签的那一份）。淘汰时最后才动它。"""
    parts = str(key or "").split("|")
    return len(parts) >= 8 and parts[7] == "auto"


def _slots():
    """槽表（调用方必须已经持有 _data_lock）。"""
    s = _daily_cache.get("slots")
    if not isinstance(s, dict):
        s = {}
        _daily_cache["slots"] = s
    return s


def _slot_get(key):
    """取一份热缓存。跨天的旧槽直接清掉（按 key 里的日期判断，不依赖调用时机）。"""
    today = time.strftime("%Y%m%d")
    with _data_lock:
        s = _slots()
        e = s.get(key)
        if not e or not e.get("out"):
            return None
        if _slot_day(key) != today:
            s.pop(key, None)
            return None
        e["ts"] = time.time()
        return e["out"]


def _slot_put(key, out, allp=None):
    if not key or not isinstance(out, dict):
        return
    with _data_lock:
        s = _slots()
        s[key] = {"out": out, "all": allp or {}, "ts": time.time()}
        if len(s) > _REC_SLOT_MAX:
            # 先淘汰「非默认槽 + 最久没用」的
            order = sorted(s.keys(),
                           key=lambda k: (1 if _slot_is_auto(k) else 0,
                                          s[k].get("ts") or 0))
            for k in order[:max(0, len(s) - _REC_SLOT_MAX)]:
                s.pop(k, None)


def _rec_key(kw, limit, tags, seed, tagmode=None, likes=None):
    """每日推荐的缓存 key。

    ⚠ 缓存命中判断和单飞判断必须用**同一把尺子**，所以抽成一个函数：
    两边各拼一遍字符串，迟早会因为某一边漏了 ev_bucket 而判成两件事
    （后果：单飞失效，又回到 N 份并发计算）。

    v4.00 改了四处（都是为了「别动不动就 12 秒」）：
      ① tags 先排序去重：`a,b` 和 `b,a` 现在是同一把 key（以前是两把，纯浪费）；
      ② 播放事件分桶 15 → 80：听 15 首就翻档太密，等于每 5 分钟就换一次缓存；
      ③ 加 likes 摘要段：红心一变结果就变，可 key 不变 → 会拿到**错的**缓存；
         摘要用 _jy_hash（跨进程稳定），不用内置 hash()（PYTHONHASHSEED 一变就全废，
         落盘的缓存重启后对不上，白算一次）；
      ④ 加 tagmode 段：auto（用户没选标签 = 当日默认）与 pick（按标签）必须分开；
         但 pick 且 tags 为空时归一到 auto —— 用户取消标签切回的就该是同一份默认结果。
    """
    try:
        limit = max(1, min(int(limit or 12), 60))
    except (TypeError, ValueError):
        limit = 12
    _tags = sorted(set([str(t).strip() for t in (tags or []) if str(t).strip()]))
    _tm = "pick" if (str(tagmode or "").strip().lower() == "pick" and _tags) else "auto"
    # JY-RECSLOW（v4.25）：红心改成**每 8 首一个桶**，不再逐首进 key。
    # 原来逐首进 key → 用户有 1 个红心，预热（likes=[]）算出来的缓存永远对不上 key，
    # 每次冷启动都重新算，真机实测首屏 34.5 秒（页面一直卡在「正在生成推荐…」）。
    # 分桶后 0~7 个红心共用一份缓存；收藏再多也就隔几首才换一次，口味影响仍然在。
    try:
        _lh = str(len([x for x in (likes or []) if str(x).strip()]) // 8)
    except Exception:
        _lh = "0"
    return "%s|%s|%s|%s|%s|%s|%s|%s" % (
        time.strftime("%Y%m%d"), (kw or ""), ",".join(_tags),
        limit, str(seed or ""), len(_events()) // 80, _lh, _tm)


def recommend_sf(kw=None, limit=12, tags=None, seed="", likes=None, tagmode=None):
    """recommend() 的单飞包装。热了就直接走缓存返回，热路径只多算一次 key。"""
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    # limit 夹住（和 recommend() 里同一套规则）：它会被拿去算候选池门槛和缓存 key
    try:
        limit = max(1, min(int(limit or 12), 60))
    except (TypeError, ValueError):
        limit = 12
    if kw:
        # 带搜索词的即时推荐不进单飞：每个 q 都是一份不同的结果，
        # 让第二个搜索词排队等第一个，只会白白变慢。
        return recommend(str(kw), limit=limit, tags=tags, seed=seed, likes=likes,
                         tagmode=tagmode)
    key = _rec_key(kw, limit, tags, seed, tagmode, likes)
    with _data_lock:
        if _daily_cache["key"] == key and _daily_cache["out"]:
            return _daily_cache["out"]
    _hot = _slot_get(key)
    if _hot is not None:
        return _ensure_topics_in(_hot, tags)
    # ---- 抢票：只有「拿到票」的那个线程去算，其余等它的 Event
    with _rec_sf_lock:
        run = _rec_sf_runs.get(key)
        if run is None:
            if len(_rec_sf_runs) >= REC_SF_MAX_RUNS:
                _rec_sf_runs.clear()
            run = {"ev": threading.Event(), "out": None, "err": ""}
            _rec_sf_runs[key] = run
            mine = True
        else:
            mine = False
        ev = run["ev"]
    if not mine:
        ev.wait(REC_SF_WAIT)
        with _rec_sf_lock:
            out = run["out"]
            err = run["err"]
        if out is not None:
            log("REC sf: 复用同批计算（省一次冷启动）")
            return out
        # 等超时 / 算的那个人失败了：自己算一遍。宁可慢一点，也不能白屏或 500。
        log("REC sf: 等了 %.0fs 没结果（%s），本请求自己算"
            % (REC_SF_WAIT, err or "timeout"))
        return recommend(kw, limit=limit, tags=tags, seed=seed, likes=likes,
                         tagmode=tagmode)
    # 计算用 try/finally 收尾：**无论算成功、算失败还是被抛异常，都一定要 ev.set()**，
    # 否则所有等待者会永久挂在 Event 上（HTTP 线程被吃光，整个服务看上去「死了」）。
    out = None
    try:
        out = recommend(kw, limit=limit, tags=tags, seed=seed, likes=likes,
                        tagmode=tagmode)
    except Exception as e:
        with _rec_sf_lock:
            run["err"] = repr(e)
        raise
    finally:
        with _rec_sf_lock:
            if out is not None:
                run["out"] = out
            _rec_sf_runs.pop(key, None)
        ev.set()
    return out


def recommend(kw=None, limit=12, tags=None, seed="", likes=None, tagmode=None):
    """每日推荐（参考源B「每日30首」）：
    口味来源：选中的标签 > 最近播放的关键词；都没有 → 热门兜底。
    候选池：源A搜索结果（在线）+ 本地曲库，抽完再按「隐式反馈 + MMR」重排。
    生成结果按「日期 + 口味 + seed + 反馈分桶」确定：同一天同一口味刷多少次都一样；
    跨天自动换一批（分数咬得紧的那几首靠每日 ±0.05 扰动换先后）。
    kw（搜索词）传入时退化为即时推荐，不走缓存。

    likes：前端的红心列表（localStorage，服务端没有），随请求带上来一起算口味；
    收藏（收进歌单的歌）在服务端能直接看到，不需要前端传。

    tagmode（v4.00 新契约，前端按这个传）：
      · "auto"（前端未选任何标签时传）→ 忽略服务端保存的 _jy["tags"]，用当日默认结果；
      · "pick" + tags= → 按这些标签影响推荐（歌曲层加分 + 歌单层挑卡）；
      · 缺省（None）→ 老行为，tags 为空时回退 _jy["tags"]（兼容旧客户端）。
        回退在 HTTP 层做（见 /api/recommend），recommend() 本身只认传进来的 tags。
    """
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    # limit 必须夹住：它下面会被拿来算「候选池补货门槛 limit*3」和 mmr 的取子集上限，
    # 传个 99999 就等于要求上游凑 30 万首 —— 实测直接卡到 45s 超时（畸形参数能拖垮
    # 整个服务）。前端只会要 12~50，夹到 1~60 足够，也顺带把缓存 key 限死。
    try:
        limit = max(1, min(int(limit or 12), 60))
    except (TypeError, ValueError):
        limit = 12
    # v4.02d：pick 口径（用户明确点了标签）在函数一开始就定下来 ——
    # 后面「要不要走外部口味召回」「要不要出主题卡」都要看它。
    _pick_mode = bool(tags) and str(tagmode or "").strip().lower() == "pick"
    today = time.strftime("%Y%m%d")
    daily = not kw
    # 反馈按「每 80 条听歌结果」分桶进 key：当天基本稳定，
    # 但你连着听了一批之后推荐会跟着口味走，不用干等到第二天。
    # v4.00：15 → 80。原来听 15 首就翻档，等于每几分钟就作废一次缓存。
    ev_bucket = len(_events()) // 80      # 下面 recall_multi 还要拿它当分桶，别删
    # ⚠ key 一律走 _rec_key：和单飞那边是同一个函数，两边不会写岔
    key = _rec_key(kw, limit, tags, seed, tagmode, likes)
    rec_t0 = time.time()
    if daily:
        with _data_lock:
            if _daily_cache["key"] == key and _daily_cache["out"]:
                _hit = _daily_cache["out"]
            else:
                _hit = None
        if _hit is not None:
            return _ensure_topics_in(_hit, tags)
        _hot = _slot_get(key)                 # 多槽：切标签再切回来直接命中，不重算
        if _hot is not None:
            return _ensure_topics_in(_hot, tags)
        # 内存冷：从磁盘落盘缓存补一次（启动预热 / 跨重启），命中即秒回
        if not _daily_disk_loaded:
            _load_daily_disk()
            with _data_lock:
                if _daily_cache["key"] == key and _daily_cache["out"]:
                    return _ensure_topics_in(_daily_cache["out"], tags)
            _hot = _slot_get(key)
            if _hot is not None:
                return _ensure_topics_in(_hot, tags)
    out = {"online": [], "local": [], "reason": "", "date": today, "sources": []}
    # 口味画像（阶段一）：隐式反馈 + 收藏 + 红心折成的歌手/歌名分数（外部召回也要用）
    prof = taste_profile(likes)
    hist = recent_played(10)
    # 关键词带上「有多可信」：自己选的标签最可信，其次是最近听的歌手，再次是最近听的歌名
    kw_w = {}
    if kw:
        kw_w[str(kw)] = 1.0
    for t in tags:
        kw_w.setdefault(t, 1.0)
    # 最近播放里同一歌手最多贡献 2 条关键词。否则连着听了某人的 5 首歌，
    # 6 个搜索词全指向他一个人，候选池就只剩那一张专辑 ——
    # 推荐出来整屏都是同一个人（这是「同名」修完之后马上暴露的下一个问题）。
    _per_a = {}
    for h in hist:
        a, s = split_title(h)
        if a and _per_a.get(a, 0) >= 2:
            continue
        if a:
            _per_a[a] = _per_a.get(a, 0) + 1
            kw_w.setdefault(a, 0.75)
        if s:
            kw_w.setdefault(s, 0.55)
        if len(kw_w) >= 6:
            break
    if not kw_w:
        kw_w = {"热门歌曲": 0.35, "经典老歌": 0.35}
        out["reason"] = "先从热门开始，之后会按你的喜好推荐"
    elif tags:
        out["reason"] = "按你感兴趣的标签（%s）推荐" % "、".join(tags[:3])
    else:
        out["reason"] = "根据你最近播放的《%s》等推荐" % (hist[0] if hist else list(kw_w)[0])
    if daily:
        out["reason"] += " · 每天更新，明天换一批"
    # 阶段二：外部源发现（Last.fm / 源B / ListenBrainz / Embeat）。
    # 返回 (建议列表, {genre_hints})；任一源失败都降级为 []，不影响主流程。
    # v4.02d：pick 口径不走外部口味召回。两个理由：
    #   · 它在平板上没有总时限，实测一个新标签能拖到 58 秒（民谣）；
    #   · 它按「你平时听谁」扩关键词，会把用户明说的标签稀释成「还是那批华语流行」。
    # 代价是没有 genre_hints —— 但标签候选现在靠 _by（按标签搜来的）判贴合，不需要它。
    if _pick_mode:
        sug_all, meta = [], {}
    else:
        sug_all, meta = recall_multi(prof, bucket="%s|%s" % (ev_bucket, _prof_sig(prof)))
    genre_hints = meta.get("genre_hints", {}) or {}
    out["sources"] = sorted(set(s["source"] for s in sug_all if s.get("source")))
    # 把外部建议扩成搜索词：歌手必加，有歌名的加「歌手 歌名」整串，
    # 让主搜索（源A + 本地）把这些外部口味真的捞进候选池。
    for s in sug_all:
        a = _norm_key(s.get("artist") or "")
        w = float(s.get("weight") or 0.5)
        if a:
            kw_w[a] = max(kw_w.get(a, 0.0), min(0.9, 0.3 + 0.4 * w))
        sn = _norm_key(s.get("song") or "")
        if a and sn:
            kk = "%s %s" % (a, sn)
            kw_w[kk] = max(kw_w.get(kk, 0.0), min(0.9, 0.35 + 0.4 * w))
    kws = list(kw_w)
    seen, pool = set(), []

    def _absorb(rs, w, kw=None):
        """把搜索结果收进候选池（按 id 去重）。只做去重 + 记来源可信度，一首不多加。

        kw：这把结果是哪个关键词搜来的。v4.02 起记进 `_by` ——
        「按标签搜出来的候补」必须能被认出来，否则标签永远是空话。"""
        for r in (rs or []):
            if not isinstance(r, dict) or not r.get("id") or r["id"] in seen:
                continue
            seen.add(r["id"])
            c = dict(r)
            if kw and not c.get("_by"):
                c["_by"] = str(kw)
            c["_w"] = max(float(w), float(c.get("_w") or 0.0))
            c["source"] = c.get("source") or ""      # 来源标记（默认空 = 源A/本地）
            pool.append(c)

    # JY-FAST: 关键词并行搜索（10s 总时限），上游卡死时最多损失未缓存的关键词
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = dict((ex.submit(search, k, 10, 8), k) for k in kws[:6])
            for f in as_completed(futs, timeout=12):
                try:
                    rs = f.result()
                except Exception:
                    rs = []
                _absorb(rs, kw_w.get(futs[f], 0.5), kw=futs[f])
    except Exception as e:
        log("recommend parallel: %r" % (e,))
    if not pool:                          # 兜底：串行试前 3 个（已缓存的关键词会秒回）
        for k in kws[:3]:
            _absorb(search(k, limit=10, wait=8), kw_w.get(k, 0.5), kw=k)
    # 阶段二：外部「具体某首歌」建议，主搜索没捞到的，定点解析一次（拿不到就丢，不造歌）
    _resolve_external(sug_all, pool, seen, kw_w)
    # 打分 → MMR 重排。排序只看分数和相似度，池子里没有的歌一律不会冒出来。
    score_candidates(pool, prof)
    _apply_genre_hints(pool, genre_hints)
    # JY-TAGMAP: 标签加成必须在曲风定下来之后（_apply_genre_hints）—— 只加分不删歌
    _apply_tag_bonus(pool, tags)
    # 同名去重放在重排**之前**：池子里一首歌常有 4~5 个同名版本（原唱/八音盒/翻唱），
    # 不去掉的话它们会各占一个「每歌手 2 首」的名额，把别人的歌挤出去。
    # 注意别反过来「多取 4 倍再砍」—— 那会把 MMR 逼到放弃每歌手上限，
    # 结果整屏都是同一个人（踩过一次）。
    pool_u = _dedupe_titles(sorted(pool, key=lambda c: -(c.get("rel") or 0)), 10 ** 6)
    # 池子太窄时补两把「通用种子」：连着听了同一个人的一整张专辑，6 个关键词全指向他，
    # 池子里只有那一张专辑（十几首、同名版本还占掉几个），MMR 的「每歌手最多 2 首」
    # 会被迫放弃，结果 12 首里 8 首是同一个人。补两把热门/老歌把池子撑开。
    # 仍然是池子（源A搜索结果），没有为了凑数去池外找歌。
    # v3.83：门槛从 limit*2 提到 limit*3 —— 正好等于 2 倍时（24 条要挑 24 条）
    # 一个都挑不掉，MMR 只能全放宽，见过 12 首里 4 首 Brian Crain 的现场。
    if len(pool_u) < limit * 3:
        _n0 = len(pool)
        # JY-FAST: 这两把兜底原来是**串行 for**，每把 search 都要抢那把只有 2 个名额的
        # 全局锁，串行就是 2×(1~5s)。改成并行（两把同时抢，抢不到就是 wait 超时跳过），
        # 顺带把「池子够不够」的判断提到搜索之后再做一次。
        try:
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(max_workers=2) as _ex2:
                _fb = list(_ex2.map(lambda k: _safe_search(k, 10, 8),
                                    ("热门歌曲", "经典老歌")))
        except Exception:
            _fb = []
        for _kw, _rows in zip(("热门歌曲", "经典老歌"), _fb):
            _absorb(_rows, 0.30, kw=_kw)
        if len(pool) > _n0:
            score_candidates(pool[_n0:], prof)
            _apply_genre_hints(pool[_n0:], genre_hints)
            _apply_tag_bonus(pool[_n0:], tags)
        pool_u = _dedupe_titles(sorted(pool, key=lambda c: -(c.get("rel") or 0)), 10 ** 6)
    # 一次排到 60 条存进全量池（够前端「加载更多」翻好几轮），out 只取前 limit 条。
    # 两处指向同一批 dict，所以下面补 online/cover 字段时 all 里也一起有了。
    _want_all = max(limit, min(len(pool_u), 60))
    # v3.83：右侧那段列表是「一首一首往下听」的，同一歌手连着刷屏最劝退 →
    # 首屏那 12 首再摊一道（同一歌手最多 2 首、不挨着），多余的挪到后面给「加载更多」。
    _rk_onl = _head_spread(
        _dedupe_titles(mmr_rerank(pool_u, lambda_=0.7, daily_seed=today,
                                  limit=_want_all), 10 ** 6), limit=limit, per=2)
    _rk_onl = _tag_lead(_rk_onl, tags)        # v4.02：贴合标签的先上
    _all_online = [_strip_priv(c) for c in _rk_onl]
    out["online"] = _all_online[:limit]
    # JY-COVER: 每日推荐的两段列表都要带封面信息，否则前端只能画「♪」。
    #   online  —— 标记 online=True。前端靠这个字段决定封面走 /api/online_cover
    #              （它只认纯数字 id 的源A歌）。缺了会被误判成本地歌，
    #              去本地找一个还没下载过的文件，结果永远没有图。
    #   cover   —— 给成相对路径（/api/...），两端各自拼自己的 host。
    #              不要拼绝对地址：这份结果有按天的缓存，写死 IP 换个网段就废了。
    for _c in _all_online:
        _c["online"] = True
        if _c.get("id"):
            _c["cover"] = "/api/online_cover?id=" + urllib.parse.quote(str(_c["id"]), safe="")

    local_rows, seenl = [], set()

    def _absorb_local(rows, w):
        for r in (rows or []):
            nm = r.get("name")
            if not nm or nm in seenl:
                continue
            seenl.add(nm)
            c = dict(r)
            c["_w"] = float(w)
            c["source"] = c.get("source") or ""
            local_rows.append(c)

    # JY-FAST: 本地候选并行搜索（6 个关键词各自 fuzzy 匹配整库，串行会累计 8~10s；
    # local_files() 自身有 120s 缓存，并行不会重复扫盘，只是把模糊匹配摊到多线程）
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as _lex:
            _local_res = list(_lex.map(lambda k: search_local(k, limit=8), kws[:6]))
        for k, _rows in zip(kws[:6], _local_res):
            _absorb_local(_rows, kw_w.get(k, 0.5))
    except Exception:
        for k in kws[:6]:
            _absorb_local(search_local(k, limit=8), kw_w.get(k, 0.5))
    # 本地池子不能只凑够首屏就停：以前是 len<limit 才补全库，搜索结果刚好 12 首就
    # 不补了 → 「加载更多」在 local 侧永远没内容，而且 12 首里只要有一个歌手占 3 席
    # 就必然相邻（实测 Two Steps From Hell 卡在 11/12 位换都换不开）。
    # 补到 limit*3（上限 60）就停，前面的高相关命中顺序不变，兜底歌只会排在后面。
    _need_l = min(60, max(limit * 3, limit))
    if len(local_rows) < _need_l:
        for n in list_local_songs():
            if len(local_rows) >= _need_l:
                break
            if n in seenl:
                continue
            seenl.add(n)
            a0, s0 = split_title(n)
            local_rows.append({"name": n, "song": s0 or clean_query(n), "artist": a0,
                               "playlist": "", "score": 0, "_w": 0.35})
    score_candidates(local_rows, prof)
    _apply_genre_hints(local_rows, genre_hints)
    for c in local_rows:
        # 本地这边多一个信号：搜索关键词的模糊命中分（rc 越像越该往前）
        c["rel"] = c["rel"] + 0.25 * float(c.get("score") or 0.0)
    _apply_tag_bonus(local_rows, tags)      # 标签在本地段同样生效
    local_u = _dedupe_titles(sorted(local_rows, key=lambda c: -(c.get("rel") or 0)), 10 ** 6)
    # 本地同样排到 60 条存全量池（本地曲库一般够，取不到就少）
    _want_all_l = max(limit, min(len(local_u), 60))
    _rk_loc = _head_spread(
        _dedupe_titles(mmr_rerank(local_u, lambda_=0.7, daily_seed=today,
                                  limit=_want_all_l), 10 ** 6),
        limit=limit, per=2)
    _rk_loc = _local_tag_order(_rk_loc, tags)     # v4.02b：本地段先上对味的、别的曲风垫底
    _all_local = [_strip_priv(c) for c in _rk_loc]
    out["local"] = _all_local[:limit]
    # JY-COVER: 本地段同样带上封面相对路径，App 端 recmiCard 直接拿去用
    for _c in _all_local:
        if _c.get("name"):
            _c["cover"] = "/api/cover?name=" + urllib.parse.quote(_c["name"], safe="")
    # 主题歌单卡：把候选池聚成「曲风/场景/语种/歌手/派生」卡（PC 端与 App 端各自渲染）。
    # 仍 100% 来自候选池，没有的就丢，不造歌。
    # 主题歌单分两段：
    #   ① 每日主题（JY-TOPIC）—— 按当天节日/热点关键词直接搜出来的歌，排在最前面，
    #      每天换一批，是「今天该听什么」；这批歌不在口味候选池里，是刻意的。
    #   ② 口味聚类（旧逻辑）—— 从你自己的候选池里聚出来的，补在后面保住个性化。
    # v4.02b：用户明确点了标签（tagmode=pick）→ 主题卡让路。两个原因都很实在：
    #   · 「万圣节/毕业/仙侠原声」跟曲风无关，摆上去就是「和标签不符」；
    #   · 主题卡首次要等上游 wait=20 秒，是「选标签要等 20 秒」的主因。
    # pick 请求只出「按标签聚类的口味卡」，又快又对味；默认口径（auto）一切照旧。
    try:
        _topics = [] if _pick_mode else daily_topic_playlists(want=8, tags=tags)
    except Exception as e:
        log("daily_topic_playlists: %r" % (e,))
        _topics = []
    try:
        # 主题卡是「今天该听什么」，口味卡是「你可能喜欢」—— 两组都要有，
        # 但主题卡优先。曾把口味卡 want 压成 10-主题数，主题满 8 张时只剩 2，
        # 再被跨卡去重摘几首就整张丢掉，结果 8 张全是主题卡、个性化彻底没了。
        # tags 传下去：歌单层要真的受标签影响（用户点名「特别是歌单」）。
        _taste = build_themed_playlists(pool, local_rows, prof, today,
                                        want=4 if len(_topics) >= 6 else 6,
                                        tags=tags)
    except Exception as e:
        log("build_themed_playlists: %r" % (e,))
        _taste = []
    # 跨卡去重：主题卡排前面，后面的卡里把已经出现过的歌摘掉 —— 不然
    # 「欧美热单」和「Taylor Swift 代表作」会并排把 Lover / 22 / Fearless
    # 各摆一遍，看着像同一张卡复制了两份。摘到不足 4 首的卡整张丢掉。
    _seen_tt, _uniq = set(), []
    for _p in (_topics + _taste):
        _ss = []
        for _c in (_p.get("songs") or []):
            _tk = (jy_norm(_c.get("song") or _c.get("name") or ""),
                   jy_norm(_c.get("artist") or ""))
            if not _tk[0] or _tk in _seen_tt:
                continue
            _seen_tt.add(_tk)
            _ss.append(_c)
        if len(_ss) < 4:
            continue
        _p = dict(_p)
        _p["songs"] = _ss
        if re.search(r"·\s*\d+\s*首$", str(_p.get("desc") or "")):
            _p["desc"] = re.sub(r"·\s*\d+\s*首$", "· %d 首" % len(_ss), _p["desc"])
        _uniq.append(_p)
    out["playlists"] = _card_lead(_uniq, tags)[:10]   # v4.02：卡也要跟着标签走
    # 左栏 2 列网格：奇数张会留孤卡，统一砍到偶数
    if len(out["playlists"]) >= 4 and len(out["playlists"]) % 2:
        out["playlists"] = out["playlists"][:-1]
    _allp = {"online": _all_online, "local": _all_local}
    if daily:
        with _data_lock:
            _daily_cache["key"] = key
            _daily_cache["out"] = out
            # 全量池给「加载更多」用；曲库/口味变了下一轮 recommend 会整个换掉
            _daily_cache["all"] = _allp
        # JY-SLOT：多留一份（切标签再切回来命中的就是它）
        _slot_put(key, out, _allp)
        # JY-A5：冷算成功立刻落盘一次 —— 进程重启/部署不用再算 12 秒。
        # 有节流：_save_daily_disk 自己带 30 秒最小间隔（见该函数说明）。
        try:
            _save_daily_disk()
        except Exception:
            pass
    log("REC timing: TOTAL=%.2fs online=%d local=%d tags=%s"
        % (time.time() - rec_t0, len(out["online"]), len(out["local"]),
           ",".join(tags) or "-"))
    # JY-PLCACHE：推荐/歌单一出就后台预取（音频+封面+歌词），播放时走本地零缓冲。
    # 非阻塞：prefetch_playlist_cache 自己起 daemon 线程，不拖慢这次响应。
    try:
        prefetch_playlist_cache(out)
    except Exception as e:
        log("pl prefetch schedule fail %r" % (e,))
    return out


def _rec_warmer(interval=900, first_delay=1):
    """后台预热每日推荐缓存：启动后延迟首次、之后每 interval 秒重算一次，
    让前台 /api/recommend 绝大多数情况直接命中 _daily_cache（秒回），不再让用户等冷启动。
    算失败也只是少了一次预热，不影响前台请求（前台 miss 时仍会自己同步算）。

    JY-A4：主题卡（10~18s）和推荐（12s）原来是**串行**的，一次预热 25~30 秒；
    两件事之间没有数据依赖（主题卡算完会合并进推荐缓存，见 _merge_topic_into_cache），
    所以并行跑，首次预热时间从 ~30s 降到 ~18s。
    """
    time.sleep(first_delay)
    while True:
        def _do_topic():
            # 让推荐先跑一步：主题卡的 11 个搜索词会和 /api/recommend 的 6 个词抢
            # 同一把上游锁（2 个名额）。错开 1.5 秒，冷启动那次的首屏就快得多。
            time.sleep(1.5)
            try:
                daily_topic_playlists(want=8, blocking=True)
            except Exception as e:
                log("topic warmer error: %r" % (e,))

        _th = threading.Thread(target=_do_topic, name="jy-warm-topic", daemon=True)
        _th.start()
        try:
            # JY-RECSLOW（v4.25）：按**客户端真实参数**预热，别只热默认那一把 key。
            # App 首屏是 /api/recommend?limit=10&tagmode=auto（红心由本机带上来），
            # PC 是 limit=12；两把都热一遍，否则预热等于白跑（真机 34.5s 就是这么来的）。
            for _lim in (10, 12):
                for _tm in ("auto", None):
                    try:
                        recommend_sf(None, _lim, tags=[], seed="", likes=[], tagmode=_tm)
                    except Exception as _e:
                        log("REC warmer(%s,%s) fail: %r" % (_lim, _tm, _e))
            log("REC warmer: refreshed daily cache")
        except Exception as e:
            log("REC warmer error: %r" % (e,))
        try:
            _th.join(timeout=70)          # 主题卡最多再等 70 秒，别把整轮预热拖死
        except Exception:
            pass
        try:
            _save_daily_disk(force=True)
        except Exception:
            pass
        time.sleep(interval)


def _xm_status(did):
    """读 xiaomusic 设备状态；失败返回 None"""
    try:
        url = BACKEND_API + "/getplayerstatus?did=" + urllib.parse.quote(did or "")
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _xm_position(did):
    st = _xm_status(did)
    try:
        return int((st or {}).get("position") or 0)
    except Exception:
        return 0


# ---------------- JY-VOL: 音量下发
# 拖竖向滑杆时前端会连发十几个请求，这里合并成"只发最后一格"，
# 既不会把音箱打爆，松手时的最终值也一定落地。
_vol_slot = {"did": "", "vol": None, "ev": threading.Event()}
_vol_lock = threading.Lock()


def _vol_push(did, vol):
    try:
        body = json.dumps({"did": did, "volume": int(vol)}).encode()
        req = urllib.request.Request(BACKEND_API + "/setvolume", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except Exception as e:
        log("setvolume %s -> %s fail: %r" % (did, vol, e))
        return {"ok": False, "msg": str(e)}


def _vol_worker():
    while True:
        _vol_slot["ev"].wait()
        time.sleep(0.12)
        _vol_slot["ev"].clear()
        with _vol_lock:
            did, vol = _vol_slot["did"], _vol_slot["vol"]
            _vol_slot["vol"] = None
        if vol is None or not did:
            continue
        _vol_push(did, vol)


def set_volume_async(did, vol):
    """把音量丢进队列立刻返回；真正下发由 _vol_worker 合并后执行"""
    with _vol_lock:
        _vol_slot["did"] = did or _default_did()
        _vol_slot["vol"] = int(vol)
    _vol_slot["ev"].set()


_fix_lock = threading.Lock()


def autofix():
    """遥控页反馈「无法播放」时的一键自检修复。
    顺序很关键：xiaomusic 重启会丢设备绑定，所以 heal 必须在所有重启之后。
    ① 服务可达 → 不可达就重启
    ② 设备绑定自愈（重启后必丢，每次都修）
    ③ 音量为 0 → 恢复 60；下发恢复播放
    ④ 进度前进 → 修复成功
    ⑤ 没在播且无播放任务 → 服务层正常，提示直接点歌
    ⑥ 有任务但卡住 → 重启 → 再 heal → 再试
    ⑦ 仍不行 → 登录态大概率失效，提示重新授权
    """
    with _fix_lock:
        steps = []
        did = ""
        try:
            with open(os.path.join(CONF_DIR, "setting.json"), encoding="utf-8") as f:
                did = str(json.load(f).get("mi_did") or "")
        except Exception:
            pass

        def wait_up(budget_rounds=15):
            for _ in range(budget_rounds):
                st = _xm_status(did)
                if st is not None:
                    return st
                time.sleep(3)
            return None

        st = _xm_status(did)
        if st is None:
            steps.append("xiaomusic 服务不可达 → 重启")
            threading.Thread(target=_restart_backend, daemon=True).start()
            st = wait_up()
            if st is None:
                return {"ok": False, "fixed": False, "steps": steps,
                        "report": "服务重启后仍不可达，请到平板上跑 start_xiaomusic.sh"}
        steps.append("服务可达 ✓")

        def heal_bind():
            try:
                with urllib.request.urlopen(BACKEND_API + "/dev/heal", timeout=60) as r:
                    res = json.loads(r.read().decode("utf-8"))
                if res.get("ok"):
                    steps.append("设备绑定自愈 ✓")
                    return True
                steps.append("设备绑定自愈失败：%s" % (res.get("reason") or res))
                return False
            except Exception as e:
                steps.append("设备绑定自愈失败 %r" % (e,))
                return False

        def try_resume():
            try:
                vol = int((_xm_status(did) or {}).get("volume") or 0)
                if vol == 0:
                    body = json.dumps({"did": did, "volume": 60}).encode()
                    req = urllib.request.Request(
                        BACKEND_API + "/setvolume", data=body,
                        headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=10).read()
                    steps.append("音量为 0 → 恢复到 60")
            except Exception as e:
                steps.append("音量恢复失败 %r" % (e,))
            try:
                with urllib.request.urlopen(
                        BACKEND_API + "/playingmusic?did=" + urllib.parse.quote(did),
                        timeout=20) as r:
                    r.read()
                steps.append("下发恢复播放 ✓")
            except Exception as e:
                steps.append("恢复播放失败 %r" % (e,))
            p1 = _xm_position(did)
            time.sleep(4)
            p2 = _xm_position(did)
            return p2 > p1, p1, p2

        # 重启后设备绑定必丢，先修一次
        heal_bind()

        ok, p1, p2 = try_resume()
        if ok:
            steps.append("进度恢复前进 %ds→%ds ✓" % (p1, p2))
            return {"ok": True, "fixed": True, "steps": steps, "report": "已自动修复，恢复播放"}

        # 没在播：看是不是根本没有播放任务（服务层其实是好的）
        try:
            with urllib.request.urlopen(
                    BACKEND_API + "/playingmusic?did=" + urllib.parse.quote(did),
                    timeout=10) as r:
                pm = json.loads(r.read().decode("utf-8"))
        except Exception:
            pm = {}
        cur = str(pm.get("cur_music") or "")
        if not cur:
            steps.append("当前无播放任务")
            return {"ok": True, "fixed": True, "steps": steps,
                    "report": "服务与设备绑定已修复；当前没有在播的歌，直接点一首即可"}

        # 有任务但卡住 → 重启 → 再 heal → 再试
        steps.append("歌曲任务卡住 → 重启 xiaomusic")
        threading.Thread(target=_restart_backend, daemon=True).start()
        if wait_up() is None:
            return {"ok": False, "fixed": False, "steps": steps,
                    "report": "服务重启失败，请到平板上跑 start_xiaomusic.sh"}
        heal_bind()
        ok, p1, p2 = try_resume()
        if ok:
            steps.append("重启后进度前进 %ds→%ds ✓" % (p1, p2))
            return {"ok": True, "fixed": True, "steps": steps, "report": "重启后已恢复播放"}

        steps.append("重启后仍未播放")
        return {"ok": False, "fixed": False, "steps": steps,
                "report": "自动修复未成功，多半是设备端登录态失效 —— 请到设置里点「重新授权登录」"}



# ---------------------------------------------------------------- HTTP 服务


def _restart_backend():
    """重启 xiaomusic（只动它，别碰 musictool 自己）。用于授权登录后让新 token 生效。"""
    try:
        time.sleep(1.5)  # 等 HTTP 响应先回去
        subprocess.run(["pkill", "-f", "[x]iaomusic --config"], check=False)
        time.sleep(2)
        subprocess.Popen(
            "setsid nohup /root/venv/bin/xiaomusic --config /root/xiaomusic_conf/config.json"
            " > /root/xiaomusic_run.log 2>&1 < /dev/null &",
            shell=True,
        )
        log("_restart_backend: xiaomusic restart triggered")
    except Exception as e:
        log("_restart_backend failed %r" % (e,))
# ------------------------------------------------ 设备端登录：扫码 / 体检 / 设备
MI_UA = ("MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) "
         "Alamofire/6.0.103 MICO/iOSApp/appStore/6.0.103")


def _http_get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def _auth_file_path():
    return os.path.join(os.environ.get("JY_CONF", CONF_DIR), "auth.json")


def mi_auth_check():
    """只读校验 auth.json 里的设备端授权是否还有效"""
    res = {"valid": False, "has_auth": os.path.isfile(_auth_file_path())}
    if not res["has_auth"]:
        res["msg"] = "尚未登录（无授权文件）"
        return res
    try:
        with open(_auth_file_path(), encoding="utf-8") as f:
            a = json.load(f)
    except Exception as e:
        res["msg"] = "授权文件损坏：%r" % (e,)
        return res
    if not a.get("passToken") or not a.get("userId"):
        res["msg"] = "授权文件缺少 passToken / userId"
        return res
    ck = ("deviceId=%s;pass_o=%s;passToken=%s;userId=%s;cUserId=%s;uLocale=zh_CN"
          % (a.get("deviceId", ""), a.get("pass_o", ""), a.get("passToken", ""),
             a.get("userId", ""), a.get("cUserId", "")))
    try:
        raw = _http_get(
            "https://account.xiaomi.com/pass/serviceLogin?_json=true&sid=micoapi&_locale=zh_CN",
            timeout=15, headers={"User-Agent": MI_UA, "Cookie": ck})
        j = json.loads(raw.split("&&&START&&&")[-1])
    except Exception as e:
        res["msg"] = "校验请求失败：%r" % (e,)
        return res
    res["code"] = j.get("code")
    if j.get("code") == 0:
        res.update(valid=True, msg="授权有效")
    else:
        res["msg"] = "授权已失效（code=%s %s）" % (j.get("code"), j.get("desc") or "")
    return res


def xm_devices():
    """取 xiaomusic 当前认到的音箱（/device_list 是轻量接口，不会卡登录锁）"""
    d = json.loads(_http_get(BACKEND_API + "/device_list", timeout=8))
    return d.get("devices") or []


def xm_mi_did():
    try:
        with open(os.path.join(os.environ.get("JY_CONF", CONF_DIR), "setting.json"),
                  encoding="utf-8") as f:
            return json.load(f).get("mi_did") or ""
    except Exception:
        return ""


def xm_setting():
    return json.loads(_http_get(BACKEND_API + "/getsetting", timeout=10))


def xm_device_rows(devs):
    rows = []
    for x in devs or []:
        rows.append({
            "did": x.get("miotDID") or x.get("deviceID") or "",
            "name": x.get("name") or "",
            "hardware": x.get("hardware") or "",
            "online": (x.get("presence") or "") == "online",
        })
    return rows


class Handler(BaseHTTPRequestHandler):
    server_version = "MusicMate/1.0"
    protocol_version = "HTTP/1.1"   # JY-FAST: keep-alive，浏览器并发请求复用连接

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _text(self, txt, ctype="text/plain; charset=utf-8"):
        body = txt.encode() if isinstance(txt, str) else txt
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype, head=False):
        """带 Range 的静态文件响应（电视拖进度条要靠它）"""
        try:
            size = os.path.getsize(path)
        except Exception:
            return self._json({"ok": False, "msg": "missing"}, 404)
        start, end, code = 0, max(0, size - 1), 200
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m and size > 0 and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:                                  # bytes=-N 取末尾
                start = max(0, size - int(m.group(2)))
                end = size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1 if size else 0))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "public, max-age=3600")
        self._cors()
        self.end_headers()
        if head or size <= 0:
            return
        left = end - start + 1
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while left > 0:
                    chunk = f.read(min(131072, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _jy_stream(self, head=False):
        """GET/HEAD /api/jy_stream?id=<mid> —— 在线歌直链代理（不落盘）。

        musictool 自己去上游取字节流，带上游要的 Referer/UA，支持 Range（音箱拖进度
        要靠它），再把流原样吐给音箱。**不 302**：上游 CDN 校验 Referer，裸链拉不动。
        """
        u0 = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u0.query)
        mid = ((qs.get("id") or [""])[0] or "").strip()
        if not mid:
            return self._json({"ok": False, "msg": "缺少 id"}, 400)
        try:
            url, src, note, _song, _art = jy_resolve(mid)
        except Exception as e:
            return self._json({"ok": False, "msg": "取直链失败：%s" % e}, 502)
        if not url:
            return self._json({"ok": False, "msg": note or "取不到直链"}, 404)
        ext = _url_ext(url)
        if ext in _LOSSLESS_EXT:
            # 无损不让音箱直接拉（会静音），让前端走「下载 → 转 wav」那条路
            return self._json({"ok": False, "fallback": "flac", "ext": ext,
                               "msg": "无损格式音箱放不了，已改为下载后播放"}, 415)
        hdrs = {"User-Agent": UA, "Accept": "*/*"}
        _ref = ""   # 公开版：不再按源附带 Referer
        if _ref:
            hdrs["Referer"] = _ref
        rng = self.headers.get("Range") or ""
        if rng:
            hdrs["Range"] = rng
        try:
            r = opener().open(urllib.request.Request(url, headers=hdrs), timeout=20)
        except Exception as e:
            # 带 Referer 被拒（403）时再试一次不带 Referer 的 —— 各源 CDN 的脾气不一样，
            # 不要因为一个 Referer 就把整条播放打成 502。
            _retry = False
            if "403" in str(e) and "Referer" in hdrs:
                hdrs.pop("Referer", None)
                _retry = True
            if not _retry:
                return self._json({"ok": False, "msg": "上游取流失败：%s" % e}, 502)
            try:
                r = opener().open(urllib.request.Request(url, headers=hdrs), timeout=20)
            except Exception as e2:
                return self._json({"ok": False, "msg": "上游取流失败：%s" % e2}, 502)
        try:
            status = int(getattr(r, "status", 200) or 200)
            code = 206 if status == 206 else 200
            ctype = r.headers.get("Content-Type") or "audio/mpeg"
            clen = r.headers.get("Content-Length")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            if clen:
                self.send_header("Content-Length", clen)
            else:
                # 上游没给长度：只能「读到一个字节算一个」，靠关连接告诉客户端结束
                self.send_header("Connection", "close")
                self.close_connection = True
            cr = r.headers.get("Content-Range")
            if cr:
                self.send_header("Content-Range", cr)
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            if head:
                return
            sent = 0
            try:
                while True:
                    buf = r.read(64 * 1024)
                    if not buf:
                        break
                    self.wfile.write(buf)
                    sent += len(buf)
                    if sent > 80 * 1024 * 1024:      # 兜底：不让一条坏流吃满内存/带宽
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log("jy_stream body: %r" % (e,))
        finally:
            try:
                r.close()
            except Exception:
                pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        g = lambda k, d="": (qs.get(k) or [d])[0]
        # parse_qs 默认 keep_blank_values=False，"?q=" 压根不会进 qs ——
        # 「参数写了但是空的」得回到原始 query 里找，否则搜索框清空就被当成「缺少 q」。
        has_q = bool(re.search(r"(?:^|&)q=", u.query or ""))

        def gi(k, d=0):
            """取整数参数：缺失 / 空 / 非数字 都回退 d，绝不抛。
            以前直接 int(g("limit")) ，传个 "abc" 就是 500 —— 畸形参数不该打崩接口。"""
            try:
                return int(g(k, str(d)) or d)
            except (TypeError, ValueError):
                return d

        if u.path in ("/api/search", "/api/match", "/api/cover", "/api/lyric", "/api/song"):
            touch_user()

        try:
            if u.path in ("/api/health", "/health"):
                return self._json({"ok": True, "songs": len(_index),
                                   "cache": CACHE_DIR, "ts": int(time.time())})

            if u.path == "/api/version":
                # musictool 自身版本：两端「关于」页只读展示。
                # py / uptime 用来判断「服务是不是刚重启过」，排障时很有用。
                return self._json({"ok": True, "ver": VERSION,
                                   "py": "%d.%d" % (sys.version_info[0], sys.version_info[1]),
                                   "uptime": int(time.time() - BOOT_TS),
                                   "ts": int(time.time())})

            if u.path == "/api/localsearch":
                # 「参数没给」和「给了空串」要分开：前者是调用方写错（400），
                # 后者是搜索框被清空（200 空结果）—— 不能弹「缺少 q」吓用户。
                if not has_q:
                    return self._json({"ok": False, "msg": "缺少 q"}, 400)
                kw = g("q").strip()
                touch_user()
                if not kw:
                    return self._json({"ok": True, "keyword": "", "local": []})
                return self._json({"ok": True, "keyword": kw,
                                   "local": search_local(kw, gi("limit", 30))})

            if u.path == "/api/widget":
                _host = (self.headers.get("Host") or "127.0.0.1:58091").split(":")[0]
                return self._json(widget_state(g("did"), _host))

            if u.path == "/api/searchall":
                if not has_q:
                    return self._json({"ok": False, "msg": "缺少 q"}, 400)
                kw = g("q").strip()
                touch_user()
                if not kw:      # 空串不去打上游（三源并发很贵），直接空结果
                    return self._json({"ok": True, "keyword": "", "local": [], "online": []})
                local = search_local(kw, gi("limit", 30))
                # JY-SRC: 三源并发（源A/源B/源C），总时长封顶，超时的源直接弃
                online = jy_search_all(kw, gi("olimit", 15))
                return self._json({"ok": True, "keyword": kw,
                                   "local": local, "online": online})

            if u.path == "/api/songfile":
                # PC 端本机播放：按展示名模糊找本地文件，返回可流式播放的 URL
                name = g("name")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                rel = find_song_file_fuzzy(name)
                if not rel:
                    return self._json({"ok": False, "msg": "本地没找到这首歌的文件"}, 404)
                host = (self.headers.get("Host") or "127.0.0.1:58091").split(":")[0]
                url = "http://%s:58090/music/%s" % (host, urllib.parse.quote(rel))
                return self._json({"ok": True, "url": url, "file": rel})

            if u.path == "/api/songurl":
                # PC 端本机播放：解析在线歌曲的真实音频直链
                # JY-SRC: id 可以是 "wy:xxx" / "kw:xxx" / 纯数字(源A)；
                # 本源拿不到就用歌名+歌手逐级换源，歌名会从搜索缓存里反查
                mid = g("id")
                if not mid:
                    return self._json({"ok": False, "msg": "缺少 id"}, 400)
                touch_user()
                src, key = jy_src_of(mid)
                if src == "gqb":
                    try:
                        url, info = resolve_download_url(key)
                        artist = info.get("artist") or ""
                        title = info.get("title") or ""
                        name = ("%s - %s" % (artist, title)) if artist else title
                        return self._json({"ok": True, "url": url, "name": name,
                                           "src": "gqb"})
                    except Exception as _e:
                        log("songurl gqb fail %r, 换源重试" % (_e,))
                song, artist = jy_src_lookup(mid)
                url, got, why, rs, ra = jy_resolve(mid, song, artist)
                song, artist = song or rs, artist or ra
                if not url:
                    return self._json({"ok": False, "msg": why}, 404)
                name = ("%s - %s" % (artist, song)) if artist else song
                return self._json({"ok": True, "url": url, "name": name,
                                   "src": got, "how": why})

            if u.path == "/api/authcheck":
                # 体检：授权文件还在不在、设备端是否还认这张票
                return self._json({"ok": True, **mi_auth_check()})

            if u.path == "/api/sync":
                # 双端输出设备状态（目前只同步「音箱是否启用」）
                return self._json({"ok": True, **read_state()})

            if u.path == "/api/av":
                # 音画补偿：全局默认 + 每首歌一份，App / PC 读写同一份
                return self._json(av_snapshot())
            if u.path == "/api/instr":
                # 纯音乐标记：按歌记一份（标了不弹导入歌词、桌面歌词不显示）
                return self._json(instr_snapshot())

            if u.path == "/api/lrcadj":
                # 逐行歌词时间微调：返回某歌的 {行key: 偏移秒}，两端加载歌词后自行应用
                name = g("name")
                d = lrcadj_load()
                fmap = {}
                if name:
                    fm = (d.get("for") or {}).get(name)
                    if isinstance(fm, dict):
                        fmap = fm
                return self._json({"ok": True, "rev": int(d.get("rev") or 0),
                                    "ts": int(d.get("ts") or 0),
                                    "for": {name: fmap} if name else (d.get("for") or {})})

            if u.path == "/api/time":
                # 给各端对时：拿到服务端的毫秒时间，算本地时钟偏移
                return self._json({"ok": True, "now": group_ms()})

            if u.path == "/api/group":
                # 环绕声同播状态：同一条时间轴 + 成员表 + 每台音量/延迟/声道
                return self._json(group_snapshot())

            if u.path == "/api/tvfollow":
                # 电视歌词屏的跟轴状态：d = 电视位置 − 组播轴，est = 去噪后的估计
                return self._json(tvf_snapshot())

            if u.path == "/api/stem":
                # 分轨状态；build=1 时顺带排队生成（幂等，重复点不会重复跑）
                name = g("name")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                touch_user()
                if g("build") in ("1", "true", "yes"):
                    info = stem_ensure(name)
                else:
                    info = stem_status(name)
                host = (self.headers.get("Host") or "127.0.0.1:58091").split(":")[0]
                info["url"] = {p: stem_url(info["key"], p, host) for p in STEM_PARTS}
                info["label"] = PART_LABEL
                return self._json(info)

            if u.path.startswith("/stem/"):
                # 分轨音频：带 Range，音箱和语音助手都能拖进度
                bits = [x for x in u.path.split("/") if x]
                # ["stem", key, "vocal.mp3"]
                if len(bits) != 3 or not bits[2].endswith(".mp3"):
                    return self._json({"ok": False, "msg": "bad path"}, 400)
                d = stem_dir_of(bits[1])
                part = bits[2][:-4]
                if not d or part not in STEM_PARTS:
                    return self._json({"ok": False, "msg": "bad path"}, 400)
                fp = os.path.join(d, part + ".mp3")
                if not os.path.exists(fp):
                    return self._json({"ok": False, "msg": "还没生成好"}, 404)
                return self._send_file(fp, "audio/mpeg")

            if u.path == "/api/wavcache":
                # 只读盘点 compat/ 下的转码 wav：多大、几首、超没超闸、正被几个进程开着
                try:
                    return self._json(wavcache_snapshot())
                except Exception as e:
                    return self._json({"ok": False, "msg": "%r" % (e,),
                                       "dir": WAVCACHE_DIR, "files": 0, "bytes": 0,
                                       "max_mb": int(WAVCACHE_MAX_MB),
                                       "max_age_d": WAVCACHE_MAX_AGE_D}, 500)

            if u.path == "/api/boxdevices":
                # 当前 xiaomusic 认到的音箱列表（空 = 没登录 / 没绑定）
                try:
                    return self._json({"ok": True, "devices": xm_device_rows(xm_devices()),
                                       "mi_did": xm_mi_did()})
                except Exception as e:
                    return self._json({"ok": False, "msg": "xiaomusic 不可达：%r" % (e,),
                                       "devices": []})

            if u.path == "/api/qr_start":
                # 取一张设备端登录二维码（复用 xiaomusic 的扫码登录通道）
                try:
                    d = json.loads(_http_get(BACKEND_API + "/api/get_qrcode", timeout=25))
                except Exception as e:
                    return self._json({"ok": False, "msg": "取二维码失败：%r" % (e,)}, 500)
                return self._json({"ok": bool(d.get("success")),
                                   "image": d.get("qrcode_url") or "",
                                   "expires": d.get("expire_seconds") or 120,
                                   "msg": d.get("message") or ""})

            if u.path == "/api/trash":
                items = list_trash()
                return self._json({"ok": True, "items": items, "count": len(items)})

            if u.path == "/api/playlists":
                touch_user()
                rows = []
                with _data_lock:
                    metas = {p.get("name"): p for p in _jy["playlists"]}
                for name, meta in metas.items():
                    songs = playlist_songs(name)
                    rows.append({"name": name, "count": len(songs),
                                 "cover": songs[0] if songs else "",
                                 "created": meta.get("created", 0)})
                rows.sort(key=lambda r: r.get("created", 0))
                return self._json({"ok": True, "playlists": rows})

            if u.path == "/api/recent":
                # 最近播放：playlog 里按 last 倒序；可选 min_count 用来出「喜欢/常听」
                try:
                    minc = gi("min_count", 1)
                except Exception:
                    minc = 1
                try:
                    lim = min(300, max(1, gi("limit", 60)))
                except Exception:
                    lim = 60
                with _data_lock:
                    items = [(k, v) for k, v in (_jy.get("playlog") or {}).items()]
                items = [(k, v) for k, v in items if (v or {}).get("count", 0) >= minc]
                items.sort(key=lambda kv: -((kv[1] or {}).get("last", 0)))
                rows = [{"name": k, "count": (v or {}).get("count", 0),
                         "last": (v or {}).get("last", 0),
                         "week": _weekly(v)} for k, v in items[:lim]]
                return self._json({"ok": True, "songs": rows})

            if u.path == "/api/playlist":
                name = g("name")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                return self._json({"ok": True, "name": name, "songs": playlist_songs(name)})

            if u.path == "/api/recommend":
                touch_user()
                # JY-TAGMAP（v4.00 契约，前端按这个传，别改口径）：
                #   tagmode=auto  → 用户没选任何标签：**忽略**服务端保存的 _jy["tags"]，
                #                   用当日默认结果（这就是「取消选择恢复默认」）。
                #   tagmode=pick  → 按 tags= 这些标签影响推荐（歌单层 + 歌曲层）。
                #   缺 tagmode    → 老行为：tags 为空时回退 _jy["tags"]（兼容旧客户端）。
                tagmode = (g("tagmode") or "").strip().lower()
                if tagmode not in ("auto", "pick"):
                    tagmode = ""
                tags = [t.strip() for t in (g("tags") or "").split(",") if t.strip()]
                if tagmode == "auto":
                    tags = []                       # 明确：以「无标签」出当日默认结果
                elif tagmode == "pick":
                    pass                            # 前端给了什么就用什么（可能是空）
                elif not tags:
                    with _data_lock:
                        tags = list(_jy.get("tags") or [])
                # 红心存在前端 localStorage（服务端看不到），跟着请求一起带上来；
                # 分隔符用 | —— 歌名里带逗号的情况太多，用逗号会把一首歌拆成两首。
                likes = [x for x in (g("likes") or "").split("|") if x.strip()]
                # limit 传 "abc" 之类不能直接 500：gi() 兜底成默认值
                _lim = gi("limit", 12)
                return self._json({"ok": True,
                                   **recommend_sf(g("q"), _lim, tags=tags,
                                                 seed=g("seed") or "", likes=likes,
                                                 tagmode=tagmode or None)})

            # JY-MORE: 每日推荐「下拉加载更多」。
            #   不从零再算一次推荐（那要重新搜上游，3~10 秒），而是从 recommend() 存下来的
            #   全量排序池（online/local 各最多 60 条）里切片 —— 秒回，且不会跟首屏重复。
            if u.path == "/api/recommend_more":
                touch_user()
                typ = (g("type") or "online").strip()
                if typ not in ("online", "local"):
                    typ = "online"
                try:
                    skip = max(0, gi("skip", 0))
                except Exception:
                    skip = 0
                try:
                    lim = max(1, min(50, gi("limit", 12)))
                except Exception:
                    lim = 12
                with _data_lock:
                    allp = _daily_cache.get("all")
                    if not allp:                       # 只有 out 也先顶上，别白跑一趟
                        _o = _daily_cache.get("out") or {}
                        allp = {"online": _o.get("online") or [],
                                "local": _o.get("local") or []}
                if not allp.get("online") and not allp.get("local"):
                    # 冷启动（服务刚起、预热没跑完）：现算一次，前端那边有 loading 态
                    try:
                        recommend_sf()
                    except Exception as e:
                        log("recommend_more recompute: %r" % (e,))
                    with _data_lock:
                        allp = _daily_cache.get("all") or {}
                rows = allp.get(typ) or []
                chunk = rows[skip:skip + lim]
                return self._json({"ok": True, "type": typ, "skip": skip,
                                   "total": len(rows), "items": chunk,
                                   "has_more": (skip + len(chunk)) < len(rows)})

            # ---------------- JY-VOL: 音量代理
            # 前端 remote.html / pc.html 的 post("/setvolume") 打的是 58091 这个口，
            # 但以前这里没路由，直接 404 → 表现为「拖音量条没反应」。转给 xiaomusic。
            if u.path in ("/getvolume", "/api/getvolume"):
                did = g("did") or _default_did()
                try:
                    with urllib.request.urlopen(
                            BACKEND_API + "/getvolume?did=" + urllib.parse.quote(did),
                            timeout=8) as r:
                        out = json.loads(r.read().decode("utf-8", "ignore") or "{}")
                    return self._json({"ok": True, **out})
                except Exception as e:
                    return self._json({"ok": False, "msg": "读音量失败：%s" % e}, 502)

            if u.path == "/api/recsources":
                cfg = rec_sources_cfg()
                names = ("lastfm", "netease", "listenbrainz", "embeat", "qqmusic")

                def _info(name):
                    c = cfg.get(name) or {}
                    d = {"enabled": bool(c.get("enabled")),
                         "has_key": bool(c.get("api_key") or c.get("cookie")
                                        or c.get("user") or c.get("url")
                                        or c.get("uin"))}
                    # 熔断状态（v4.01）：cooling/retry_in 让前端和排查的人一眼看出
                    # 「现在是在等上游，不是推荐算不出来」。
                    d.update(_brk_state(name))
                    return d

                return self._json({"ok": True,
                                   "sources": {n: _info(n) for n in names},
                                   # 搜索源不是「外部召回源」，单独放一格。
                                   # 本仓库不含在线检索实现，这里恒为关闭。
                                   "search": {"name": "none", "enabled": False,
                                              "reason": _JY_NO_SOURCE_MSG},
                                   "timeout": _rs_get("timeout", default=4.0),
                                   "stats": _rec_stats.get("last", {}),
                                   "errors": _rec_stats.get("errors", {})})

            if u.path == "/api/sources":
                # JY-SRC 音源开关与探活：?probe=1 才真发请求（默认只看配置，秒回）
                cfg = jy_src_load()
                out = {"ok": True, "order": JY_SRC_ORDER,
                       "enabled": cfg, "cap": JY_SRC_CAP, "wait": JY_SRC_WAIT}
                if g("probe") in ("1", "true", "yes"):
                    out["health"] = jy_src_health()
                return self._json(out)

            if u.path == "/api/tags":
                with _data_lock:
                    return self._json({"ok": True, "tags": list(_jy.get("tags") or [])})

            if u.path == "/api/search":
                if not has_q:
                    return self._json({"ok": False, "msg": "缺少 q"}, 400)
                kw = g("q").strip()
                if not kw:
                    return self._json({"ok": True, "keyword": "", "results": []})
                # JY-SRC: 这是 pc.html / remote.html 搜歌下载走的主路径，必须多源
                return self._json({"ok": True, "keyword": kw,
                                   "results": jy_search_all(kw, gi("limit", 20))})

            if u.path == "/api/warm":
                if not _warm["running"]:
                    threading.Thread(target=warm_worker,
                                     kwargs={"delay": float(g("delay", "1.5") or 1.5)},
                                     daemon=True).start()
                return self._json({"ok": True, "started": True, **_warm})

            if u.path == "/api/warmstatus":
                return self._json({"ok": True, **_warm})

            if u.path == "/api/local":
                # JY-FAST: 曲库清单 + 每首的封面/歌词有无。
                # 缓存键改成「内容签名」（首数 + 总字节 + 名字指纹）—— 原来用
                # _files["ts"]（本地清单缓存的时间戳），那个每 120s 就会变一次，
                # 于是每 2 分钟的第一发请求都要整表重算。内容签名只要曲库没变就不变。
                _lrows = local_files()
                songs = [r[0] for r in _lrows]
                ck = "%d|%d|%d" % (len(_lrows), sum(r[2] for r in _lrows),
                                   hash("|".join(songs)) & 0x7FFFFFFF)
                rows = _local_rows["rows"] if _local_rows["key"] == ck else None
                if rows is None:      # JY-FAST: 构建一次缓存，曲库刷新自动失效
                    # 封面/高清封面一律查索引（O(1) 字典），不再逐首 stat。
                    # 老版每首 `any(os.path.exists(_p) for _p in cover_cache_paths(s))`
                    # 要试 14 条路径，581 首光是 stat 就上千次；再叠上
                    # `hi_cover_exists` 的「查不到就重建索引」，冷调用 11.1s。
                    cidx = _ensure_cover_index() or {}
                    # 「普通封面有没有」必须和原来 cover_cache_paths() 完全等价：
                    # 候选全都在 COVER_DIR 里，所以 exists(p) == basename(p) ∈ listdir。
                    # 一次 listdir 换掉每首 14 次 stat，语义零变化
                    # （踩过坑：直接拿 _track_norm 索引判，会有 10 首带全角括号/双空格的
                    #   歌因为 sanitize_filename 与归一化口径不同而丢 cover 标记）。
                    try:
                        cover_names = set(os.listdir(COVER_DIR))
                    except OSError:
                        cover_names = set()
                    try:
                        lrc_have = set(os.listdir(LRC_DIR))
                    except OSError:
                        lrc_have = set()
                    rows = []
                    for s in songs:
                        _a, _t = display_meta(s)     # JY-TAG: 内嵌标签优先，前端直接用
                        _ce = cidx.get(_track_norm(s)) or {}
                        _hi = bool(_ce.get("hi"))
                        _cov = any(os.path.basename(_p) in cover_names
                                   for _p in cover_cache_paths(s)) or _hi
                        rows.append({
                            "name": s,
                            "artist": _a,
                            "title": _t,
                            "cover": _cov,
                            "hicover": _hi,
                            "lrc": (sanitize_filename(s) + ".lrc") in lrc_have,
                        })
                    _local_rows["key"] = ck
                    _local_rows["rows"] = rows
                return self._json({"ok": True, "total": len(rows), "songs": rows})

            if u.path == "/api/match":
                name = g("name")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                return self._json({"ok": True, **match_assets(name)})

            if u.path == "/api/cover":
                name = g("name")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                hi = b""
                if g("big"):      # JY-TAG: 大图要高清（内嵌 ≥600 或源B 1024）
                    hp = ensure_hi_cover(name, allow_net=False)   # 先查现成的，毫秒级
                    if not hp:
                        # 没有现成的就别卡住界面：甩到后台去抓，这次先给普通封面
                        threading.Thread(target=ensure_hi_cover, args=(name, True),
                                         daemon=True).start()
                    if hp and os.path.getsize(hp) > 3000:
                        try:
                            with open(hp, "rb") as f:
                                hi = f.read()
                        except Exception:
                            hi = b""
                norm = serve_cover(name) or b""
                # JY-FIX: big=1 绝不低于普通封面（高清图没抓到、退回更小内嵌图时会这样），
                # 两张都取，按最长边像素挑更清晰的那个下发
                data = hi if _img_side(hi) >= _img_side(norm) else norm
                if not data:
                    return self._json({"ok": False, "msg": "no cover"}, 404)
                self.send_response(200)
                ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else (
                    "image/gif" if data[:3] == b"GIF" else "image/jpeg")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self._cors()
                self.end_headers()
                return self.wfile.write(data)

            if u.path == "/api/jy_stream":
                # JY-STREAM：在线歌「本地没有就直接播」用的稳定流地址（服务端代理，不落盘）。
                # 给音箱的是一个稳定的 http://<板子IP>:58091/api/jy_stream?id=... ，
                # 字节流由 musictool 自己去上游取，详见 Handler._jy_stream。
                touch_user()
                return self._jy_stream(head=False)

            if u.path == "/api/online_cover":
                # 在线搜索结果的封面：歌曲还没下载，按 id 去源站详情页取（带内存缓存）
                mid = (g("id") or "").strip()
                if not mid:
                    return self._json({"ok": False, "msg": "缺少 id"}, 400)
                touch_user()
                try:
                    data = online_cover_bytes(mid)
                except Exception as e:
                    return self._json({"ok": False, "msg": str(e)}, 404)
                self.send_response(200)
                ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else (
                    "image/gif" if data[:3] == b"GIF" else "image/jpeg")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self._cors()
                self.end_headers()
                return self.wfile.write(data)

            if u.path == "/api/plcover":
                # 歌单配图：没有封面的歌单按名字/关键词找一张（见 plcover_bytes）。
                # 回的是图片字节而不是 JSON —— <img src> 直接用。
                key = (g("k") or g("name") or "").strip()
                if not key:
                    return self._json({"ok": False, "msg": "缺少 name / k"}, 400)
                try:
                    data = plcover_bytes(key)
                except Exception as e:
                    return self._json({"ok": False, "msg": str(e)}, 404)
                self.send_response(200)
                ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else (
                    "image/gif" if data[:3] == b"GIF" else "image/jpeg")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                # 一张图只抓一次，之后浏览器一个月内都不用再问
                self.send_header("Cache-Control", "public, max-age=2592000")
                self._cors()
                self.end_headers()
                return self.wfile.write(data)

            if u.path == "/api/avatar/info":
                ts = avatar_ts()
                return self._json({"ok": True, "ts": ts, "has": bool(ts)})

            if u.path == "/api/avatar":
                data = avatar_read()
                if not data:
                    return self._json({"ok": False, "msg": "no avatar"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                return self.wfile.write(data)

            if u.path == "/api/lyric":
                name = g("name")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                lrc, artist = serve_lyric(name)
                return self._json({"ok": True, "name": name, "lrc": lrc, "artist": artist})

            if u.path == "/api/lyric/report":
                # 歌词报错清单：默认只给未处理的，?all=1 连已修的一起给
                d = lrc_report_load()
                allf = g("all") in ("1", "true", "yes")
                items = [x for x in d["items"]
                         if allf or str(x.get("state") or "new") != "done"]
                items.sort(key=lambda x: (-int(x.get("count") or 1),
                                          -int(x.get("ts") or 0)))
                return self._json({"ok": True, "items": items,
                                   "total": len(items), "kinds": list(LRC_KINDS)})

            if u.path == "/api/lvideo":
                name = g("name")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                st = lyric_studio()
                if st is None:
                    return self._json({"ok": False, "msg": "服务端不支持歌词视频"}, 501)
                try:
                    prio = int(g("prio", "0") or 0)
                except ValueError:
                    prio = 0
                ready = st.ready(name)
                if not ready and not g("nowarm"):
                    st.request(name, prio)
                host = (self.headers.get("Host") or "127.0.0.1:58091").split(":")[0]
                meta = st.meta(name)
                return self._json({
                    "ok": True,
                    "name": name,
                    "ready": ready,
                    "url": "http://%s:%d/lvideo/%s.mp4" % (host, PORT, st.key(name)),
                    "noLrc": st.known_none(name),
                    "accent": meta.get("accent") or "",
                    "dur": meta.get("dur") or 0,
                    "bytes": meta.get("bytes") or 0,
                })

            if u.path == "/api/lvideo/status":
                st = lyric_studio()
                if st is None:
                    return self._json({"ok": False, "msg": "not supported"}, 501)
                return self._json({"ok": True, **st.status()})

            if u.path.startswith("/lvideo/"):
                key = u.path[len("/lvideo/"):].split(".")[0]
                st = lyric_studio()
                p = st.path_by_key(key) if st else ""
                if not p:
                    return self._json({"ok": False, "msg": "no video"}, 404)
                return self._send_file(p, "video/mp4")

            if u.path == "/api/song":
                mid = g("id")
                if not mid:
                    return self._json({"ok": False, "msg": "缺少 id"}, 400)
                return self._json({"ok": True, **fetch_song(mid)})

            if u.path == "/api/meta":
                name = g("name")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                m = song_meta(name) or {}
                _a, _t = display_meta(name)
                return self._json({"ok": True, "name": name, "artist": _a, "title": _t,
                                   "album": m.get("album") or "", "dur": m.get("dur") or 0,
                                   "hi": hi_cover_exists(name)})

            if u.path == "/api/metawarm":
                if not _mw["running"]:
                    threading.Thread(target=meta_worker,
                                     kwargs={"allow_net": g("net", "1") != "0"},
                                     daemon=True).start()
                return self._json({"ok": True, **(_mw)})

            if u.path == "/api/metastatus":
                return self._json({"ok": True, **_mw,
                                   "hi_total": len(os.listdir(COVER_HI_DIR))})

            if u.path in ("/", "/index.html"):
                return self._text(PAGE, "text/html; charset=utf-8")

            # ---------------- JY-SWAP 换源工作台
            if u.path in ("/swap", "/swap/", "/swap.html"):
                p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swap.html")
                if not os.path.isfile(p):
                    return self._json({"ok": False, "msg": "swap.html 不在"}, 404)
                with open(p, "rb") as f:
                    return self._text(f.read(), "text/html; charset=utf-8")

            if u.path.startswith("/api/swap/"):
                if _swapmod is None:
                    return self._json({"ok": False, "msg": "换源模块没加载"}, 501)
                if u.path == "/api/swap/list":
                    return self._json(_swapmod.api_rows(g("filter") or "bad", g("q") or ""))
                if u.path == "/api/swap/pending":
                    touch_user()
                    return self._json(_swapmod.api_pending())
                if u.path == "/api/swap/history":
                    return self._json(_swapmod.api_history())

            return self._json({"ok": False, "msg": "not found"}, 404)
        except Exception as e:
            log("GET %s error %r" % (u.path, e))
            return self._json({"ok": False, "msg": str(e)}, 500)

    def do_HEAD(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/jy_stream":
            # 有些播放器会先发 HEAD 探格式/长度，不接管的话会拿到 501 → 直接放弃播放
            return self._jy_stream(head=True)
        if u.path.startswith("/lvideo/"):
            key = u.path[len("/lvideo/"):].split(".")[0]
            st = lyric_studio()
            p = st.path_by_key(key) if st else ""
            if not p:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()
                return
            return self._send_file(p, "video/mp4", head=True)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(ln) if ln else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except Exception:
                payload = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}

            if u.path == "/api/avatar":
                if payload.get("clear"):
                    try:
                        os.remove(AVATAR_FILE)
                    except Exception:
                        pass
                    return self._json({"ok": True, "ts": 0})
                ok, msg = avatar_save(payload.get("img") or "")
                return self._json({"ok": ok, "msg": msg, "ts": avatar_ts()},
                                  200 if ok else 400)

            if u.path == "/api/lyric/save":
                name = str(payload.get("name") or "").strip()
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                txt = str(payload.get("lrc") or payload.get("text") or "")
                if not txt.strip():
                    return self._json({"ok": False, "msg": "歌词是空的"}, 400)
                mode = str(payload.get("mode") or "auto")
                try:
                    dur = float(payload.get("dur") or 0)
                except Exception:
                    dur = 0.0
                # 以文件真实时长为准：前端给的可能是别的歌留下的值，
                # 拿它排轴会把整段歌词压在半首歌里
                try:
                    _ap = audio_path_of(name)
                    if _ap and os.path.exists(_ap):
                        _d = _probe_dur(_ap)
                        if _d > 5:
                            dur = _d
                except Exception:
                    pass
                if mode != "raw":
                    txt = lrc_auto(txt, dur)          # 纯文字排轴；已有的时间戳当锚点保留
                ok, msg = lyric_save(name, txt)
                if not ok:
                    return self._json({"ok": False, "msg": msg}, 500)
                return self._json({"ok": True, "name": name, "lrc": txt,
                                   "lines": len([x for x in txt.split("\n") if x.strip()])})

            if u.path == "/api/lyric/report":
                # 两端标记歌词错误：{name,t,text,kind,delta,tail,fix,note,by}
                hit, msg = lrc_report_add(payload)
                if hit is None:
                    return self._json({"ok": False, "msg": msg}, 400)
                return self._json({"ok": True, "id": hit["id"],
                                   "count": hit["count"], "state": hit["state"]})

            if u.path == "/api/lyric/report/done":
                # 处理完勾掉：{ids:[...]} 或 {name:"..."}；{state:"skip"} 标为不修
                ids = payload.get("ids") or []
                if isinstance(ids, str):
                    ids = [ids]
                st = str(payload.get("state") or "done")
                if st not in ("done", "skip", "new"):
                    st = "done"
                n = lrc_report_done(ids, str(payload.get("name") or ""), st)
                return self._json({"ok": True, "changed": n, "state": st})

            if u.path == "/api/lvideo/warm":
                st = lyric_studio()
                if st is None:
                    return self._json({"ok": False, "msg": "not supported"}, 501)
                names = payload.get("names") or []
                if isinstance(names, str):
                    names = [names]
                prio = int(payload.get("prio") or 2)
                hit = 0
                for i, nm in enumerate(names[:6]):
                    if nm and st.request(str(nm), min(prio + i, 3)):
                        hit += 1
                return self._json({"ok": True, "queued": hit, **st.status()})

            if u.path == "/api/tvfollow":
                # 前端投完屏调一次：{on:true, rid, name, video} 让服务端接管跟轴；
                # {on:false} 收工（顺便把电视音量还回去）。
                # 不传 rid 时自动挑一台正在放的电视。
                on = bool(payload.get("on"))
                if not on:
                    return self._json(tvf_set(False))
                rid = str(payload.get("rid") or "")
                name = str(payload.get("name") or "")
                if not rid:
                    for r in tv_renderers():
                        st = tv_status(r.get("id") or "")
                        if st and st.get("playing"):
                            rid = r.get("id")
                            break
                if not rid:
                    return self._json({"ok": False, "msg": "没找到正在播放的电视"}, 404)
                if not name:
                    name = read_group().get("name") or ""
                vid = payload.get("video")
                return self._json(tvf_set(True, rid, name,
                                          video=(None if vid is None else bool(vid)),
                                          mute=(payload.get("mute") is not False)))

            if u.path == "/api/stem":
                # 排队生成分轨（幂等）。返回当前状态，前端拿着轮询 pct。
                name = str(payload.get("name") or "")
                if not name:
                    return self._json({"ok": False, "msg": "缺少 name"}, 400)
                touch_user()
                info = stem_ensure(name)
                host = (self.headers.get("Host") or "127.0.0.1:58091").split(":")[0]
                info["url"] = {p: stem_url(info["key"], p, host) for p in STEM_PARTS}
                info["label"] = PART_LABEL
                return self._json(info)

            if u.path == "/api/download":
                touch_user()
                mid = str(payload.get("id") or "")
                if not mid:
                    return self._json({"ok": False, "msg": "缺少 id"}, 400)
                # JY-SRC: 支持 wy:/kw: 前缀；源A失败会自动换源重试
                res = jy_download(mid, payload.get("name"),
                                  payload.get("song") or "", payload.get("artist") or "")
                local_files_invalidate()
                refresh_backend()
                return self._json({"ok": True, **res})

            if u.path == "/api/play_online":
                # 在线歌曲点击即播（v4.00a 口径）：
                #   · 本地已有 → 直接播本地（返回 {local:true,name}）
                #   · 本地没有 + **默认** → 老行为（jy_download + refresh_backend），返回 name
                #   · 本地没有 + {"stream": true} → 只播不落盘，返回 jy_stream 的稳定地址
                #   · 无损（flac/ape/aiff）→ stream 模式自动回落下载（音箱放不了无损）
                # ⚠ 默认是**下载**不是 stream：前端只认 name，默认给 url 会让点歌没反应，
                #   详见 _play_online_mode() 的注释。
                touch_user()
                mid = str(payload.get("id") or "")
                if not mid:
                    return self._json({"ok": False, "msg": "缺少 id"}, 400)
                want_stream = _play_online_mode(payload) == "stream"
                want = payload.get("name") or ""
                if not want:
                    # JY-SRC: 只有源A的 id 才经得起 fetch_song，wy:/kw: 走缓存反查
                    _s, _a = jy_src_of(mid)
                    if _s == "gqb":
                        try:
                            _i = fetch_song(_a)
                            want = "%s - %s" % (_i.get("artist") or "", _i.get("title") or "")
                        except Exception:
                            pass
                    if not want:
                        _song, _art = jy_src_lookup(mid)
                        want = ("%s - %s" % (_art, _song)) if _art else _song
                # 本地已下载过同首歌（按 歌手 - 歌名 模糊比对，见 _local_hit）
                artist, song = split_title(want) if want else ("", "")
                probe = song or want
                # 本地已有同一首歌就直接播。判断必须带歌手：多源列表里同名不同版本太多，
                # 旧的「歌名片段包含」会把《孤勇者 (Single Version) 田一名》认成
                # 《孤勇者 陈奕迅》，点了 A 唱出 B。
                hit, _best = _local_hit(probe, artist)
                if hit:
                    return self._json({"ok": True, "local": True, "name": hit,
                                       "score": round(float(_best or 0.0), 3)})
                if want_stream:
                    # ---- stream 分支：先看拿到的是不是无损；是就回落下载 ----
                    _u2, _src, _note = "", "", ""
                    try:
                        _u2, _src, _note, _s2, _a2 = jy_resolve(mid, song, artist)
                    except Exception as e:
                        _note = "取直链失败：%r" % (e,)
                    _ext = _url_ext(_u2)
                    if _u2 and _ext not in _LOSSLESS_EXT:
                        _base = jy_stream_base(self.headers.get("Host") or "")
                        return self._json({
                            "ok": True, "local": False, "stream": True,
                            "url": "%s/api/jy_stream?id=%s" % (
                                _base, urllib.parse.quote(mid, safe="")),
                            "fmt": _ext or ".mp3", "src": _src or "",
                            # v4.01：前端要用这三个把「当前曲显示名」钉准 + 用真实总长当
                            # 进度轴（设备回报的 cur_music 可能直接是那条流地址，认不得）。
                            # 拿不到就留空串/0，别编。
                            "title": song or _s2 or "",
                            "artist": artist or _a2 or "",
                            "dur": 0,
                            "msg": "在线播放（不占本地空间）"})
                    # 无损 / 取不到直链 → 走下载（下载完 refresh 才有本地文件可播）
                    _fallback = "flac" if _ext in _LOSSLESS_EXT else "resolve"
                    try:
                        _song, _art = jy_src_lookup(mid)
                        res = jy_download(mid, want or None, _song or song, _art or artist)
                        local_files_invalidate()
                        refresh_backend()
                        return self._json({
                            "ok": True, "local": False, "name": res.get("file"),
                            "existed": res.get("existed", False),
                            "fallback": _fallback,
                            "msg": ("无损格式已自动下载后播放" if _fallback == "flac"
                                    else "取不到在线直链，已下载后播放")})
                    except Exception as e:
                        return self._json({"ok": False, "fallback": _fallback,
                                           "msg": "取不到可播地址：%s" % (_note or e)}, 502)
                # ---- download 分支：保持老行为 ----
                # JY-SRC: wy:/kw:/源A 统一入口，取不到自动换源
                # v4.01：这里以前裸着抛，上游一挂就回 500 + 原始异常串
                # （`HTTP Error 520: <none>`），前端只能显示「点了没反应」。
                # 现在包成 502 + 人话 msg + reason，原始异常只用日志留证。
                try:
                    _song, _art = jy_src_lookup(mid)
                    res = jy_download(mid, want or None, _song, _art)
                    local_files_invalidate()
                    refresh_backend()
                    return self._json({"ok": True, "local": False, "name": res.get("file"),
                                       "existed": res.get("existed", False)})
                except Exception as e:
                    _msg, _reason = _dl_err(e)
                    try:
                        log("play_online download fail id=%s: %r" % (mid, e))
                    except Exception:
                        pass
                    return self._json({"ok": False, "msg": _msg, "reason": _reason}, 502)

            if u.path == "/api/playlog":
                name = str(payload.get("name") or "")
                if name:
                    # dur/played = 这一趟听歌的结果（总长 / 实际听了多少秒），前端切歌时补报；
                    # 只带 name 就是起播打点（老客户端照旧能用）
                    record_play(name, payload.get("dur"), payload.get("played"))
                return self._json({"ok": True})

            if u.path == "/api/metas":
                # 批量取元信息：列表一次要几百首，一首一个请求太碎
                names = payload.get("names") or []
                if not isinstance(names, list):
                    return self._json({"ok": False, "msg": "names 必须是数组"}, 400)
                items = {}
                dirty = 0
                for nm in names[:400]:
                    if not isinstance(nm, str) or not nm:
                        continue
                    row = song_meta(nm)
                    if row:
                        items[nm] = {k: row.get(k) for k in ("dur", "size", "album", "artist", "bit", "fmt")}
                        dirty += 1
                if dirty:
                    save_meta()
                return self._json({"ok": True, "items": items})

            if u.path == "/api/group":
                # 任意一端换歌/跳进度/改成员都写这里：写进去的是锚点，不是心跳
                return self._json(group_snapshot(
                    write_group(payload, str(payload.get("by") or ""))))

            if u.path == "/api/sync":
                # 任意一端切了输出设备就写这里，另一端轮询到 rev 变化后跟着改
                box = payload.get("box")
                if isinstance(box, str):
                    box = box.strip().lower() in ("1", "true", "yes", "box", "on")
                elif box is not None:
                    box = bool(box)
                st = write_state(box, str(payload.get("by") or ""))
                return self._json({"ok": True, **st})

            if u.path == "/api/av":
                # 音画补偿：{lag:数值|null, for:{歌名:数值|null}}，任一端改了另一端很快就看到
                return self._json(av_snapshot(av_write(payload)))
            if u.path == "/api/instr":
                # 纯音乐标记：{for:{歌名:0|1|null}}，App / PC 读写同一份
                return self._json(instr_snapshot(instr_write(payload)))

            if u.path == "/api/lrcadj":
                # 逐行歌词时间微调：{for:{歌名:{行key:偏移秒}}}，0/null 删除该行
                return self._json(lrcadj_snapshot(lrcadj_write(payload)))

            if u.path == "/api/recsources":
                raw = payload or {}
                cur = rec_sources_cfg()
                for name in ("lastfm", "netease", "listenbrainz", "embeat", "qqmusic"):
                    if name in raw and isinstance(raw[name], dict):
                        c = dict(cur.get(name) or {})
                        for k in ("enabled", "api_key", "cookie", "user", "url", "uin"):
                            if k in raw[name]:
                                c[k] = raw[name][k]
                        cur[name] = c
                if "timeout" in raw:
                    try:
                        cur["timeout"] = max(1.0, min(20.0, float(raw["timeout"])))
                    except Exception:
                        pass
                try:
                    with open(_REC_SRC_FILE, "w", encoding="utf-8") as f:
                        json.dump(cur, f, ensure_ascii=False, indent=2)
                    rec_sources_cfg(refresh=True)
                    with _rec_cache_lock:
                        _rec_cache["key"] = ""        # 配置变了，外部召回缓存失效
                    return self._json({"ok": True})
                except Exception as e:
                    return self._json({"ok": False, "msg": str(e)}, 500)

            if u.path == "/api/sources":
                # JY-SRC: 开关某个音源。关掉之后搜索少一路、取链也跳过它
                raw = payload or {}
                try:
                    cfg = jy_src_save({k: bool(v) for k, v in raw.items()
                                       if k in JY_SRC_NAME})
                    return self._json({"ok": True, "enabled": cfg})
                except Exception as e:
                    return self._json({"ok": False, "msg": str(e)}, 500)

            if u.path == "/api/tags":
                raw = payload.get("tags") or []
                tags = []
                for t in raw:
                    t = str(t).strip()
                    if t and t not in tags and len(t) <= 12:
                        tags.append(t)
                with _data_lock:
                    _jy["tags"] = tags[:16]
                    save_data()
                return self._json({"ok": True, "tags": tags[:16]})

            # ---------------- JY-VOL: 前端音量入口（58091 上的 /setvolume）
            if u.path in ("/setvolume", "/api/setvolume"):
                try:
                    vol = int(round(float(payload.get("volume"))))
                except Exception:
                    return self._json({"ok": False, "msg": "volume 不对"}, 400)
                vol = max(0, min(100, vol))
                did = str(payload.get("did") or "") or _default_did()
                if not did:
                    return self._json({"ok": False, "msg": "没有绑定设备"}, 400)
                set_volume_async(did, vol)
                return self._json({"ok": True, "volume": vol, "did": did})

            if u.path == "/api/playlist_new":
                name = sanitize_filename(str(payload.get("name") or ""))
                if not name:
                    return self._json({"ok": False, "msg": "歌单名不能为空"}, 400)
                d = playlist_dir(name)
                if os.path.isdir(d):
                    return self._json({"ok": False, "msg": "歌单已存在"}, 400)
                try:
                    os.makedirs(d)
                except Exception as e:
                    return self._json({"ok": False, "msg": "创建失败：%r" % (e,)}, 500)
                with _data_lock:
                    _jy["playlists"].append({"name": name, "created": int(time.time())})
                    save_data()
                refresh_backend()
                return self._json({"ok": True, "name": name})

            if u.path == "/api/playlist_del":
                name = str(payload.get("name") or "")
                d = playlist_dir(name)
                if os.path.isdir(d):
                    # 歌单里是副本，清掉目录不会伤到根目录下的原文件
                    try:
                        for fn in os.listdir(d):
                            fp = os.path.join(d, fn)
                            if os.path.islink(fp) or os.path.isfile(fp):
                                os.remove(fp)
                        os.rmdir(d)
                    except Exception as e:
                        return self._json({"ok": False, "msg": "删除失败：%r" % (e,)}, 500)
                with _data_lock:
                    _jy["playlists"] = [p for p in _jy["playlists"] if p.get("name") != name]
                    save_data()
                refresh_backend()
                return self._json({"ok": True})

            if u.path == "/api/playlist_rename":
                name = str(payload.get("name") or "")
                new = sanitize_filename(str(payload.get("new_name") or ""))
                if not new:
                    return self._json({"ok": False, "msg": "新名字不能为空"}, 400)
                d, nd = playlist_dir(name), playlist_dir(new)
                if not os.path.isdir(d):
                    return self._json({"ok": False, "msg": "歌单不存在"}, 404)
                if os.path.isdir(nd):
                    return self._json({"ok": False, "msg": "新名字已被占用"}, 400)
                try:
                    os.rename(d, nd)
                except Exception as e:
                    return self._json({"ok": False, "msg": "重命名失败：%r" % (e,)}, 500)
                with _data_lock:
                    for p in _jy["playlists"]:
                        if p.get("name") == name:
                            p["name"] = new
                    save_data()
                refresh_backend()
                return self._json({"ok": True, "name": new})

            if u.path == "/api/playlist_add":
                name = str(payload.get("name") or "")
                song = str(payload.get("song") or "")
                res = add_to_playlist(name, song)
                if res.get("ok"):
                    refresh_backend()
                return self._json(res)

            if u.path == "/api/playlist_remove":
                name = str(payload.get("name") or "")
                song = str(payload.get("song") or "")
                res = remove_from_playlist(name, song)
                if res.get("ok"):
                    refresh_backend()
                return self._json(res)

            if u.path == "/api/refresh":
                _file_index["ts"] = 0.0          # 曲库变了，索引与元信息缓存都作废
                _meta_miss.clear()
                drop_local_files()
                return self._json({"ok": refresh_backend()})

            if u.path == "/api/autofix":
                # 一键自检修复（耗时可达 1-2 分钟，客户端超时要给足）
                return self._json(autofix())

            if u.path == "/api/save_login":
                # App 内账号直接授权：WebView 登录页回调提取 token，
                # 写入 auth.json 并重启 xiaomusic 使其生效
                pass_token = str(payload.get("passToken") or "").strip()
                user_id = str(payload.get("userId") or "").strip()
                if not pass_token or not user_id:
                    return self._json({"ok": False, "msg": "授权信息不完整"}, 400)
                conf = os.environ.get(
                    "JY_CONF",
                    "/root/xiaomusic_conf")
                auth_file = os.path.join(conf, "auth.json")
                try:
                    old = {}
                    if os.path.isfile(auth_file):
                        with open(auth_file, "r", encoding="utf-8") as f:
                            old = json.load(f)
                    bak = auth_file + ".bak"
                    if os.path.isfile(auth_file):
                        os.rename(auth_file, bak)
                    new = dict(old)
                    new["passToken"] = pass_token
                    new["userId"] = user_id
                    if str(payload.get("cUserId") or "").strip():
                        new["cUserId"] = str(payload.get("cUserId")).strip()
                    if str(payload.get("deviceId") or "").strip():
                        new["deviceId"] = str(payload.get("deviceId")).strip()
                    new["passInfo"] = "login-end"
                    # 扫码拿到的只有 passToken，配套的旧 micoapi（ssecurity+serviceToken）必须一并作废。
                    # 留着的话下次启动会先复用这份死 token，白等一次 401 才重登 ——
                    # 历史上「扫码了、也提示授权成功，可音箱还是离线」就是这么来的。
                    for _k in ("ssecurity", "serviceToken"):
                        new.pop(_k, None)
                    with open(auth_file, "w", encoding="utf-8") as f:
                        json.dump(new, f, ensure_ascii=False, indent=2)
                    # .mi.token 缓存的是旧会话的 micoapi，同样作废（先留档再删，出问题能还原）
                    try:
                        _mt = os.path.join(conf, ".mi.token")
                        if os.path.isfile(_mt):
                            shutil.copy2(_mt, _mt + ".bak_beforelogin")
                            os.remove(_mt)
                            log("save_login: 已作废旧 .mi.token 缓存")
                    except Exception as _e2:
                        log("save_login: 清理 .mi.token 失败 %r" % (_e2,))
                    log("save_login: auth.json updated (userId=%s), restarting xiaomusic" % user_id)
                except Exception as e:
                    return self._json({"ok": False, "msg": "写入授权失败：%r" % (e,)}, 500)
                # 重启 xiaomusic 让新登录态生效（异步，先返回响应）
                threading.Thread(target=_restart_backend, daemon=True).start()
                return self._json({"ok": True, "msg": "授权成功，服务重启中"})

            if u.path == "/api/delsong":
                # 删除歌曲：移入回收站（不销毁），并让 xiaomusic 重扫曲库
                touch_user()
                res = trash_song(name=payload.get("name"), rel=payload.get("file") or payload.get("rel"))
                if res.get("ok"):
                    drop_local_files()      # 立刻失效曲库缓存，列表/搜索马上干净
                    _stop_if_current(payload.get("name") or "")
                    threading.Thread(target=refresh_backend, daemon=True).start()
                return self._json(res, 200 if res.get("ok") else 400)

            if u.path == "/api/delsong_batch":
                # 批量移入回收站：逐首搬，但只失效一次缓存、只重扫一次曲库
                touch_user()
                names = payload.get("names") or []
                if not isinstance(names, list):
                    names = []
                names = [str(x) for x in names if str(x or "").strip()][:300]
                if not names:
                    return self._json({"ok": False, "msg": "没有选中歌曲"}, 400)
                done, failed = [], []
                for nm in names:
                    try:
                        res = trash_song(name=nm)
                    except Exception as e:
                        res = {"ok": False, "msg": "删除异常：%r" % (e,)}
                    if res.get("ok"):
                        done.append({"name": nm, "rel": res.get("rel") or "",
                                     "size": res.get("size") or 0,
                                     "playlists": res.get("playlists") or []})
                    else:
                        failed.append({"name": nm, "msg": res.get("msg") or "失败"})
                if done:
                    drop_local_files()
                    for nm in names:
                        if _stop_if_current(nm):
                            break
                    threading.Thread(target=refresh_backend, daemon=True).start()
                log("delsong_batch: 选中 %d 首，成功 %d，失败 %d"
                    % (len(names), len(done), len(failed)))
                return self._json({"ok": bool(done), "count": len(done),
                                   "deleted": done, "failed": failed},
                                  200 if done else 400)

            if u.path == "/api/trash_purge_batch":
                # 批量彻底删除（回收站多选）
                tids = payload.get("tids") or []
                if not isinstance(tids, list):
                    tids = []
                tids = [str(x) for x in tids if str(x or "").strip()][:300]
                if not tids:
                    return self._json({"ok": False, "msg": "没有选中条目"}, 400)
                done, failed = [], []
                for tid in tids:
                    try:
                        r = purge_song(tid)
                    except Exception as e:
                        r = {"ok": False, "msg": "彻底删除异常：%r" % (e,)}
                    if r.get("ok"):
                        done.append(tid)
                    else:
                        failed.append({"tid": tid, "msg": r.get("msg") or "失败"})
                log("trash_purge_batch: 选中 %d，成功 %d，失败 %d"
                    % (len(tids), len(done), len(failed)))
                return self._json({"ok": bool(done), "count": len(done),
                                   "deleted": done, "failed": failed},
                                  200 if done else 400)

            if u.path == "/api/restore":
                touch_user()
                res = restore_song(payload.get("tid"))
                if res.get("ok"):
                    drop_local_files()
                    threading.Thread(target=refresh_backend, daemon=True).start()
                return self._json(res, 200 if res.get("ok") else 400)

            if u.path == "/api/purge":
                res = purge_song(payload.get("tid"))
                return self._json(res, 200 if res.get("ok") else 400)

            if u.path == "/api/trash_clear":
                return self._json(clear_trash())

            if u.path == "/api/box_sync":
                # 扫码完成后调用：重启 xiaomusic 让新 token 生效，并等设备列表回来
                threading.Thread(target=_restart_backend, daemon=True).start()
                devs, waited = [], 0
                while waited < 48:
                    time.sleep(3)
                    waited += 3
                    try:
                        devs = xm_devices()
                    except Exception:
                        devs = []
                    if devs:
                        break
                log("box_sync: waited=%ss devices=%d" % (waited, len(devs)))
                return self._json({"ok": bool(devs), "count": len(devs),
                                   "devices": xm_device_rows(devs), "waited": waited})

            # ---------------- JY-SWAP 换源工作台
            if u.path.startswith("/api/swap/"):
                if _swapmod is None:
                    return self._json({"ok": False, "msg": "换源模块没加载"}, 501)
                if u.path == "/api/swap/apply":
                    touch_user()
                    want = payload.get("paths") or []
                    force = bool(payload.get("force"))
                    srows = _swapmod.scan()
                    if want:
                        srows = [r for r in srows if r.get("path") in want]
                    else:
                        srows = [r for r in srows if r.get("status") in ("ok", "durwarn")]
                    if not srows:
                        return self._json({"ok": False, "msg": "收件箱里没有能替换的文件"})
                    res = _swapmod.apply_many(srows, force=force)
                    if res.get("done"):
                        drop_local_files()          # 曲库变了，列表缓存作废
                        _file_index["ts"] = 0.0
                        _meta_miss.clear()
                        threading.Thread(target=refresh_backend, daemon=True).start()
                    return self._json(res)

                if u.path == "/api/swap/undo":
                    res = _swapmod.undo(payload.get("stamp") or None)
                    if res.get("ok"):
                        drop_local_files()
                        _file_index["ts"] = 0.0
                        _meta_miss.clear()
                        threading.Thread(target=refresh_backend, daemon=True).start()
                    return self._json(res)

                if u.path == "/api/swap/autosave":
                    return self._json({"ok": True, "auto": _swapmod.set_auto(payload.get("auto"))})

                if u.path == "/api/swap/librefresh":
                    t0 = time.time()
                    lib = _swapmod.load_lib(force=True)
                    return self._json({"ok": True, "total": len(lib),
                                       "bad": len([e for e in lib if e.get("sev", 3) <= 0]),
                                       "cost": round(time.time() - t0, 1)})

                return self._json({"ok": False, "msg": "not found"}, 404)

            return self._json({"ok": False, "msg": "not found"}, 404)
        except Exception as e:
            log("POST %s error %r" % (u.path, e))
            return self._json({"ok": False, "msg": str(e)}, 500)


PAGE = "<!doctype html><meta charset=utf-8><title>MusicMate</title><h3>MusicMate OK</h3>"


# ---------------------------------------------------------------- JY-SWAP 换源守护
def _now_playing_name():
    """音箱当前在放哪首（拿不到就空）"""
    try:
        did = _default_did()
        with urllib.request.urlopen(
                BACKEND_API + "/playingmusic?did=" + urllib.parse.quote(did), timeout=4) as r:
            pm = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        return str(pm.get("cur_music") or "")
    except Exception:
        return ""


def swap_watcher():
    """盯着收件箱：名字、时长、音质三项都对得上就自动替换（老文件先备份，可撤销）"""
    if _swapmod is None:
        return
    time.sleep(20)
    while True:
        try:
            if _swapmod.auto_on() and _swapmod.inbox_files():
                srows = _swapmod.scan()
                picks = _swapmod.auto_pick(srows)
                if picks:
                    cur = os.path.splitext(os.path.basename(_now_playing_name()))[0]
                    if cur:      # 正在放的那首先不动，等停了再说
                        picks = [p for p in picks
                                 if os.path.splitext(os.path.basename(
                                     (p.get("target") or {}).get("rel") or ""))[0] != cur]
                if picks:
                    res = _swapmod.apply_many(picks)
                    if res.get("done"):
                        log("换源自动替换 %d 首：%s" % (
                            len(res["done"]), ",".join(d.get("dst", "") for d in res["done"])))
                        drop_local_files()
                        _file_index["ts"] = 0.0
                        _meta_miss.clear()
                        refresh_backend()
        except Exception as e:
            log("swap watcher fail %r" % (e,))
        time.sleep(_swapmod.AUTO_INTERVAL)


def main():
    load_index()
    load_data()
    load_meta()
    jy_src_load()          # JY-SRC 音源开关（文件不存在默认全开）
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log("MusicMate listening on 0.0.0.0:%d  cache=%s" % (PORT, CACHE_DIR))
    # JY-TAG: 起服务后慢慢补内嵌标签与高清封面（限速跑，不抢播放的 IO）
    threading.Thread(target=meta_worker, kwargs={"allow_net": True}, daemon=True).start()
    threading.Thread(target=backfill_beside_lrc, daemon=True).start()
    threading.Thread(target=_vol_worker, daemon=True).start()      # JY-VOL 音量合并下发
    try: _load_daily_disk()                                          # JY-RECS 启动即载入当天推荐，首屏秒回
    except Exception: pass
    threading.Thread(target=_rec_warmer, daemon=True).start()       # JY-RECS 每日推荐后台预热+落盘
    threading.Thread(target=plcover_warmer, daemon=True).start()    # JY-PERF 后台补没有图的歌单配图
    threading.Thread(target=swap_watcher, daemon=True).start()     # JY-SWAP 收件箱守护
    threading.Thread(target=tvf_worker, daemon=True).start()       # JY-TVF 电视歌词屏跟轴
    threading.Thread(target=_tvf_boot, daemon=True).start()        # 还原上次残留的电视静音
    threading.Thread(target=axis_worker, daemon=True).start()      # JY-AXIS 组播轴自动重锚
    threading.Thread(target=_wavcache_boot, daemon=True).start()   # JY-WAVCACHE 开机补扫一次
    threading.Thread(target=_plcache_boot, daemon=True).start()    # JY-PLCACHE 开机补扫一次
    srv.serve_forever()


if __name__ == "__main__":
    main()
