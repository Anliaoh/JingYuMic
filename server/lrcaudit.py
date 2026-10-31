#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""歌词体检 v2：分层 + 内容错配检测"""
import os, re, sys, json, unicodedata

sys.path.insert(0, "/root/musictool")
MUSIC = "/sdcard/Music"
AUDIO = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".ape", ".aac", ".wma", ".opus",
         ".dsf", ".dff", ".aiff", ".wv", ".mp2"}
TS = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
CJK = re.compile(r"[\u4e00-\u9fff]")


def walk():
    for root, dirs, files in os.walk(MUSIC):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO:
                yield os.path.join(root, f)


def decode(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", "ignore")


def parse(path):
    txt = decode(path)
    times, body_lines = [], []
    for ln in txt.splitlines():
        s = ln.strip()
        tt = TS.findall(s)
        if not tt:
            continue
        body = TS.sub("", s).strip()
        if not body:
            continue
        for h, mi, fr in tt:
            times.append(int(h) * 60 + int(mi) + (int((fr + "00")[:3]) / 1000.0 if fr else 0))
            body_lines.append(body)
    if not times:
        return None
    return {"n": len(times), "t0": min(times), "t1": max(times),
            "text": "".join(body_lines), "head": body_lines[0] if body_lines else "",
            "lines": len(txt.splitlines())}


def dur_of(p):
    try:
        import swap
        return float(swap.probe(p).get("dur") or 0)
    except Exception:
        return 0.0


def norm(s):
    s = re.sub(r"\.(mp3|flac|ogg|m4a|wav|ape|aac|wma|opus)$", "", s, flags=re.I)
    s = re.sub(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^\]]*\]", "", s)
    s = re.sub(r"^\s*\d{1,3}[.\-_、\s]+", "", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", s)
    return s.lower()


cats = {"trunc": [], "short": [], "long": [], "wrong": [], "ok": []}
nolrc, thin = [], []
for p in sorted(walk()):
    stem = os.path.splitext(os.path.basename(p))[0]
    lp = os.path.splitext(p)[0] + ".lrc"
    if not os.path.exists(lp):
        nolrc.append(os.path.relpath(p, MUSIC))
        continue
    r = parse(lp)
    d = dur_of(p)
    if not r or d <= 0:
        continue
    rel = os.path.relpath(p, MUSIC)
    if r["n"] < 5 or r["t1"] < 15:
        thin.append({"f": rel, "n": r["n"], "t1": round(r["t1"], 1), "dur": round(d, 1)})
        continue
    cov = r["t1"] / d
    rec = {"f": rel, "n": r["n"], "t0": round(r["t0"], 1), "t1": round(r["t1"], 1),
           "dur": round(d, 1), "cov": round(cov, 2), "head": r["head"][:30]}
    # 内容错配：文件名是中文歌，歌词却几乎没中文（或反过来）
    ncjk = len(CJK.findall(r["text"]))
    ratio = ncjk / max(1, len(r["text"]))
    if "中" in "".join("中" if CJK.match(c) else "" for c in stem) and ratio < 0.12 and len(r["text"]) > 100:
        rec["cjk"] = round(ratio, 2)
        cats["wrong"].append(rec)
        continue
    if cov < 0.55:
        cats["trunc"].append(rec)
    elif cov < 0.85:
        cats["short"].append(rec)
    elif cov > 1.06:
        cats["long"].append(rec)
    else:
        cats["ok"].append(rec)

print("== 分层 ==")
print("正常(覆盖 85%%~106%%): %d" % len(cats["ok"]))
print("严重残缺(<55%%)      : %d" % len(cats["trunc"]))
print("偏短(55%%~85%%)       : %d" % len(cats["short"]))
print("超长(>106%%)         : %d" % len(cats["long"]))
print("疑似内容错配          : %d" % len(cats["wrong"]))
print("无效/只有几行         : %d" % len(thin))
print("完全没歌词            : %d" % len(nolrc))

for k, label in (("wrong", "内容错配"), ("trunc", "严重残缺"), ("long", "超长")):
    print("\n== %s (%d) ==" % (label, len(cats[k])))
    for r in sorted(cats[k], key=lambda x: x["cov"])[:14]:
        print("  %-40s 词%4d行 覆盖%5.2f 词尾%6.1f/音频%6.1f | %s"
              % (r["f"][:40], r["n"], r["cov"], r["t1"], r["dur"], r["head"][:22]))
json.dump(cats, open("/root/musictool/_lrc2.json", "w"), ensure_ascii=False)
json.dump(nolrc, open("/root/musictool/_lrc_none.json", "w"), ensure_ascii=False)
