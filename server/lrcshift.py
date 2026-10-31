#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lrcshift.py —— 歌词时间轴整体平移

「开头差一点、后面越来越对不上」或「整体早/晚 N 秒」时，一条命令校准。

用法：
  python3 lrcshift.py "黑猫 - 喜欢" -1.5     # 全部提前 1.5 秒
  python3 lrcshift.py "黑猫 - 喜欢" +0.8     # 全部延后 0.8 秒
  python3 lrcshift.py "黑猫 - 喜欢" -1.5 --dry   # 只看结果不写

同时更新两处（服务端缓存 + 曲库旁置），并备份原文件到 /sdcard/JYSwap/lrcbak。
"""
import os
import re
import sys
import shutil
import time

MUSIC = "/sdcard/Music"
CACHE = "/root/xiaomusic_conf/cache/lrc"
BAK = "/sdcard/JYSwap/lrcbak"

LINE = re.compile(r"^(\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])(.*)$")


def fmt(sec):
    if sec < 0:
        sec = 0.0
    m = int(sec // 60)
    s = sec - m * 60
    return "[%02d:%05.2f]" % (m, s)


def parse(tag):
    m = re.match(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]", tag)
    if not m:
        return None
    mm, ss, cs = m.group(1), m.group(2), m.group(3)
    frac = float("0." + cs) if cs else 0.0
    return int(mm) * 60 + int(ss) + frac


def shift_text(lrc, delta):
    out = []
    for line in lrc.split("\n"):
        m = LINE.match(line.strip())
        if not m:
            out.append(line)
            continue
        tag, rest = m.group(1), m.group(2)
        t = parse(tag)
        if t is None:
            out.append(line)
            continue
        out.append(fmt(t + delta) + rest)
    return "\n".join(out)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    name = sys.argv[1]
    delta = float(sys.argv[2].replace("+", ""))
    dry = "--dry" in sys.argv

    cands = [os.path.join(CACHE, name + ".lrc"), os.path.join(MUSIC, name + ".lrc")]
    hit = [p for p in cands if os.path.isfile(p)]
    if not hit:
        print("找不到歌词：%s" % " / ".join(cands))
        return

    for p in hit:
        lrc = open(p, encoding="utf-8", errors="replace").read()
        new = shift_text(lrc, delta)
        if dry:
            print("=== %s ===" % p)
            for a, b in list(zip(lrc.split("\n"), new.split("\n")))[:3]:
                print("  原 %s\n  新 %s" % (a[:46], b[:46]))
            continue
        os.makedirs(BAK, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        shutil.copy2(p, os.path.join(BAK, "%s.%s.bak" % (name, stamp)))
        open(p, "w", encoding="utf-8").write(new)
        print("已更新 %s（%+.2fs）" % (p, delta))

    if not dry:
        print("备份在 %s" % BAK)
        print("刷新页面或重播即可生效")


if __name__ == "__main__":
    main()
