#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lrcflat.py —— 找「时间轴坏了」的歌词（v2）

判定（双语歌词共用时间戳属正常，不能用"重复率"判）：
  1. 压平：不同时间戳 <= 2，或跨度 < 5 秒
  2. 越界：末句时间 > 音频时长 + 5 秒（整首根本出不来）
  3. 开头空：首句时间 > 音频时长 * 0.25 且 > 15 秒（前段一直没词）
  4. 结尾空：末句 < 音频时长 * 0.5（后半段没词）

用法：
  python3 lrcflat.py            # 报告
  python3 lrcflat.py --json     # 另存清单 /root/musictool/lrcflat.json
"""
import os
import re
import sys
import json

MUSIC = "/sdcard/Music"
CACHE = "/root/xiaomusic_conf/cache/lrc"
SKIP = {"#recycle", "$RECYCLE.BIN", "System Volume Information"}
AUDIO = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".ape", ".aac", ".wma")

TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def parse_ts(lrc):
    out = []
    for m, s, _ms in TS.findall(lrc):
        out.append(int(m) * 60 + int(s))
    return out


def dur_of(path):
    try:
        from mutagen import File as MFile
        f = MFile(path)
        if f is not None and f.info is not None:
            return float(f.info.length)
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, timeout=20)
        return float((r.stdout or b"0").decode().strip() or 0)
    except Exception:
        return 0.0


def judge(ts, dur):
    if len(ts) < 3:
        return None
    uniq = len(set(ts))
    span = max(ts) - min(ts)
    first, last = min(ts), max(ts)
    why = []
    if uniq <= 2:
        why.append("全部挤在 %d 个时间戳" % uniq)
    if span < 5:
        why.append("跨度仅 %ds" % span)
    if dur > 0:
        if last > dur + 5:
            why.append("末句 %.0fs 超出音频 %.0fs" % (last, dur))
        if first > max(15, dur * 0.25):
            why.append("首句 %.0fs 才开始（前 %.0f%% 无词）" % (first, first / dur * 100))
        if last < dur * 0.5 and dur > 60:
            why.append("末句只有 %.0fs，音频 %.0fs" % (last, dur))
    if why:
        return {"lines": len(ts), "uniq": uniq, "span": span,
                "first": first, "last": last, "dur": round(dur, 1),
                "why": "；".join(why)}
    return None


def main():
    bad = []
    total = 0
    for root, dirs, files in os.walk(MUSIC):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
        for fn in files:
            if not fn.lower().endswith(AUDIO):
                continue
            total += 1
            name = os.path.splitext(fn)[0]
            lp = os.path.join(root, name + ".lrc")
            if not os.path.isfile(lp):
                lp = os.path.join(CACHE, name + ".lrc")
            if not os.path.isfile(lp):
                continue
            try:
                lrc = open(lp, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            ts = parse_ts(lrc)
            r = judge(ts, dur_of(os.path.join(root, fn)))
            if r:
                r["name"] = name
                bad.append(r)

    bad.sort(key=lambda x: -(x["last"] - x["first"]))
    print("扫了 %d 首，可疑 %d 首" % (total, len(bad)))
    for b in bad:
        print("  %-40s 行%-4d 唯一%-3d 首%4ds 末%4ds 音频%5.0fs  %s" % (
            b["name"][:38], b["lines"], b["uniq"], b["first"], b["last"],
            b["dur"], b["why"]))
    if "--json" in sys.argv:
        p = "/root/musictool/lrcflat.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(bad, f, ensure_ascii=False, indent=1)
        print("清单已写 %s" % p)


if __name__ == "__main__":
    main()
