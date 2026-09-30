# -*- coding: utf-8 -*-
"""TV 直播表「云端构建」—— 名册驱动、不依赖本机（2026-09-30 主人钦定）

为什么有它：主人明确「不要依赖本机和局域网」。原链路 = 本机实测优选后推送，
本机不开机表就不更新。本脚本把「结构」搬到云：
  · 名册 roster.json（332 台：chno / 台名 / 分组）是唯一权威结构 —— 频道号写死不变
  · 源只是填充物：公开 IPTV 源（本就是公网）+ 可选继承线上现表的源
  · 输出 tvg-chno + tvg-logo + group-title，格式与线上表逐字节同构
  · FCVB1（AES-256-GCM，确定性 nonce）加密后经 GitHub Contents API 发布

不测速：云端测的是 GitHub 机房网络，测不出家宽可达性（本机脚本注释原话）。
可用性交给端侧：星幕/夜航有多源静默切换（LiveChannel.backups + 死源黑名单 + 三级回退）。

用法（本机 dry-run）：
  python _tv_cloud_build.py                     # 只出表到 _lg/tv_cloud.m3u
  python _tv_cloud_build.py --inherit-remote    # 先拉线上表解密，继承其源（防缩水）
  python _tv_cloud_build.py --push              # 加密并推送 v1/live/normal.m3u
云端（GitHub Actions）：环境变量 FC_VAULT_KEY_B64 + GH_TOKEN，加 --push
"""
import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
LG = os.path.join(HERE, "_lg")

REPO = "lidawei1985/filmcollector-pages-xingmu"
BRANCH = "main"
REMOTE_PATH = "v1/live/normal.m3u"
LOGO_BASE = "https://cdn.jsdelivr.net/gh/lidawei1985/filmcollector-logos@main/"

MAX_SRC = 6          # 每台最多源数（1 主 + 5 备）
UA = {"User-Agent": "okhttp/3.12"}
MAGIC = b"FCVB1"

# 公开源池：全部公网可达，云端/本机都能拉（本机脚本同款清单）
PUBLIC_SOURCES = [
    "https://cdn.jsdelivr.net/gh/suxuang/myIPTV@main/ipv4.m3u",
    "https://cdn.jsdelivr.net/gh/vbskycn/iptv@master/tv/iptv4.m3u",
    "https://cdn.jsdelivr.net/gh/YanG-1989/m3u@main/Gather.m3u",
    "https://cdn.jsdelivr.net/gh/YanG-1989/m3u@main/Migu.m3u",
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "https://iptv-org.github.io/iptv/languages/zho.m3u",
    "https://cdn.jsdelivr.net/gh/kimwang1978/xmbjm3u@main/xmbj.txt",
    "https://cdn.jsdelivr.net/gh/ssili126/tv@main/iptv.m3u",
    "https://cdn.jsdelivr.net/gh/YueChan/Live@main/IPTV.m3u",
    "https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/m3u/ipv4.m3u",
    "https://cdn.jsdelivr.net/gh/suxuang/myIPTV@main/%E7%A7%BB%E5%8A%A8IPTV.m3u",
    "https://cdn.jsdelivr.net/gh/iptv-org/iptv@master/streams/cn.m3u",
    "https://cdn.jsdelivr.net/gh/iptv-org/iptv@master/streams/zho.m3u",
]

PROVINCES = ["北京", "上海", "天津", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏",
             "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
             "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "宁夏", "新疆", "西藏",
             "内蒙古", "深圳", "东南", "大湾区", "三沙", "延边", "山东教育"]

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
OP = urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies()),
                                 urllib.request.HTTPSHandler(context=CTX))


# ---------------------------------------------------------------- 解析 / 归一
def parse_m3u(text):
    """→ [(name, url, group, logo)]"""
    out, cur = [], None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            cur = s
        elif s.startswith("#"):
            continue
        elif cur is not None:
            m = re.search(r",([^,]*)$", cur)
            g = re.search(r'group-title="([^"]*)"', cur)
            lg = re.search(r'tvg-logo="([^"]*)"', cur)
            out.append(((m.group(1).strip() if m else ""), s,
                        (g.group(1) if g else ""), (lg.group(1) if lg else "")))
            cur = None
    return out


