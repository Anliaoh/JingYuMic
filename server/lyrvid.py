# -*- coding: utf-8 -*-
"""电视歌词画面：生成 MP4（渐变底图 + 唱片机 + 右侧滚动歌词）

思路
----
电视是 DLNA 渲染器，协议里没有「歌词」这个字段，唯一能控制屏幕上画什么的办法
就是给它一段**视频**。所以这里按歌把「唱片机 + 歌词」烧成 1080p MP4，
音频一起封装进去，投屏时用这段 MP4 代替原来的 MP3：
暂停/跳转/音量/进度回报全部照旧，环绕对时也不受影响。

性能（实测 SM8150 / 8 核）
  · 底图 + 歌词烧录用 ffmpeg 单帧输入 + loop 滤镜，1080p 10fps 约 11 倍速
    （60 秒画面 5.5 秒出片，一首 4 分钟的歌约 22 秒）
  · 关键：必须先把 PNG 转成 yuv420p 再 loop，否则每帧拷 RGBA 会慢 4 倍

缓存
----
/root/xiaomusic_conf/lvideo/<md5>.mp4，超过 MAX_KEEP 个按 mtime 淘汰。
没有歌词的歌写一个 .none 标记，24h 内不再重试。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

try:
    from PIL import Image, ImageDraw, ImageFilter
    HAVE_PIL = True
except Exception:                                   # 没装 Pillow 就只降级不报错
    HAVE_PIL = False


W, H = 1920, 1080
FPS = 10
CRF = 23
FONT = "Noto Sans CJK SC"
MAX_KEEP = 32
NONE_TTL = 10800.0
KEY_VER = "lv5"          # 缓存键版本：解析/排版逻辑一改就 +1，老片子自然淘汰
# 声轨做成静音：电视只当歌词屏（用户要的），声音交给音箱。
# 不用「把电视音量压到 0」那种做法——那会改掉电视自己的音量，
# 之后看电视/投别的视频都是哑的，服务被杀还还原不回来。
SILENT = True


def _ffmpeg() -> str:
    for p in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg") or ""


import re

TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
KANA = re.compile(r"[\u3040-\u30ff]")
LATIN = re.compile(r"[A-Za-z]")
ASCII_ONLY = re.compile(r"^[\x00-\x7f]+$")


def _is_cn(s: str) -> bool:
    """中文行：有汉字、没有假名（拉丁只是专有名词，不算外语）"""
    return bool(CJK.search(s)) and not KANA.search(s)


def _lang_of(s: str) -> str:
    if KANA.search(s):
        return "ja"
    if CJK.search(s):
        return "zh"
    if LATIN.search(s):
        return "en"
    return ""


def _clean(s: str) -> str:
    s = re.sub(r"<\d+:\d+\.\d+>", "", s)          # 增强型 LRC 时间标签
    s = s.replace("\\n", " ").strip()
    return s


def parse_lrc(text: str) -> list:
    """返回 [{t, s}] 按时间排序；同一时间戳的多行用 m 标记同组"""
    raw = []
    lines = []
    for line in (text or "").splitlines():
        line = line.rstrip("\r")
        stamps = TS.findall(line)
        body = TS.sub("", line)
        body = _clean(body)
        if not stamps:
            if body and lines:            # 没有时间戳的续行：并到上一行
                lines[-1]["s"] += " " + body
            continue
        for h, m, frac in stamps:
            t = int(h) * 60 + int(m)
            if frac:
                t += int(frac) / (1000.0 if len(frac) == 3 else 100.0)
            raw.append({"t": round(t, 2), "s": body, "m": body})
    raw.sort(key=lambda x: x["t"])

    # 合并同时间戳的原始行 + 译文行
    # 源A的 LRC 把「译文(N)」放在「原文(N+1)」的同一时间戳、且排在其前面：
    #   [t1]原文1
    #   [t2]译文1
    #   [t2]原文2
    # 所以要把 2 行组里的译文回填给上一块。
    groups = []
    i = 0
    while i < len(raw):
        cur = raw[i]
        grp = [cur]
        j = i + 1
        while j < len(raw) and abs(raw[j]["t"] - cur["t"]) < 0.01:
            grp.append(raw[j])
            j += 1
        texts = [g["s"] for g in grp if g["s"]]
        if texts:
            groups.append({"t": cur["t"], "texts": texts})
        i = j

    # 判断整首歌词的「主语种」：单行组基本都是原文，用它投票
    votes = {}
    for g in groups:
        # 词曲/制作信息行不算数：它们总是中文，会把外语歌误判成中文歌
        if len(g["texts"]) == 1 and not _is_credit(g["texts"][0]):
            lg = _lang_of(g["texts"][0])
            if lg:
                votes[lg] = votes.get(lg, 0) + 1
    song_lang = max(votes, key=votes.get) if votes else ""

    out = []
    for g in groups:
        texts = g["texts"]
        t = g["t"]
        if len(texts) == 1:
            out.append({"t": t, "o": texts[0], "tr": ""})
            continue
        a, b = texts[0], texts[1]
        foreign = bool(song_lang) and song_lang != "zh"
        # 源A的排版（译文行排在原文前面，时间戳挂在**下一句**原文上）：
        #   [t1]原文1 / [t2]译文1 / [t2]原文2
        # 只有「外语歌 + 中文在前 + 上一块还缺译文」才按这种回填。
        if foreign and _is_cn(a) and not _is_cn(b) and out and not out[-1]["tr"]:
            out[-1]["tr"] = a
            if g["t"] <= out[-1]["t"]:
                continue
            out.append({"t": t, "o": b, "tr": ""})
            continue
        # 其余情况（源B tlyric 合并出来的）：同一时间戳就是一对，
        # 原文在前、译文在后，译文属于**这一句**。
        # 以前这里一律回填给上一块，于是每句下面显示的都是下一句的译文。
        out.append({"t": t, "o": a, "tr": b})

    # 丢掉连续重复（空行合并后常见）
    ded = []
    for b in out:
        if ded and b["o"] == ded[-1]["o"] and b["tr"] == ded[-1]["tr"]:
            continue
        ded.append(b)

    # 丢掉开头的「歌名 - 歌手」行和词曲/制作信息行
    keep = []
    for i, b in enumerate(ded):
        if i == 0 and b["t"] < 1.6 and (" - " in b["o"] or " - " in (b["tr"] or "")):
            continue
        if _is_credit(b["o"]):
            continue
        keep.append(b)
    ded = keep or ded

    # 太密集的行（<0.55s）并进上一块，避免快速滚动
    merged = []
    for b in ded:
        if merged and b["t"] - merged[-1]["t"] < 0.55 and not b["tr"]:
            merged[-1]["o"] = (merged[-1]["o"] + " " + b["o"]).strip()
            continue
        merged.append(b)
    return merged


def _tc(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def _ez(s: str) -> str:
    """ASS 文本转义"""
    return (s or "").replace("\\", "\\\\").replace("{", "(").replace("}", ")")


CREDIT = re.compile(
    r"^\s*(作词|作曲|编曲|词|曲|制作人|监制|录音|混音|母带|和声|"
    r"Lyrics\s*by|Composed\s*by|Arranged\s*by|Written\s*by|Producer|"
    r"Mixed\s*by|Mastered\s*by|OP|SP|出品|发行)\s*[:：]",
    re.I)


def _is_credit(s: str) -> bool:
    """词曲/制作信息行，不该出现在歌词画面里"""
    s = s.strip()
    if not s:
        return True
    if CREDIT.match(s):
        return True
    # 纯英文的制作名单
    if re.match(r"^(Lyrics|Music|Composed|Arranged|Produced)\b", s, re.I):
        return True
    return False


def _ass_rgb(hexcolor: str) -> str:
    """#RRGGBB -> ASS 用的 BBGGRR"""
    h = (hexcolor or "").lstrip("#").strip()
    if len(h) != 6:
        return "24272A"
    return (h[4:6] + h[2:4] + h[0:2]).upper()


