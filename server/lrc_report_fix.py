#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
歌词报错处理：把 App / PC 两端标记的错误行按类型修掉。

报告来源：两端歌词行「长按 / 右键 → 标记歌词错误」，落到
/root/xiaomusic_conf/lrc_report.json，每条带 {name,t,text,kind,delta,tail,fix,note}。

能自动改的：
  time + delta          该行（或 tail：从该行起往后整首）时间戳平移 delta 秒
  text + fix            按时间/原文定位到那一行，把词换成 fix
自动不了（pair / missing / other，或没给 delta、没给 fix）的：
  只列清单，state 保持 new，等人处理；加 --refetch 会尝试重取歌词整体换新
  （沿用 lrcfix.py 的覆盖率校验，新旧差异过大就不换）。

用法（在平板上跑）：
  python3 /root/musictool/lrc_report_fix.py                只出计划，不动文件
  python3 /root/musictool/lrc_report_fix.py --go           实修
  python3 /root/musictool/lrc_report_fix.py --go --refetch 顺带重取「改不了」的那些
  python3 /root/musictool/lrc_report_fix.py --json         机器可读
"""
import os, re, sys, json, time, shutil

sys.path.insert(0, "/root/musictool")
import server

LRC_DIR = server.LRC_DIR
REPORT = server.LRCREPORT_FILE
TS = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
ARGS = [a for a in sys.argv[1:]]
DRY = "--go" not in ARGS
REFETCH = "--refetch" in ARGS
AS_JSON = "--json" in ARGS
LIMIT = int([a.split("=")[1] for a in ARGS if a.startswith("--limit=")][0]) \
    if any(a.startswith("--limit=") for a in ARGS) else 999
BAK = "/sdcard/JYSwap/lrcbak/" + time.strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- LRC 读写
def txt_of(p):
    raw = open(p, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", "ignore")


def parse_rows(lrc):
    """拆成 [(times|None, precs|None, body, rawline)]。
    precs 记每个时间戳原本的小数位数，改完按原精度写回，
    免得动一句就把整首的 [00:06.940] 都变成 [00:06.94]。"""
    out = []
    for ln in lrc.split("\n"):
        s = ln.rstrip("\r")
        tt = TS.findall(s)
        if not tt:
            out.append((None, None, "", s))
            continue
        body = TS.sub("", s).strip()
        times = [int(h) * 60 + int(mi) + (int((fr + "00")[:3]) / 1000.0 if fr else 0)
                 for h, mi, fr in tt]
        precs = [len(fr) for _, _, fr in tt]
        out.append((times, precs, body, s))
    return out


def fmt_ts(t, prec=2):
    t = max(0.0, t)
    m = int(t // 60)
    # 宽度 = 秒的两位整数 + 小数点 + prec 位小数
    return ("[%02d:%0" + str(3 + prec) + "." + str(prec) + "f]") % (m, t - m * 60)


def render(rows):
    out = []
    for times, precs, body, raw in rows:
        if times is None:
            out.append(raw)
        elif body:
            out.append("".join(fmt_ts(t, precs[i] if precs else 2)
                               for i, t in enumerate(times)) + body)
        else:
            out.append(raw)
    return "\n".join(out)


def norm(s):
    return re.sub(r"\s+", "", str(s or ""))


def find_row(rows, t, text):
    """先按时间找（±0.75s），找不到再按原文找。返回 (下标, 依据)"""
    best, bd = -1, 1e9
    for i, row in enumerate(rows):
        times = row[0]
        if not times:
            continue
        d = min(abs(x - t) for x in times)
        if d < bd:
            bd, best = d, i
    if best >= 0 and bd <= 0.75:
        return best, "t"
    nt = norm(text)
    if nt:
        for i, row in enumerate(rows):
            if row[0] and norm(row[2]) == nt:
                return i, "text"
    return -1, ""


# ---------------------------------------------------------------- 定位歌词文件
def lrc_src(name):
    """返回现有歌词文件的路径；没有就返回 ''。"""
    cands = []
    stem = os.path.splitext(name)[0]
    for n in (name, stem):
        cands.append(os.path.join(LRC_DIR, server.sanitize_filename(n) + ".lrc"))
    try:
        ap = server.audio_path_of(name)
        if ap:
            cands.append(os.path.splitext(ap)[0] + ".lrc")
    except Exception:
        pass
    for p in cands:
        if p and os.path.exists(p) and os.path.getsize(p) > 20:
            return p
    return ""


def backup(p):
    try:
        os.makedirs(BAK, exist_ok=True)
        shutil.copy2(p, os.path.join(BAK, os.path.basename(p)))
    except Exception as e:
        print("   备份失败:", e)


def save(name, txt):
    """双写：xiaomusic 缓存（前端读的那份）+ 音频旁置（换播放器也在）。"""
    stem = os.path.splitext(name)[0]
    ok, msg = server.lyric_save(name, txt)
    if not ok:
        return [], msg
    written = ["cache:" + server.sanitize_filename(name) + ".lrc"]
    # 老脚本（lrcfix）存的是不带扩展名那份，顺手同步，别留下两份不一致的
    cp = os.path.join(LRC_DIR, server.sanitize_filename(stem) + ".lrc")
    if os.path.exists(cp) and cp != os.path.join(
            LRC_DIR, server.sanitize_filename(name) + ".lrc"):
        try:
            with open(cp + ".tmp", "w", encoding="utf-8") as f:
                f.write(txt)
            os.replace(cp + ".tmp", cp)
            written.append("cache:" + server.sanitize_filename(stem) + ".lrc")
        except Exception as e:
            print("   同步 stem 版失败:", e)
    return written, ""


# ---------------------------------------------------------------- 修法
def apply_shift(rows, i0, delta, tail):
    n = 0
    for i in range(len(rows)):
        times, precs, body, raw = rows[i]
        if not times:
            continue
        if tail or i == i0:
            rows[i] = ([max(0.0, t + delta) for t in times], precs, body, raw)
            n += 1
    return n


def apply_text(rows, i0, fix):
    times, precs, body, raw = rows[i0]
    rows[i0] = (times, precs, fix, raw)
    return 1


def try_refetch(name, dur=0.0):
    """整体重取歌词，覆盖率合理才认为可用。返回 (lrc, 说明)"""
    try:
        new = server.nc_lyric(name)
    except Exception as e:
        return "", "重取失败:%s" % e
    if not new:
        return "", "没有候选"
    r = parse_rows(new)
    ts = [t for row in r for t in (row[0] or [])]
    if len(ts) < 5:
        return "", "候选行数不足"
    if dur > 30:
        cov = max(ts) / dur
        if not (0.85 <= cov <= 1.12):
            return "", "候选对不上(%.2f)" % cov
    return new, "可换"


def main():
    try:
        data = json.load(open(REPORT, encoding="utf-8"))
    except Exception:
        print("没有报告文件：", REPORT)
        return
    items = [x for x in data.get("items", [])
             if str(x.get("state") or "new") != "done"]
    if not items:
        print("没有待处理的歌词报错。")
        return
    items.sort(key=lambda x: (-int(x.get("count") or 1), str(x.get("name"))))

    print("歌词报错 %d 条（%s）" % (len(items), "计划" if DRY else "执行"))
    print("-" * 78)

    out, fixed, manual, failed = [], [], [], []
    shifted = set()        # (name, delta, tail) 已平移过就别再平一次
    backed = set()         # 同一次运行里每个文件只备份一份原版
    n = 0
    for it in items:
        if n >= LIMIT:
            break
        n += 1
        name = it.get("name") or ""
        kind = it.get("kind") or "other"
        rec = {"id": it.get("id"), "name": name, "kind": kind,
               "t": it.get("t"), "text": it.get("text"),
               "note": it.get("note"), "count": it.get("count") or 1, "act": ""}

        if kind in ("time", "text") and (
                (kind == "time" and it.get("delta")) or (kind == "text" and it.get("fix"))):
            src = lrc_src(name)
            if not src:
                rec["act"] = "找不到歌词文件"
                failed.append(rec)
                print("  %-4s %-30s %-7s 找不到歌词文件" %
                      (it.get("id"), name[:30], kind))
                continue
            rows = parse_rows(txt_of(src))
            i0, by = find_row(rows, float(it.get("t") or 0), it.get("text") or "")
            if i0 < 0:
                rec["act"] = "定位不到那一行"
                failed.append(rec)
                print("  %-4s %-30s %-7s 定位不到那一行（原句「%s」）" %
                      (it.get("id"), name[:30], kind, (it.get("text") or "")[:18]))
                continue
            if kind == "time":
                # 同一首歌、同一方向、同一范围的平移算同一件事：用户在两句上各标一次
                # 「整首慢 1.5 秒」不该被叠加成 3 秒
                key = (name, round(float(it.get("delta") or 0), 2), bool(it.get("tail")))
                if key in shifted:
                    print("  %-4s %-30s %-7s 同上，已平移过，跳过" %
                          (it.get("id"), name[:30], kind))
                    rec["act"] = "已含在同一次平移里"
                    if not DRY:
                        server.lrc_report_done([it.get("id")])
                    out.append(rec)
                    continue
                shifted.add(key)
                cnt = apply_shift(rows, i0, float(it.get("delta") or 0), bool(it.get("tail")))
                desc = "%+.1fs%s 平移 %d 行" % (
                    float(it.get("delta") or 0), "（整首）" if it.get("tail") else "", cnt)
            else:
                apply_text(rows, i0, it.get("fix"))
                desc = "改成「%s」" % (it.get("fix") or "")[:20]
            new = render(rows)
            print("  %-4s %-30s %-7s %s  [%s匹配]" % (it.get("id"), name[:30], kind, desc, by))
            if not DRY:
                if src not in backed:
                    backup(src)
                    backed.add(src)
                w, err = save(name, new)
                if err:
                    rec["act"] = "写入失败:" + err
                    failed.append(rec)
                    print("     写入失败:", err)
                    continue
                server.lrc_report_done([it.get("id")])
                rec["act"] = "已改"
                rec["files"] = w
            else:
                rec["act"] = "将改"
            fixed.append(rec)
            out.append(rec)
            continue

        # ---- 改不了的：只列出来；--refetch 时尝试整体重取
        why = {"time": "没给偏移量", "text": "没给正确的词"}.get(kind, "需要人工判断")
        if REFETCH:
            src = lrc_src(name)
            dur = 0.0
            try:
                ap = server.audio_path_of(name)
                if ap and os.path.exists(ap):
                    dur = float(server._probe_dur(ap) or 0)
            except Exception:
                pass
            new, msg = try_refetch(name, dur)
            if new:
                print("  %-4s %-30s %-7s 重取换新（%s）" % (it.get("id"), name[:30], kind, msg))
            if not DRY:
                if src and src not in backed:
                    backup(src)
                    backed.add(src)
                    w, err = save(name, new)
                    if err:
                        rec["act"] = "重取写入失败:" + err
                        failed.append(rec)
                        out.append(rec)
                        continue
                    server.lrc_report_done([it.get("id")])
                    rec["act"] = "重取换新"
                    rec["files"] = w
                else:
                    rec["act"] = "将重取换新"
                out.append(rec)
                continue
            why = msg
        rec["act"] = "待人工：" + why
        manual.append(rec)
        out.append(rec)
        print("  %-4s %-30s %-7s 待人工（%s）「%s」%s" %
              (it.get("id"), name[:30], kind, why, (it.get("text") or "")[:18],
               (" 备注:" + it["note"]) if it.get("note") else ""))

    print("-" * 78)
    print("自动改 %d · 待人工 %d · 没法改 %d%s" %
          (len(fixed), len(manual), len(failed),
           "（本次未动文件，加 --go 才落盘）" if DRY else ""))
    if not DRY:
        print("旧歌词备份：", BAK)
    if manual:
        print("\n需要人工看一眼（报告里仍是 new，下次还会列出来）：")
        for r in manual:
            print("  %-4s %-28s %-7s %s" % (r["id"], r["name"][:28], r["kind"],
                                            (r.get("note") or (r.get("text") or ""))[:40]))

    try:
        json.dump(out, open("/root/musictool/_lrcrep_last.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass
    if AS_JSON:
        print("\nJSON:" + json.dumps({"fixed": fixed, "manual": manual,
                                      "failed": failed}, ensure_ascii=False, indent=1))


main()