def canon(name):
    """频道名归一 —— 与 _tv_live_build.py 逐字一致（保证与名册 key 匹配）。"""
    n = re.sub(r"[\s]+", "", name or "")
    n = re.sub(r"·备\d+$", "", n)
    n = n.replace("CCTV", "CCTV-").replace("CCTV--", "CCTV-")
    m = re.match(r"^CCTV-?0*(\d+)(\+?)", n, re.I)
    if m:
        return "CCTV-%s%s" % (m.group(1), m.group(2))
    if re.search(r"CCTV-?5\+|CCTV-?5PLUS|CCTV-?5体育赛事", n, re.I):
        return "CCTV-5+"
    m2 = re.match(r"^([\u4e00-\u9fa5]{2,4})(电视台|广播电视台|卫视)?$", n)
    if m2 and m2.group(1) in PROVINCES:
        return m2.group(1) + "卫视"
    return n


# ---------------------------------------------------------------- 加密（FCVB1）
def load_key():
    """密钥优先级：env FC_VAULT_KEY(_B64)（云，base64 编码的 32B 原始密钥）> 本机金库文件。"""
    b64 = (os.environ.get("FC_VAULT_KEY_B64") or os.environ.get("FC_VAULT_KEY") or "").strip()
    if b64:
        try:
            k = base64.b64decode(b64)
        except Exception:
            k = b""
        if len(k) != 32:
            raise SystemExit("!! FC_VAULT_KEY 解出 %d 字节，应为 32（须为 base64 编码的原始密钥）" % len(k))
        return k
    p = os.path.expanduser("~/.workbuddy/SECRETS/adult_vault.key")
    if os.path.exists(p):
        k = open(p, "rb").read()
        if len(k) != 32:
            raise SystemExit("!! 金库文件 %d 字节，应为 32" % len(k))
        return k
    raise SystemExit("!! 找不到密钥：设 FC_VAULT_KEY_B64 或放 ~/.workbuddy/SECRETS/adult_vault.key")


def encrypt_fcvb1(data: bytes, key: bytes) -> bytes:
    """明文 → FCVB1 信封。确定性 nonce（同明文同密文），与发布层 fc_crypto 完全一致。"""
    if bytes(data[:5]) == MAGIC:
        return bytes(data)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = hashlib.sha256(b"FCVB1-DET-NONCE" + key + data).digest()[:12]
    ct = AESGCM(key).encrypt(nonce, data, None)
    return MAGIC + b"\x00" + nonce + ct


def decrypt_fcvb1(data: bytes, key: bytes) -> bytes:
    if bytes(data[:5]) != MAGIC:
        return bytes(data)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = bytes(data[6:18])
    return AESGCM(key).decrypt(nonce, bytes(data[18:]), None)


# ---------------------------------------------------------------- 取源
def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with OP.open(req, timeout=timeout) as r:
        return r.read()


def collect_from_public():
    """拉全部公网源 → canon_name -> [url]（保序去重）。"""
    by_key = {}
    for u in PUBLIC_SOURCES:
        try:
            txt = http_get(u).decode("utf-8", "replace")
        except Exception as e:
            print("  [skip] %s (%s)" % (u.split("/gh/")[-1][:46], type(e).__name__))
            continue
        n = 0
        for (name, url, g, logo) in parse_m3u(txt):
            if not url.startswith("http"):
                continue
            ck = canon(name)
            if not ck:
                continue
            lst = by_key.setdefault(ck, [])
            if url not in lst:
                lst.append(url)
                n += 1
        print("  [ok]   %s -> 新增 %d 源" % (u.split("/gh/")[-1][:46], n))
    return by_key


def collect_from_remote(key):
    """拉线上现表 → 解密 → 按 canon 归并（现有源排前面，保证不缩水）。"""
    url = "https://raw.githubusercontent.com/%s/%s/%s" % (REPO, BRANCH, REMOTE_PATH)
    try:
        raw = http_get(url, timeout=30)
    except Exception as e:
        print("  [warn] 线上表拉取失败：%s" % e)
        return {}
    try:
        txt = decrypt_fcvb1(raw, key).decode("utf-8", "replace")
    except Exception as e:
        print("  [warn] 线上表解密失败：%s" % e)
        return {}
    by_key = {}
    for (name, url2, g, logo) in parse_m3u(txt):
        if not url2.startswith("http"):
            continue
        ck = canon(name)
        lst = by_key.setdefault(ck, [])
        if url2 not in lst:
            lst.append(url2)
    print("  [ok]   线上现表继承 %d 台的源" % len(by_key))
    return by_key


# ---------------------------------------------------------------- EPG id 映射
# 2026-09-30 端侧接通 EPG：表头加 x-tvg-url（fanmingming 公网 XMLTV，7.7MB/覆盖今明），
# 每台加 tvg-id（与 EPG 的 channel id 同一归一化规则，端侧 EpgIndex 按它查节目单）。
# 地址走 jsDelivr CDN：盒子直连 raw.githubusercontent 实测超时（SocketTimeout），
# 而 jsDelivr 在同机取直播表一直成功；EPG 数据是"今天/明天"，分支缓存 12h 不影响时效。
EPG_URL = "https://cdn.jsdelivr.net/gh/fanmingming/live@main/e.xml"