def _width_em(s: str) -> float:
    """粗估文本宽度（em）：汉字/假名 1.0，拉丁 0.52，空格 0.3"""
    w = 0.0
    for ch in s:
        if re.match(r"[\u1100-\u11ff\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef]", ch):
            w += 1.0
        elif ch == " ":
            w += 0.30
        elif ch in "iljt.,'!|":
            w += 0.26
        elif ch.isupper():
            w += 0.62
        else:
            w += 0.52
    return w



def _dim(hexcolor: str, k: float) -> str:
    """按亮度缩放到灰（k<1 变暗，用于非当前行）"""
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    l = 0.299 * r + 0.587 * g + 0.114 * b
    r = int(r * 0.35 + l * 0.65 * k)
    g = int(g * 0.35 + l * 0.65 * k)
    b = int(b * 0.35 + l * 0.65 * k)
    return "#%02X%02X%02X" % (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def build_ass(blocks, out_path, accent="#E8546A", W=1920, H=1080,
              dur=0.0, title="", artist="", font="Noto Sans CJK SC",
              slots=2, shift_ms=260):
    """生成滚动歌词 ASS

    blocks=[{t,o,tr}]；slots=当前块上下各显示几块。
    每块是一条 Dialogue：\\move 做上移、\\t 把颜色渐变成强调色。
    """
    n = len(blocks)
    if n == 0:
        return None

    fs_o = int(H * 0.0425)
    fs_t = int(H * 0.0335)
    # 有译文的一段占两行，行距要大些；纯原文的歌收紧，不然显得空
    has_tr = any(b.get("tr") for b in blocks)
    pitch = int(H * (0.108 if has_tr else 0.086))
    cy = int(H * 0.600)
    cx = int(W * 0.735)
    maxw = W * 0.50
    dark = _ass_rgb("#2A2724")
    accent_hex = _ass_rgb(accent)
    end_t = dur if dur and dur > 0 else (blocks[-1]["t"] + 3600.0)

    def fit(text, fs, maxpx):
        """长句缩字号，但最多缩到 78%，再长就允许略微出界（比忽大忽小好看）"""
        w = _width_em(text) * fs
        if w <= maxpx or w <= 0:
            return fs
        return max(int(fs * 0.78), int(fs * maxpx / w))

    # 每块的字号只算一次，避免滚动时字号跳变
    sizes = []
    for b in blocks:
        sizes.append((fit(b["o"], fs_o, maxw), fit(b.get("tr") or "", fs_t, maxw)))

    def block_text(b, k, a_o, a_t):
        fo, ft = sizes[k]
        o = _ez(b["o"])
        tr = _ez(b.get("tr") or "")
        s = r"{\fs%d\b1\alpha&H%s&}" % (fo, a_o) + o
        if tr:
            s += r"\N{\fs%d\b0\alpha&H%s&}" % (ft, a_t) + tr
        return s

    out = []
    out.append("[Script Info]")
    out.append("ScriptType: v4.00+")
    out.append("PlayResX: %d" % W)
    out.append("PlayResY: %d" % H)
    out.append("WrapStyle: 2")          # 不自动折行（已按宽度缩字号）
    out.append("ScaledBorderAndShadow: yes")
    out.append("")
    out.append("[V4+ Styles]")
    out.append("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
               "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
               "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
               "Alignment, MarginL, MarginR, MarginV, Encoding")
    out.append("Style: L,%s,%d,&H00%s,&H00%s,&H00FFFFFF,&H00000000,0,0,0,0,"
               "100,100,0.3,0,1,0,0,5,0,0,0,1"
               % (font, fs_o, dark, accent_hex))
    out.append("")
    out.append("[Events]")
    out.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")

    # ---- 顶部固定的标题 / 歌手
    if title:
        tt = _ez(title)
        ar = _ez(artist or "")
        txt = r"{\fs%d\b1\c&H%s&\alpha&H18&}%s" % (int(H * 0.048), dark, tt)
        if ar:
            txt += r"\N{\fs%d\b0\alpha&H50&}%s" % (int(H * 0.032), ar)
        out.append(r"Dialogue: 1,%s,%s,L,,0,0,0,,{\an5\pos(%d,%d)\fad(420,300)}%s"
                   % (_tc(0.0), _tc(end_t), cx, int(H * 0.165), txt))

    # ---- 歌词块
    for i in range(n):
        t0 = blocks[i]["t"]
        t1 = blocks[i + 1]["t"] if i + 1 < n else end_t
        if t1 <= t0:
            t1 = t0 + 0.4
        for r in range(-slots, slots + 1):
            j = i + r
            if j < 0 or j >= n:
                continue
            y_now = cy + r * pitch
            y_prev = cy + (r + 1) * pitch
            cur = (r == 0)
            # 越远越淡
            if cur:
                a_o, a_t = "00", "26"
            else:
                d = abs(r)
                a_o = {1: "62", 2: "9E"}.get(d, "C8")
                a_t = {1: "80", 2: "B4"}.get(d, "D8")
            tags = r"{\an5\pos(%d,%d)\move(%d,%d,%d,%d,0,%d)" % (
                cx, y_now, cx, y_prev, cx, y_now, shift_ms)
            if r == slots:
                tags += r"\fad(360,0)"
            elif r == -slots:
                tags += r"\fad(0,360)"
            if cur:
                # 先按暗色显示，滚动结束后渐变到强调色
                tags += r"\c&H%s&\t(%d,%d,\c&H%s&)" % (
                    dark, shift_ms - 30, shift_ms + 420, accent_hex)
            else:
                tags += r"\c&H%s&" % dark
            tags += "}"
            out.append("Dialogue: 0,%s,%s,L,,0,0,0,,%s%s"
                       % (_tc(t0), _tc(t1), tags, block_text(blocks[j], j, a_o, a_t)))

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    return out_path


def accent_from_cover(path, fallback="#E8546A"):
    """从封面取主色（加权平均高饱和像素），保证亮度适中可读"""
    try:
        from PIL import Image
    except Exception:
        return fallback
    try:
        im = Image.open(path).convert("RGB")
        im.thumbnail((48, 48))
        tot = [0.0, 0.0, 0.0]
        wsum = 0.0
        for r, g, b in im.getdata():
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / float(mx or 1)
            v = mx / 255.0
            w = (0.15 + sat) * (0.35 + v)
            tot[0] += r * w
            tot[1] += g * w
            tot[2] += b * w
            wsum += w
        if wsum <= 0:
            return fallback
        r, g, b = [int(t / wsum) for t in tot]
        # 封面五花八门：灰封面取出来的颜色太淡，高亮行会看不出来。
        # 所以只保留它的「色相」，饱和度提上去、亮度压进可读区间，
        # 保证任何封面都能得到一个像 QQ 音乐那种能当强调色的颜色。
        import colorsys
        hh, ll, ss = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        if ss < 0.08:                      # 几乎是灰度封面：用默认粉
            return fallback
        ss = min(1.0, ss * 1.60 + 0.30)
        ll = min(0.52, max(0.40, ll))
        r, g, b = [int(round(v * 255)) for v in colorsys.hls_to_rgb(hh, ll, ss)]
        return "#%02X%02X%02X" % (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
    except Exception:
        return fallback

def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))

