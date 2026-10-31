#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给没有有效歌词的歌补词：源B优先 → 源站兜底 → 时长校验 → 双写。
用法: python3 lrcfill.py [--limit=40] [--offset=0] [--dry]
"""
import os, re, sys, json, time, shutil

sys.path.insert(0, "/root/musictool")
import server

MUSIC = "/sdcard/Music"
LRC_DIR = server.LRC_DIR
AUDIO = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".ape", ".aac", ".wma", ".opus",
         ".dsf", ".dff", ".aiff", ".wv", ".mp2"}
TS = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
ARGS = sys.argv[1:]
DRY = "--dry" in ARGS
LIMIT = int([a.split("=")[1] for a in ARGS if a.startswith("--limit=")][0]) if any(
    a.startswith("--limit=") for a in ARGS) else 40
OFFSET = int([a.split("=")[1] for a in ARGS if a.startswith("--offset=")][0]) if any(
    a.startswith("--offset=") for a in ARGS) else 0

import swap


def span(lrc):
    ts = []
    for h, mi, fr in TS.findall(lrc or ""):
        ts.append(int(h) * 60 + int(mi) + (int((fr + "00")[:3]) / 1000.0 if fr else 0))
    return (min(ts), max(ts)) if ts else (0, 0)


def walk():
    for root, dirs, files in os.walk(MUSIC):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO:
                yield os.path.join(root, f)


def save(stem, lrc):
    written = []
    for ext in AUDIO:
        ap = os.path.join(MUSIC, stem + ext)
        if os.path.exists(ap):
            lp = os.path.splitext(ap)[0] + ".lrc"
            with open(lp, "w", encoding="utf-8") as f:
                f.write(lrc)
            written.append(lp)
            break
    cp = os.path.join(LRC_DIR, server.sanitize_filename(stem) + ".lrc")
    try:
        os.makedirs(LRC_DIR, exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            f.write(lrc)
        written.append(cp)
    except Exception:
        pass
    return written


todo = []
for p in sorted(walk()):
    stem = os.path.splitext(os.path.basename(p))[0]
    lp = os.path.splitext(p)[0] + ".lrc"
    if os.path.exists(lp):
        s = open(lp, encoding="utf-8", errors="ignore").read()
        if not server.is_junk_lrc(s):
            continue
    todo.append((stem, p))

print("待补词 %d 首，本批 %d（offset %d）" % (len(todo), LIMIT, OFFSET))
hit = miss = bad = 0
for stem, p in todo[OFFSET:OFFSET + LIMIT]:
    d = float(swap.probe(p).get("dur") or 0)
    if d < 40:
        continue
    src = "源B"
    lrc = server.nc_lyric(stem)
    if server.is_junk_lrc(lrc):
        src = "源站"
        try:
            lrc = (server.match_assets(stem) or {}).get("lrc") or ""
        except Exception:
            lrc = ""
    if server.is_junk_lrc(lrc):
        miss += 1
        print("  ×  %-42s 无词" % stem[:42])
        continue
    t0, t1 = span(lrc)
    cov = t1 / d
    if not (0.45 <= cov <= 1.35):
        bad += 1
        print("  !  %-42s 覆盖率 %.2f 不采（%s）" % (stem[:42], cov, src))
        continue
    if not DRY:
        save(stem, lrc)
    hit += 1
    print("  √  %-42s %s 覆盖 %.2f" % (stem[:42], src, cov))
    time.sleep(0.15)

print("\n本批：补上 %d / 无词 %d / 版本不符 %d" % (hit, miss, bad))