def epg_id(name):
    """与 fanmingming EPG 的 channel id 对齐：'CCTV-1 综合'→'CCTV1'，'CCTV-5+ 体育赛事'→'CCTV5+'，'湖南卫视'→'湖南卫视'。"""
    first = name.strip().split(" ")[0]
    up = first.upper()
    if up.startswith("CCTV") or up.startswith("CGTN") or up.startswith("CETV") or up.startswith("CHC"):
        return first.replace("-", "").replace("－", "").upper()
    return name.replace(" ", "").replace("-", "").replace("－", "")


# ---------------------------------------------------------------- 出表
def build(roster, srcmap):
    lines = ["#EXTM3U x-tvg-url=\"%s\"" % EPG_URL]
    hit, miss = 0, []
    for r in roster:
        chno = int(r["chno"])
        name = r["name"]
        group = r.get("group") or "其他"
        urls = srcmap.get(r["key"]) or []
        urls = urls[:MAX_SRC]
        if not urls:
            miss.append(name)
            continue
        hit += 1
        for i, u in enumerate(urls):
            nm = name if i == 0 else "%s·备%d" % (name, i)
            lines.append('#EXTINF:-1 tvg-chno="%d" tvg-id="%s" tvg-name="%s" tvg-logo="%s%04d.png" '
                         'group-title="%s",%s' % (chno, epg_id(name), name, LOGO_BASE, chno, group, nm))
            lines.append(u)
    return "\n".join(lines) + "\n", hit, miss


def gh_api(path, method="GET", fields=None, token=None):
    url = "https://api.github.com/" + path.lstrip("/")
    body = json.dumps(fields).encode() if fields is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "tv-cloud-build")
    if body:
        req.add_header("Content-Type", "application/json")
    with OP.open(req, timeout=40) as r:
        return json.loads(r.read().decode())


def push(blob: bytes, token: str, message: str):
    cur = gh_api("repos/%s/contents/%s?ref=%s" % (REPO, REMOTE_PATH, BRANCH), token=token)
    sha = cur.get("sha")
    fields = {"message": message, "branch": BRANCH,
              "content": base64.b64encode(blob).decode()}
    if sha:
        fields["sha"] = sha
    res = gh_api("repos/%s/contents/%s" % (REPO, REMOTE_PATH), method="PUT",
                 fields=fields, token=token)
    return res.get("commit", {}).get("sha", "")[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", default=os.path.join(HERE, "roster.json"),
                    help="名册（默认与脚本同目录，云端即 tools/live/roster.json）")
    ap.add_argument("--out", default=os.path.join(HERE, "tv_cloud.m3u"))
    ap.add_argument("--inherit-remote", action="store_true", help="继承线上现表的源（防缩水）")
    ap.add_argument("--push", action="store_true", help="加密并推送")
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN", ""))
    args = ap.parse_args()

    t0 = time.time()
    roster = json.load(open(args.roster, encoding="utf-8"))
    print("[roster] %d 台" % len(roster))

    inherited = {}
    if args.inherit_remote:
        print("[base] 继承线上现表…")
        inherited = collect_from_remote(load_key())

    print("[pull] 公网源池 %d 条…" % len(PUBLIC_SOURCES))
    public = collect_from_public()

    # 合并：现有源优先（实测过），公网源补充
    merged = {}
    for k in set(list(inherited.keys()) + list(public.keys())):
        seen, out = set(), []
        for u in (inherited.get(k) or []) + (public.get(k) or []):
            if u not in seen:
                seen.add(u)
                out.append(u)
        merged[k] = out

    text, hit, miss = build(roster, merged)
    open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    n_extinf = text.count("#EXTINF")
    print("[build] 覆盖 %d/%d 台 ｜ EXTINF %d 条 ｜ 缺源 %d 台 ｜ 用时 %.0fs"
          % (hit, len(roster), n_extinf, len(miss), time.time() - t0))
    if miss:
        print("  缺源示例：" + "、".join(miss[:14]) + ("…" if len(miss) > 14 else ""))
    print("[out] %s" % args.out)

    if args.push:
        if not args.token:
            raise SystemExit("!! --push 需要 GH_TOKEN")
        blob = encrypt_fcvb1(text.encode("utf-8"), load_key())
        sha = push(blob, args.token, "chore(live): 云端按名册重建 TV 直播表（%d 台）" % hit)
        print("[push] OK %s ｜ 密文 %d B" % (sha, len(blob)))


if __name__ == "__main__":
    main()