def diag_grad(W, H, stops):
    """沿左上→右下做多色标渐变"""
    small = Image.new("RGB", (256, 256))
    px = small.load()
    for y in range(256):
        for x in range(256):
            t = (x * 0.45 + y * 0.55) / 255.0
            # 找区间
            for i in range(len(stops) - 1):
                t0, c0 = stops[i]
                t1, c1 = stops[i + 1]
                if t <= t1 or i == len(stops) - 2:
                    k = 0 if t1 <= t0 else max(0.0, min(1.0, (t - t0) / (t1 - t0)))
                    px[x, y] = lerp(c0, c1, k)
                    break
    return small.resize((W, H), Image.BICUBIC)

def radial(W, H, cx, cy, r, color, alpha=170):
    """柔和径向色斑（用于角落的粉/绿光晕）"""
    layer = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(layer)
    steps = 26
    for i in range(steps, 0, -1):
        k = i / float(steps)
        rr = r * k
        a = int(alpha * (1 - k) ** 1.6)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=a)
    layer = layer.filter(ImageFilter.GaussianBlur(r * 0.12))
    over = Image.new("RGB", (W, H), color)
    return over, layer

def circle_crop(img, size):
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size * 4 - 1, size * 4 - 1], fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out

def build_bg(out_path, cover_path="", W=1920, H=1080):
    # 1) 底色渐变：左上米绿 → 中间奶油 → 左下粉
    bg = diag_grad(W, H, [
        (0.00, (238, 245, 226)),
        (0.30, (250, 246, 230)),
        (0.62, (252, 236, 232)),
        (1.00, (247, 201, 208)),
    ])
    # 2) 左下粉光 + 右上绿光
    ov, al = radial(W, H, int(W * 0.18), int(H * 1.02), int(W * 0.55), (247, 176, 190), 150)
    bg = Image.composite(ov, bg, al)
    ov, al = radial(W, H, int(W * 0.96), int(H * 0.02), int(W * 0.42), (214, 236, 196), 110)
    bg = Image.composite(ov, bg, al)

    # 3) 唱片机底座（白色圆角方 + 柔和投影）
    bw = int(H * 0.56)
    bx = int(W * 0.115)
    by = int((H - bw) / 2) + int(H * 0.015)
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [bx + 6, by + 26, bx + bw + 6, by + bw + 26], radius=int(bw * 0.11), fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(38))
    dark = Image.new("RGB", (W, H), (198, 168, 176))
    bg = Image.composite(dark, bg, shadow)

    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([bx, by, bx + bw, by + bw], radius=int(bw * 0.075),
                        fill=(255, 255, 255, 246))
    d.rounded_rectangle([bx + 2, by + 2, bx + bw - 2, by + bw - 2], radius=int(bw * 0.07),
                        outline=(255, 255, 255, 255), width=2)
    bg = Image.alpha_composite(bg.convert("RGBA"), base)

    # 4) 黑胶唱片
    cx = bx + int(bw * 0.455)
    cy = by + int(bw * 0.545)
    R = int(bw * 0.368)

    # 4a) 盘面：径向微亮（中心炭灰 → 边缘近黑）
    disc = Image.new("RGBA", (2 * R + 4, 2 * R + 4), (0, 0, 0, 0))
    inner = Image.new("RGB", (2 * R, 2 * R), (26, 24, 25))
    grad = Image.new("L", (2 * R, 2 * R), 0)
    gd = ImageDraw.Draw(grad)
    for i in range(R, 0, -1):
        gd.ellipse([R - i, R - i, R + i, R + i], fill=int(70 * (1 - i / float(R)) ** 2))
    sheen = Image.new("RGB", (2 * R, 2 * R), (120, 116, 118))
    inner = Image.composite(sheen, inner, grad)

    # 4b) 细密纹路（同心圆，极低对比，避免摩尔纹）
    grooves = Image.new("RGBA", (2 * R, 2 * R), (0, 0, 0, 0))
    gdd = ImageDraw.Draw(grooves)
    n = 46
    for i in range(n):
        rr = R * (0.46 + 0.54 * i / float(n))
        a = 16 if i % 3 else 26
        gdd.ellipse([R - rr, R - rr, R + rr, R + rr], outline=(255, 255, 255, a), width=1)
    inner = Image.alpha_composite(inner.convert("RGBA"), grooves)

    # 4c) 斜向高光（左上柔光带，模拟灯光反射）
    hi = Image.new("L", (2 * R, 2 * R), 0)
    ImageDraw.Draw(hi).polygon(
        [(R * 0.30, 0), (R * 0.72, 0), (R * 1.05, 2 * R), (R * 0.62, 2 * R)], fill=60)
    hi = hi.filter(ImageFilter.GaussianBlur(R * 0.16))
    inner = Image.composite(Image.new("RGB", (2 * R, 2 * R), (232, 226, 228)),
                            inner.convert("RGB"), hi).convert("RGBA")

    # 4d) 裁成圆形贴上去
    m = Image.new("L", (2 * R * 2, 2 * R * 2), 0)
    ImageDraw.Draw(m).ellipse([0, 0, 2 * R * 2 - 1, 2 * R * 2 - 1], fill=255)
    m = m.resize((2 * R, 2 * R), Image.LANCZOS)
    disc.paste(inner, (2, 2), m)
    tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tmp.paste(disc, (cx - R, cy - R), disc)
    bg = Image.alpha_composite(bg, tmp)

    # 5) 封面圆片
    cr = int(R * 0.60)
    if cover_path and os.path.exists(cover_path):
        cv = Image.open(cover_path)
        tile = circle_crop(cv, cr * 2)
    else:
        tile = Image.new("RGBA", (cr * 2, cr * 2), (0, 0, 0, 0))
        ImageDraw.Draw(tile).ellipse([0, 0, cr * 2 - 1, cr * 2 - 1], fill=(120, 120, 128, 255))
    bg.paste(tile, (cx - cr, cy - cr), tile)
    ring = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([cx - cr - 3, cy - cr - 3, cx + cr + 3, cy + cr + 3],
                                 outline=(255, 255, 255, 210), width=4)
    bg = Image.alpha_composite(bg, ring)

    # 6) 唱臂（右侧立柱 + 横臂 + 唱头）
    arm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arm)
    px_ = bx + int(bw * 0.80)
    py_ = by + int(bw * 0.20)
    ad.rounded_rectangle([px_ - int(bw * 0.030), py_ - int(bw * 0.030),
                          px_ + int(bw * 0.030), py_ + int(bw * 0.030)],
                         radius=int(bw * 0.02), fill=(238, 238, 240, 255))
    ad.line([px_, py_, px_ - int(bw * 0.10), py_ + int(bw * 0.34)],
            fill=(240, 240, 242, 255), width=max(4, int(bw * 0.016)))
    hx = px_ - int(bw * 0.10)
    hy = py_ + int(bw * 0.34)
    ad.line([hx, hy, hx + int(bw * 0.02), hy + int(bw * 0.075)],
            fill=(232, 232, 236, 255), width=max(5, int(bw * 0.022)))
    bg = Image.alpha_composite(bg, arm)

    bg.convert("RGB").save(out_path, quality=95)
    return out_path


