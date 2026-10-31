#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
歌词修复：对「时间轴与音频长度明显不符」的歌词重新取词。

判定：词尾 / 音频时长 落在 [0.85, 1.12] 外 → 需要修
修法：源B重取（按歌名+歌手打分）→ 校验新词覆盖率 → 备份旧词 → 双写（旁置 + 缓存）

用法：
  python3 lrcfix.py --dry            只出计划
  python3 lrcfix.py --go --limit=15  实修前 15 个
"""
import os, re, sys, json, time, shutil

sys.path.insert(0, "/root/musictool")
import server
import swap

MUSIC = "/sdcard/Music"
LRC_DIR = server.LRC_DIR
AUDIO = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".ape", ".aac", ".wma", ".opus",
         ".dsf", ".dff", ".aiff", ".wv", ".mp2"}
TS = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
AUDIO_ARG = [a for a in sys.argv[1:] if a.startswith("--")]
DRY = "--dry" in AUDIO_ARG or "--go" not in AUDIO_ARG
LIMIT = int([a.split("=")[1] for a in AUDIO_ARG if a.startswith("--limit=")][0]) if any(
    a.startswith("--limit=") for a in AUDIO_ARG) else 999
OFFSET = int([a.split("=")[1] for a in AUDIO_ARG if a.startswith("--offset=")][0]) if any(
    a.startswith("--offset=") for a in AUDIO_ARG) else 0


def txt_of(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", "ignore")


def parse(lrc):
    times, bodies = [], []
    for ln in lrc.splitlines():
        s = ln.strip()
        tt = TS.findall(s)
        if not tt:
            continue
        b = TS.sub("", s).strip()
        if not b:
            continue
        for h, mi, fr in tt:
            times.append(int(h) * 60 + int(mi) + (int((fr + "00")[:3]) / 1000.0 if fr else 0))
            bodies.append(b)
    if not times:
        return None
    return {"n": len(times), "t0": min(times), "t1": max(times), "bodies": bodies}


def dur_of(p):
    try:
        return float(swap.probe(p).get("dur") or 0)
    except Exception:
        return 0.0


def walk():
    for root, dirs, files in os.walk(MUSIC):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO:
                yield os.path.join(root, f)


def save(name, lrc, stem):
    """双写：旁置 + xiaomusic 缓存；返回写入的路径"""
    ap = None
    for ext in AUDIO:
        c = os.path.join(MUSIC, name + ext)
        if os.path.exists(c):
            ap = c
            break
    written = []
    if ap:
        lp = os.path.splitext(ap)[0] + ".lrc"
        if os.path.exists(lp):
            os.makedirs(BAK, exist_ok=True)
            shutil.copy2(lp, os.path.join(BAK, os.path.basename(lp)))
        with open(lp, "w", encoding="utf-8") as f:
            f.write(lrc)
        written.append(lp)
    cp = os.path.join(LRC_DIR, server.sanitize_filename(stem) + ".lrc")
    try:
        os.makedirs(LRC_DIR, exist_ok=True)
        if os.path.exists(cp):
            os.makedirs(BAK, exist_ok=True)
            shutil.copy2(cp, os.path.join(BAK, "cache_" + os.path.basename(cp)))
        with open(cp, "w", encoding="utf-8") as f:
            f.write(lrc)
        written.append(cp)
    except Exception as e:
        print("  缓存写入失败:", e)
    return written


def main():
    rows = []
    for p in sorted(walk()):
        stem = os.path.splitext(os.path.basename(p))[0]
        lp = os.path.splitext(p)[0] + ".lrc"
        if not os.path.exists(lp):
            continue
        d = dur_of(p)
        if d < 45:
            continue
        r = parse(txt_of(lp))
        if not r or r["n"] < 5:
            continue
        cov = r["t1"] / d
        if 0.55 <= cov <= 1.12:
            continue
        rows.append({"stem": stem, "path": lp, "n": r["n"], "t1": round(r["t1"], 1),
                     "dur": round(d, 1), "cov": round(cov, 2),
                     "head": (r["bodies"][0] if r["bodies"] else "")[:26]})

    print("需要修：%d 首（%s）" % (len(rows), "计划" if DRY else "执行"))
    done = fix = keep = 0
    out = []
    for i, r in enumerate(rows[OFFSET:OFFSET + LIMIT], 1):
        done += 1
        new = server.nc_lyric(r["stem"])
        nr = parse(new) if new else None
        act, ncov = "无候选", 0
        if nr and nr["n"] >= 5:
            ncov = nr["t1"] / r["dur"]
            if 0.85 <= ncov <= 1.12:
                act = "换新"
            else:
                act = "候选对不上"
        print("%2d. %-38s 原%5.2f(词尾%6.1f/%6.1f) → %s %5.2f"
              % (i, r["stem"][:38], r["cov"], r["t1"], r["dur"], act, ncov))
        if act == "换新" and not DRY:
            save(os.path.basename(r["path"])[:-4], new, r["stem"])
            fix += 1
        elif act == "换新":
            fix += 1
        else:
            keep += 1
        out.append(dict(r, act=act, ncov=round(ncov, 2)))
        time.sleep(0.2)
    print("\n汇总：可换 %d / 保持 %d" % (fix, keep))
    json.dump(out, open("/root/musictool/_lrcfix_last.json", "w"), ensure_ascii=False)


BAK = "/sdcard/JYSwap/lrcbak/" + time.strftime("%Y%m%d-%H%M%S")
main()
