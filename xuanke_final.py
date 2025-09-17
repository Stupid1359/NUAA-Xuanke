#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NUAA 选课脚本（最终版 v2）
- 默认使用浏览器里复制的整行 Cookie（不依赖 Selenium）。
- 自动适配 profileId / electionProfile.id 两种参数名。
- 预热 defaultPage，仿真 XHR 头。
- 提交阶段优先使用与拉取课表时相同的参数名；若被重定向到统一认证，则自动换备选 URL。
- 支持输入“序号”或“课程ID”两种方式选择课程。
- 新增：对返回体进行“智能解码”（自动尝试 gzip/deflate/多编码），避免出现“乱码”日志。
"""

import datetime
import re
import time
import sys
import requests
import zlib
import gzip
import random

# ===== 学校固定地址 =====
BASE = "https://aao-eas.nuaa.edu.cn"
HOME_URL = f"{BASE}/eams/homeExt.action"
DEFAULT_TPL = f"{BASE}/eams/stdElectCourse!defaultPage.action?electionProfile.id={{pid}}"

# ===== 行为参数 =====
REQUEST_TIMEOUT = 5          # 每次请求超时（秒）
POST_INTERVAL = 0.7          # 提交间隔（秒），过快会触发“请不要过快点击”
BACKOFF_SECONDS = 3          # 命中限速提示后的退避（秒）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

def is_login_bounce(resp) -> bool:
    """是否被重定向/返回到统一认证页面"""
    try:
        url_l = resp.url.lower()
    except Exception:
        url_l = ""
    text = ""
    try:
        text = resp.text
    except Exception:
        pass
    return ("统一身份认证" in text) or ("authserver" in url_l)

def smart_read(resp):
    """尽最大可能把响应体解码成可读文本；返回 (text, used_encoding)"""
    raw = resp.content or b""
    # 先尝试按 headers 的 Content-Encoding 自动解压；requests 通常会处理，这里再兜底
    def _maybe_decompress(b: bytes) -> bytes:
        if len(b) >= 2 and b[0] == 0x1F and b[1] == 0x8B:  # gzip magic
            try:
                return gzip.decompress(b)
            except Exception:
                return b
        if len(b) >= 2 and b[0] == 0x78 and b[1] in (0x01, 0x5E, 0x9C, 0xDA):  # zlib
            try:
                return zlib.decompress(b)
            except Exception:
                return b
        return b

    raw = _maybe_decompress(raw)

    # 优先用服务器声明的编码与 requests 的检测
    for enc in [getattr(resp, "encoding", None), getattr(resp, "apparent_encoding", None),
                "utf-8", "gb18030", "gbk", "gb2312", "latin1"]:
        if not enc:
            continue
        try:
            return raw.decode(enc, errors="ignore"), enc
        except Exception:
            continue
    # 兜底
    return raw.decode("utf-8", errors="ignore"), "utf-8"

def get_profile_id() -> str:
    # pid = input("请输入选课档案ID（示例：4665）：").strip()
    pid = "4665"
    if not pid.isdigit():
        print("profileId 应为数字。")
        sys.exit(1)
    return pid

def get_cookie_manual() -> str:
    print("\n在浏览器开发者工具 Network 里，选中发往 aao-eas 的请求（推荐 data.action 或 batchOperator.action），"
          "在 Request Headers 复制整行 Cookie，原样粘贴到这里：")
    return input("粘贴 Cookie：").strip()

def make_session(cookie_str: str, pid: str) -> requests.Session:
    s = requests.Session()
    # 直接把整行 Cookie 放进请求头，避免 CookieJar 域名/路径不匹配的坑
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Cookie": cookie_str.strip(),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": BASE,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })

    # 0) 先打到首页
    s.get(HOME_URL, timeout=REQUEST_TIMEOUT, allow_redirects=True)

    # 1) 预热 defaultPage（关键）
    referer = DEFAULT_TPL.format(pid=pid)
    s.get(referer, timeout=REQUEST_TIMEOUT, allow_redirects=True)

    # 2) 标准 XHR 头
    s.headers.update({
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    })

    # 3) 快速登录态探测
    rchk = s.get(f"{BASE}/eams/home.action", timeout=REQUEST_TIMEOUT, allow_redirects=True)
    if is_login_bounce(rchk):
        raise RuntimeError("Cookie 未生效或已过期：请在 Network→stdElectCourse 的那条请求复制整行 Cookie 再试。")
    return s

# ============ 拉取课程列表辅助 ============

_ID_PATTERNS = [
    re.compile(r"(?:\bprofileId\b|\belectionProfile\.id\b)\s*[:=]\s*['\"]?(\d+)"),
    re.compile(r"(?:\?|&)(?:profileId|electionProfile\.id)=(\d+)"),
]

def _extract_profile_ids(html: str):
    hits = []
    for pat in _ID_PATTERNS:
        hits += pat.findall(html)
    # 去重保序
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out

def _try_fetch_data(session: requests.Session, pid: str):
    """尝试两种参数名去拉 data.action；返回 (status, text, used_param)"""
    url1 = f"{BASE}/eams/stdElectCourse!data.action?electionProfile.id={pid}"
    r1 = session.get(url1, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    t1, _ = smart_read(r1)
    if r1.status_code == 200 and "id:" in t1 and "<html" not in t1.lower():
        return r1.status_code, t1, "electionProfile.id"

    url2 = f"{BASE}/eams/stdElectCourse!data.action?profileId={pid}"
    r2 = session.get(url2, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    t2, _ = smart_read(r2)
    return r2.status_code, t2, "profileId"

def course_info(session: requests.Session, pid: str):
    print("正在获取抢课信息，请稍候……")

    # 1) 直接用用户输入的 pid 尝试
    status, text, used = _try_fetch_data(session, pid)
    if status != 200 or "id:" not in text or "<html" in text.lower():
        # 2) 从 defaultPage 反向解析候选 pid
        warm = session.get(DEFAULT_TPL.format(pid=pid), timeout=REQUEST_TIMEOUT, allow_redirects=True)
        candidates = _extract_profile_ids(warm.text)
        if not candidates:
            dp = session.get(f"{BASE}/eams/stdElectCourse!defaultPage.action",
                             timeout=REQUEST_TIMEOUT, allow_redirects=True)
            candidates = _extract_profile_ids(dp.text)
        for cand in ([pid] + candidates):
            status, text, used = _try_fetch_data(session, cand)
            if status == 200 and "id:" in text and "<html" not in text.lower():
                pid = cand
                break

    if status != 200 or "id:" not in text or "<html" in text.lower():
        print("课程列表请求返回异常状态码：", status)
        print(text[:300])
        sys.exit(1)

    # 解析课程
    find_id = re.compile(r"id:(\d+),")
    find_name = re.compile(r"name:'([^']*)',")
    id_list = find_id.findall(text)
    name_list = []
    for item in text.split("code:"):
        m = find_name.findall(item)
        if m:
            name_list.append(m[0])
        else:
            break

    if not id_list:
        print("未解析到任何课程 ID，返回片段：", text[:300])
        sys.exit(1)

    n = min(len(id_list), len(name_list))
    print("\n命中的选课档案ID:", pid, f"(参数名 {used})")
    print("可选课程：")
    for i in range(n):
        print(f"序号: {i:<3}  课程ID: {id_list[i]:<10}  课程名称: {name_list[i]}")

    # 支持“序号”或“课程ID”混填
    print("\n请输入想要抢的‘序号’或‘课程ID’（可多个，空格分隔）：")
    # tokens = input().strip().split()
    tokens = "400785 398319 398891 398568".split()

    chosen = []
    id_set = set(id_list)
    for t in tokens:
        if not t.isdigit():
            print(f"非法输入：{t}")
            continue
        idx = int(t)
        if 0 <= idx < n:
            chosen.append(id_list[idx])
            continue
        if t in id_set:
            chosen.append(t)
            continue
        print(f"未找到该序号/课程ID：{t}")

    if not chosen:
        print("未选择任何课程，退出。")
        sys.exit(0)
    return chosen, pid, used

# ============ 提交选课 ============

def grab_courses(session: requests.Session, lesson_ids, pid: str, used_param: str):
    # open_at = input("请输入抢课开启时间（格式：YYYY-MM-DD HH:MM:SS）：").strip()
    open_at = "2025-9-16 16:00:00"

    try:
        dt = datetime.datetime.strptime(open_at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        print("时间格式不正确。")
        sys.exit(1)

    base_post = f"{BASE}/eams/stdElectCourse!batchOperator.action"
    if used_param == "profileId":
        post_urls = [f"{base_post}?profileId={pid}", f"{base_post}?electionProfile.id={pid}"]
    else:
        post_urls = [f"{base_post}?electionProfile.id={pid}", f"{base_post}?profileId={pid}"]

    forms = [{"optype": "true", "operator0": f"{cid}:true:0", "lesson0": cid} for cid in lesson_ids]

    # ——新增：两次提交的最小全局间隔，避免同一时间内多次提交——
    POST_MIN_GAP = 0.8  # 可按需调到 1.6～1.8 更稳
    last_post_ts = 0.0  # monotonic 时间戳

    print("\n开始等待放闸时间……")
    while True:
        now = datetime.datetime.now()
        if now >= dt:
            for data in forms:
                sent_ok = False
                for url in post_urls:
                    # ——限速关键处：每次真正提交前，确保与上次提交至少间隔 POST_MIN_GAP——
                    gap = time.monotonic() - last_post_ts
                    if gap < POST_MIN_GAP:
                        # 加一点抖动，避免卡在整秒
                        # time.sleep((POST_MIN_GAP - gap) + random.uniform(0.18, 0.35))
                        time.sleep(POST_MIN_GAP - gap)

                    try:
                        resp = session.post(url, data=data, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                        # 记录“这次提交已经发生”
                        last_post_ts = time.monotonic()

                        # 被踢回统一认证，视为失败，换下一个 URL
                        if is_login_bounce(resp):
                            continue

                        body, enc = smart_read(resp)
                        chinese = re.findall(r"([\u4e00-\u9fa5]+)", body)
                        msg = "".join(chinese) or body[:180]
                        # 用当前时间打印更准确
                        ts = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        print(f"[{ts}] {resp.status_code} -> {msg}")

                        sent_ok = True
                        if ("请不要过快点击" in body) or (resp.status_code in (429, 503)):
                            time.sleep(BACKOFF_SECONDS)
                        break
                    except Exception:
                        # 提交异常也算一次尝试，已限速；继续下一个 URL
                        continue

                if not sent_ok:
                    print("提交未成功：两个提交地址都被重定向或异常")
        else:
            remain = dt - now
            print(f"抢课界面未开启，剩余：{remain}")

        # 维持你原有的外层节奏
        time.sleep(POST_INTERVAL)


def main():
    pid = get_profile_id()              # 例如：4665
    cookie = get_cookie_manual()        # 粘贴整行 Cookie
    session = make_session(cookie, pid)
    ids, pid, used = course_info(session, pid)
    grab_courses(session, ids, pid, used)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户中断，已退出。")