# ---------------------------------------------------------------- 任务队列
class LyricStudio:
    """按歌生成歌词视频，带后台队列

    回调（由 server.py 注入，避免循环 import）：
      lyric_fn(name) -> (lrc_text, artist)
      cover_fn(name) -> bytes | None
      audio_fn(name) -> 本地音频绝对路径 | None
      dur_fn(name)   -> 秒（0 表示未知）
    """

    def __init__(self, cache_dir, lyric_fn, cover_fn, audio_fn, dur_fn=None,
                 log=print):
        self.dir = cache_dir
        self.lyric_fn = lyric_fn
        self.cover_fn = cover_fn
        self.audio_fn = audio_fn
        self.dur_fn = dur_fn or (lambda n: 0)
        self.log = log
        self._q = []                 # [(prio, seq, name)]
        self._qset = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._busy = None            # 正在生成的歌名
        self._fails = {}
        self._thread = None
        self._stop = False
        # 歌词指纹缓存：缓存键里要带上歌词内容，否则补了译文/换了源之后
        # 旧的 MP4 永远命中，电视上就一直放没有译文的那一版（真踩过）
        self._stamp = {}
        self._stamp_ttl = 20.0
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception as e:
            self.log("lvideo mkdir fail %r" % (e,))

    # ---------------- 路径
    def _lyr_stamp(self, name: str) -> str:
        """歌词指纹。歌词改了（补译文、换源、手工修正）指纹就变，视频跟着重出。

        读取频率很高（前端 700ms 一拉 /api/lvideo），所以按 20 秒缓存一次；
        serve_lyric 本身有磁盘缓存，命中时只是一次文件读。
        """
        now = time.time()
        c = self._stamp.get(name)
        if c and now - c[0] < self._stamp_ttl:
            return c[1]
        h = "0"
        try:
            txt, _artist = self.lyric_fn(name)
            h = hashlib.md5((txt or "").encode("utf-8")).hexdigest()[:8]
        except Exception:
            h = "0"
        self._stamp[name] = (now, h)
        return h

    def key(self, name: str) -> str:
        # KEY_VER：渲染/解析逻辑改了就 +1，逼着老片子重出（否则旧 MP4 一直命中）
        return hashlib.md5((KEY_VER + "|%s|%s" % (name or "", self._lyr_stamp(name)))
                           .encode("utf-8")).hexdigest()[:16]

    def video_path(self, name: str) -> str:
        return os.path.join(self.dir, self.key(name) + ".mp4")

    def _none_path(self, name: str) -> str:
        return os.path.join(self.dir, self.key(name) + ".none")

    def _meta_path(self, name: str) -> str:
        return os.path.join(self.dir, self.key(name) + ".json")

    def ready(self, name: str) -> bool:
        p = self.video_path(name)
        return bool(p) and os.path.exists(p) and os.path.getsize(p) > 4096

    def known_none(self, name: str) -> bool:
        p = self._none_path(name)
        try:
            return os.path.exists(p) and (time.time() - os.path.getmtime(p)) < NONE_TTL
        except Exception:
            return False

    def meta(self, name: str) -> dict:
        try:
            with open(self._meta_path(name), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def path_by_key(self, key: str) -> str:
        if not re.match(r"^[0-9a-f]{6,32}$", key or ""):
            return ""
        p = os.path.join(self.dir, key + ".mp4")
        return p if os.path.exists(p) else ""

    # ---------------- 队列
    def request(self, name: str, prio: int = 2):
        """排一个生成任务。prio 0=当前 1=下一首 2=预热"""
        if not name or self.ready(name) or self.known_none(name):
            return False
        with self._lock:
            if name in self._qset:
                # 已有任务，优先级更高就提前
                old = self._qset[name]
                if prio < old[0]:
                    self._qset[name] = (prio, old[1], name)
                    self._q = [x for x in self._q if x[2] != name]
                    self._q.append((prio, old[1], name))
                    self._q.sort()
                return False
            self._seq += 1
            item = (prio, self._seq, name)
            self._q.append(item)
            self._qset[name] = item
            self._q.sort()
        self._ensure_thread()
        return True

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="lyrvid", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop:
            with self._lock:
                item = self._q.pop(0) if self._q else None
                if item:
                    self._qset.pop(item[2], None)
                    self._busy = item[2]
            if not item:
                time.sleep(1.2)
                continue
            name = item[2]
            try:
                self.build(name)
            except Exception as e:
                self.log("lvideo build fail %r: %r" % (name, e))
            finally:
                with self._lock:
                    self._busy = None

    def status(self) -> dict:
        with self._lock:
            return {"busy": self._busy,
                    "queue": [x[2] for x in self._q][:12],
                    "qlen": len(self._q),
                    "count": self._count()}

    def _count(self):
        try:
            return len([f for f in os.listdir(self.dir) if f.endswith(".mp4")])
        except Exception:
            return 0

    # ---------------- 生成
    def build(self, name: str, force: bool = False) -> str:
        if not force and self.ready(name):
            return self.video_path(name)
        if not HAVE_PIL or not _ffmpeg():
            raise RuntimeError("缺 Pillow 或 ffmpeg")

        audio = self.audio_fn(name)
        if not audio or not os.path.exists(audio):
            raise RuntimeError("找不到音频文件")
        lrc, artist = self.lyric_fn(name)
        blocks = parse_lrc(lrc or "")
        if len(blocks) < 4:
            self._mark_none(name)
            raise RuntimeError("歌词太少（%d 行）" % len(blocks))

        tmp = tempfile.mkdtemp(prefix="lyv_")
        try:
            cover_p = os.path.join(tmp, "cover.img")
            blob = self.cover_fn(name)
            if blob:
                with open(cover_p, "wb") as f:
                    f.write(blob)
            accent = accent_from_cover(cover_p) if blob else "#E8546A"
            bg_p = os.path.join(tmp, "bg.png")
            build_bg(bg_p, cover_p if blob else "", W, H)

            title, art = split_name(name)
            art = art or artist or ""
            dur = 0.0
            try:
                dur = float(self.dur_fn(name) or 0)
            except Exception:
                dur = 0.0
            ass_p = os.path.join(tmp, "lyr.ass")
            build_ass(blocks, ass_p, accent=accent, W=W, H=H, dur=dur,
                      title=title, artist=art, font=FONT)

            out = self.video_path(name)
            part = out + ".part.mp4"   # ffmpeg 靠后缀判断封装格式
            # 背景图是 loop=-1 的无限流，靠 -shortest 收尾在「音频过滤镜」时并不可靠
            # （实测输出无限增长，两分钟写不完）。所以显式给时长：歌曲时长 + 尾巴。
            total = float(dur) if dur and dur > 1 else 0.0
            if not total:
                total = (blocks[-1]["t"] + 12.0) if blocks else 60.0
            total += 3.0
            cmd = [
                _ffmpeg(), "-y", "-loglevel", "error",
                "-i", bg_p, "-i", audio,
                "-filter_complex",
                "[0:v]format=yuv420p,loop=loop=-1:size=1:start=0,"
                "setpts=N/(%d*TB),ass=%s,fps=%d,format=yuv420p[v]"
                "%s" % (FPS, _esc(ass_p), FPS,
                        ";[1:a]volume=0[a]" if SILENT else ""),
                "-map", "[v]", "-map", "[a]" if SILENT else "1:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(CRF),
                # 关键帧必须密：10fps 下默认 GOP 是 250 帧＝25 秒才一个 I 帧，
                # 电视 DLNA seek 时只能往**前**跳到下一个关键帧（seek 40 落到 50），
                # 结果每次纠偏都跳过头，越纠越乱。每帧一个 I 帧后，seek 精确到秒。
                "-g", str(FPS), "-keyint_min", str(FPS), "-sc_threshold", "0",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ac", "2",
                "-movflags", "+faststart",
                "-t", "%.2f" % total, part,
            ]
            t0 = time.time()
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode != 0:
                try:
                    os.remove(part)
                except Exception:
                    pass
                raise RuntimeError("ffmpeg: %s" % (r.stderr or b"")[-300:].decode("utf-8", "ignore"))
            os.replace(part, out)
            with open(self._meta_path(name), "w", encoding="utf-8") as f:
                json.dump({"name": name, "title": title, "artist": art,
                           "accent": accent, "dur": dur, "bytes": os.path.getsize(out),
                           "cost": round(time.time() - t0, 1),
                           "ts": int(time.time())}, f, ensure_ascii=False)
            self.log("lvideo ok %r in %.1fs (%.1fMB)"
                     % (name, time.time() - t0, os.path.getsize(out) / 1048576.0))
            self._trim()
            return out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _mark_none(self, name: str):
        try:
            with open(self._none_path(name), "w") as f:
                f.write(str(int(time.time())))
        except Exception:
            pass

    def _trim(self):
        try:
            files = [os.path.join(self.dir, f) for f in os.listdir(self.dir)
                     if f.endswith(".mp4")]
            if len(files) <= MAX_KEEP:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[:len(files) - MAX_KEEP]:
                os.remove(p)
                try:
                    os.remove(p[:-4] + ".json")
                except Exception:
                    pass
        except Exception as e:
            self.log("lvideo trim fail %r" % (e,))


def _esc(p: str) -> str:
    """ffmpeg filter 参数里的路径转义"""
    return p.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def split_name(name: str):
    """「歌手 - 歌名」→ (歌名, 歌手)"""
    raw = (name or "").rsplit("/", 1)[-1]
    raw = re.sub(r"\.(mp3|flac|m4a|wav|ogg|ape|aac|wma|opus)$", "", raw, flags=re.I)
    if " - " in raw:
        a, t = raw.split(" - ", 1)
        if a.strip() and t.strip():
            return t.strip(), a.strip()
    return raw.strip(), ""
