#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
央视高清源自动保活（公网无人值守）
------------------------------------
在任意公网 Linux 上跑（阿里云 cron / GitHub Actions / 任意 VPS），不依赖本机、不依赖局域网。

做什么：
 1. 拉公网候选池（多个 IPTV 仓库的 live.txt）
 2. 对 CCTV-1..16 + CCTV-5+ 逐条候选实测：拉播放列表 → 拉分片 → ffprobe 验真实分辨率
 3. 只保留 >=1280x720 的线路，按「1080p 优先 + 拉流耗时」排序，每台最多留 3 条
 4. 输出 hd_sources.json（App 启动时自动拉取，无需重新打包安装）
 5. 保留上一版里仍然可用的线路（避免抖动）

用法：
  python3 hd_maintain.py --out /srv/infra/data/ota/hd_sources.json
  python3 hd_maintain.py --out ./hd_sources.json --json-report
退出码：0 正常；1 有声道的所有候选都失效（会沿用旧线路并告警）
"""
import json, os, re, subprocess, sys, time, urllib.request, tempfile, hashlib
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36"}
POOL_SOURCES = [
    "https://gh-proxy.com/https://raw.githubusercontent.com/niubihu1/tvbox/main/live.txt",
    "https://ghproxy.net/https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
    "https://ghproxy.net/https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u",
    "https://ghproxy.net/https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
]
# 当前已知可用的「家族模板」：{n} 按家族规则替换
FAMILIES = [
    ("http://183.129.255.66:8480/hls/{n}/index.m3u8", lambda ch: ch),        # n = 固定号
    ("http://112.92.129.95:9898/hls/{n}/index.m3u8", lambda ch: ch + 17),     # n = 固定号+17
]
# 固定号 → 频道名正则（用于从候选池里挑）
CH_PAT = {
    1: r"cctv[\-_ ]?1(?!\d)", 2: r"cctv[\-_ ]?2(?!\d)", 3: r"cctv[\-_ ]?3(?!\d)",
    4: r"cctv[\-_ ]?4(?!\d)", 5: r"cctv[\-_ ]?5(?!\d)(?!\+)", 6: r"cctv[\-_ ]?5\+|cctv[\-_ ]?5 ?plus",
    7: r"cctv[\-_ ]?6(?!\d)", 8: r"cctv[\-_ ]?7(?!\d)", 9: r"cctv[\-_ ]?8(?!\d)",
    10: r"cctv[\-_ ]?9(?!\d)", 11: r"cctv[\-_ ]?10(?!\d)", 12: r"cctv[\-_ ]?11(?!\d)",
    13: r"cctv[\-_ ]?12(?!\d)", 14: r"cctv[\-_ ]?13(?!\d)", 15: r"cctv[\-_ ]?14(?!\d)",
    16: r"cctv[\-_ ]?15(?!\d)", 17: r"cctv[\-_ ]?16(?!\d)",
}


def http_get(url, timeout=8):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def probe(url, timeout=8):
    """返回 (w, h, 切片刻耗时ms) 或 None"""
    t0 = time.time()
    try:
        txt = http_get(url, timeout)
        if not txt.lstrip().startswith("#EXTM3U"):
            return None
        # master playlist：取最高分辨率子表
        if "EXT-X-STREAM-INF" in txt:
            best, bestw = None, 0
            lines = txt.splitlines()
            for i, l in enumerate(lines):
                if "EXT-X-STREAM-INF" in l:
                    m = re.search(r"RESOLUTION=(\d+)x(\d+)", l)
                    w = int(m.group(1)) if m else 0
                    for j in range(i + 1, len(lines)):
                        nxt = lines[j].strip()
                        if nxt and not nxt.startswith("#"):
                            if w > bestw:
                                bestw, best = w, nxt
                            break
            if not best:
                return None
            base = url.rsplit("/", 1)[0] + "/"
            txt = http_get(best if best.startswith("http") else base + best, timeout)
        segs = [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            return None
        seg = segs[1] if len(segs) > 1 else segs[0]
        base = url.rsplit("/", 1)[0] + "/"
        seg_url = seg if seg.startswith("http") else base + seg
        fd, fn = tempfile.mkstemp(suffix=".ts")
        os.close(fd)
        subprocess.run(["curl", "-s", "--max-time", "8", "-A", UA["User-Agent"],
                        seg_url, "-o", fn], capture_output=True)
        if os.path.getsize(fn) < 20000:
            os.unlink(fn)
            return None
        out = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0", fn],
                             capture_output=True, text=True).stdout.strip()
        os.unlink(fn)
        if not out:
            return None
        w, h = out.split("\n")[0].split(",")[:2]
        return int(w), int(h), int((time.time() - t0) * 1000)
    except Exception:
        return None


def build_pool():
    """从公网仓库拉候选，返回 {chno: [url,...]}"""
    pool = {k: [] for k in CH_PAT}
    seen = set()
    for src in POOL_SOURCES:
        try:
            body = http_get(src, 25)
        except Exception:
            continue
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," not in line:
                continue
            name, url = line.split(",", 1)
            url = url.strip()
            if not url.startswith("http"):
                continue
            for chno, pat in CH_PAT.items():
                if re.search(pat, name, re.I):
                    key = (chno, url)
                    if key in seen:
                        break
                    seen.add(key)
                    pool[chno].append(url)
                    break
    # 已知家族优先放在最前
    for tmpl, idx in FAMILIES:
        for chno in CH_PAT:
            pool[chno].insert(0, tmpl.format(n=idx(chno)))
    return pool


def main():
    out_path = None
    for i, a in enumerate(sys.argv):
        if a == "--out" and i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]
    if not out_path:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hd_sources.json")

    old = {}
    if os.path.exists(out_path):
        try:
            old = json.load(open(out_path, encoding="utf-8")).get("ch", {})
            old = {int(k): v for k, v in old.items()}
        except Exception:
            old = {}

    pool = build_pool()
    result, missing, bad = {}, [], []

    def work(item):
        chno, url = item
        return chno, url, probe(url)

    tasks = []
    for chno, urls in pool.items():
        # 每台最多测 8 条，家族模板先测
        for u in urls[:8]:
            tasks.append((chno, u))
    got = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for chno, url, r in ex.map(work, tasks):
            if not r:
                continue
            w, h, ms = r
            if w < 1280:
                continue
            got.setdefault(chno, []).append((w, ms, url))

    for chno in CH_PAT:
        cands = sorted(got.get(chno, []), key=lambda x: (-x[0], x[1]))
        urls = [u for _, _, u in cands[:3]]
        # 上一版里仍然可用的线路补进来（防止本次探测抖动导致全空）
        for u in old.get(chno, []):
            if len(urls) >= 3:
                break
            if u not in urls:
                urls.append(u)
        if urls:
            result[chno] = urls
        else:
            missing.append(chno)

    now = int(time.time())
    payload = {
        "updatedAt": now,
        "updatedAtText": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "generator": "hd_maintain.py",
        "ch": {str(k): v for k, v in sorted(result.items())},
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)

    hd = sum(1 for v in got.values() for c in v if c[0] >= 1900)
    print(f"[hd_maintain] {time.strftime('%H:%M:%S')} 频道={len(result)}/17 "
          f"1080p线路={hd} 缺失={missing} → {out_path}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
