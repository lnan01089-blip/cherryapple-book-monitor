#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cherryapple.myallvalue.com 书店上新监控

原理：
  该商城是有赞国际版搭建的 SPA，商品列表通过公开接口返回 JSON：
    POST https://api.allvalue.com/gateway/api/soa/.../searchGoodsWithPage
    (form 字段 json={"kdtId":100208962,"pageNo":1,"pageSize":40})
  本脚本被 Windows 计划任务每分钟调用一次，抓取全部在售商品，
  与本地快照比对；发现新 goodsId（上新/补货）时通过 Server酱(微信)
  和 Windows 桌面通知提醒，附书名、价格、上架时间、库存和购买链接。
  同时每次运行都刷新 books.html 仪表盘。

用法：
  python monitor.py          正常检查一轮
  python monitor.py --test   只发一条测试通知（验证推送配置）
"""

import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.sax.saxutils as saxutils

# pythonw 运行时无控制台，防止 print 报错
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LAST_GOODS_FILE = os.path.join(BASE_DIR, "last_goods.json")
LOG_FILE = os.path.join(BASE_DIR, "monitor.log")
HTML_FILE = os.path.join(BASE_DIR, "books.html")
LOCK_FILE = os.path.join(BASE_DIR, ".lock")

API_URL = ("https://api.allvalue.com/gateway/api/soa/"
           "com.youzan.i18n.goods.customer.web.GoodsCustomerController/searchGoodsWithPage")
SHOP_ID = 100208962  # kdtId
DETAIL_URL = "https://cherryapple.myallvalue.com/m/pages3/goods/detail/index?goodsId={goods_id}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

DEFAULT_CONFIG = {
    "shop_name": "cherryapple 书店",
    "check_page_size": 40,
    "currency_symbol": "¥",
    "ignore_keywords": ["邮费", "运费", "补差价", "定制", "专用链接", "定金", "尾款"],
    "notify": {
        "wechat_serverchan": True,          # 微信推送（Server酱）
        "serverchan_sendkey": "",           # 主 SendKey（https://sct.ftqq.com）
        "serverchan_sendkeys": [],          # 额外的 SendKey 列表，可推给多个微信（第二台设备）
        "desktop_toast": True,              # Windows 桌面弹窗
        "notify_sold_out": True,            # 商品售罄/下架时也提醒
        "offline_warn_after_failures": 10,  # 连续失败多少次后发“监控异常”提醒
    },
}

STATUS_MAP = {"saleable": "在售", "soldout": "已售罄", "up": "在售"}


# ---------------------------------------------------------------- 基础工具

def log(msg):
    """打印并写入日志文件"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                user_cfg = json.load(f)
            # 浅合并两层，用户缺的字段用默认值补齐
            for k, v in user_cfg.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            log(f"读取 config.json 失败，使用默认配置: {e}")
    else:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        log("已生成默认 config.json")
    # 云端模式：SendKey 从环境变量注入（GitHub Secrets），不写进仓库文件。
    # 支持多个密钥 = 同时推送给多个微信（例如第二台设备的另一个微信账号）。
    env_keys = []
    for var in ("BOOK_MONITOR_SENDKEY", "BOOK_MONITOR_SENDKEY_2", "BOOK_MONITOR_SENDKEY_3"):
        k = os.environ.get(var, "").strip()
        if k:
            env_keys.append(k)
    if env_keys:
        cfg["notify"]["serverchan_sendkey"] = env_keys[0]
        cfg["notify"]["serverchan_sendkeys"] = env_keys[1:]
    return cfg


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log("state.json 损坏，重新初始化")
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def acquire_lock():
    """防止两个实例同时运行（计划任务 + 手动）"""
    if os.path.exists(LOCK_FILE):
        try:
            age = time.time() - os.path.getmtime(LOCK_FILE)
            if age < 300:  # 5 分钟内的锁视为有效
                return False
        except OSError:
            pass
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def fmt_price(cfg, mn, mx):
    sym = cfg["currency_symbol"]
    if mn == mx:
        return f"{sym}{mn / 100:.2f}"
    return f"{sym}{mn / 100:.2f} ~ {sym}{mx / 100:.2f}"


def fmt_ts(ms):
    if not ms:
        return "未知"
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知"


# ---------------------------------------------------------------- 网络请求

def http_post_form(url, form_data, timeout=20):
    req = urllib.request.Request(
        url,
        data=form_data.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": UA,
            "Referer": "https://cherryapple.myallvalue.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_goods(page_size):
    """分页抓取全部在售商品，返回 item 列表"""
    collected, page = [], 1
    while True:
        payload = {"kdtId": SHOP_ID, "pageNo": page, "pageSize": page_size}
        body = urllib.parse.urlencode({"json": json.dumps(payload, ensure_ascii=False)})
        data = http_post_form(API_URL, body)
        if data.get("code") != 200:
            raise RuntimeError(f"API 返回错误: code={data.get('code')} message={data.get('message')}")
        items = (data.get("data") or {}).get("items") or []
        collected.extend(items)
        total = (data.get("data") or {}).get("paginator", {}).get("totalCount") or len(collected)
        if not items or len(collected) >= total:
            break
        page += 1
        if page > 50:  # 防御性上限
            break
    return collected


def summarize(it, cfg):
    gid = str(it["goodsId"])
    status = it.get("goodsSoldStatusStr") or it.get("goodsStatusStr") or ""
    media = it.get("media") or []
    cover = ""
    if media:
        cover = media[0].get("coverUrl") or media[0].get("url") or ""
        cover = re.sub(r"\?!\d+x\d+$", "", cover)  # 去掉缩略图尺寸后缀
    return {
        "goods_id": gid,
        "title": (it.get("title") or it.get("handle") or f"商品 {gid}").strip(),
        "status": status,
        "status_str": STATUS_MAP.get(status, status or "未知"),
        "price_min": int(it.get("minPrice") or 0),
        "price_max": int(it.get("maxPrice") or 0),
        "quantity": it.get("quantity"),
        "total_sold": it.get("totalSoldNum") or it.get("soldNum") or 0,
        "up_at_ms": it.get("goodsUpAt"),
        "cover": cover,
        "url": DETAIL_URL.format(goods_id=gid),
    }


# ---------------------------------------------------------------- 通知渠道

def send_toast(cfg, title, body):
    """Windows 桌面通知（通过 PowerShell + WinRT，无需额外模块）"""
    if os.name != "nt":  # 云端 Linux 环境不支持弹窗，静默跳过
        return
    if not cfg["notify"].get("desktop_toast"):
        return
    body = body[:250]
    xml_str = (
        "<toast duration='long'><visual><binding template='ToastGeneric'>"
        f"<text>{saxutils.escape(title)}</text>"
        f"<text>{saxutils.escape(body)}</text>"
        "</binding></visual></toast>"
    )
    b64 = base64.b64encode(xml_str.encode("utf-8")).decode("ascii")
    ps = (
        "$ErrorActionPreference='SilentlyContinue'\n"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        f"$x=[Windows.Data.Xml.Dom.XmlDocument]::new(); $x.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))\n"
        "$t=New-Object Windows.UI.Notifications.ToastNotification $x\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Windows PowerShell').Show($t)"
    )
    enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", enc],
            timeout=30, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log("桌面弹窗已发送")
    except Exception as e:
        log(f"桌面弹窗发送失败: {e}")


def get_sendkeys(cfg):
    """汇总所有 SendKey（主密钥 + 额外密钥列表），去重"""
    keys = []
    k0 = (cfg["notify"].get("serverchan_sendkey") or "").strip()
    if k0:
        keys.append(k0)
    for k in (cfg["notify"].get("serverchan_sendkeys") or []):
        if k and k.strip():
            keys.append(k.strip())
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def send_serverchan(cfg, title, desp):
    """微信推送（Server酱 sct.ftqq.com），支持同时推送给多个微信账号"""
    if not cfg["notify"].get("wechat_serverchan"):
        return False
    keys = get_sendkeys(cfg)
    if not keys:
        log("微信推送未配置（SendKey 为空），跳过")
        return False
    ok = True
    for key in keys:
        try:
            url = f"https://sctapi.ftqq.com/{key}.send"
            data = urllib.parse.urlencode({"title": title[:32], "desp": desp}).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0:
                log(f"微信推送成功 ({key[:8]}...)")
            else:
                log(f"微信推送失败 ({key[:8]}...): {result}")
                ok = False
        except Exception as e:
            log(f"微信推送异常 ({key[:8]}...): {e}")
            ok = False
    return ok


def matches_ignore(cfg, title):
    return any(k in title for k in cfg["ignore_keywords"])


# ---------------------------------------------------------------- 消息组装

def build_new_books_desp(cfg, items):
    parts = []
    for i, it in enumerate(items, 1):
        status_badge = "✅ 在售" if it["status"] == "saleable" else f"⚠️ {it['status_str']}"
        img = f"![封面]({it['cover']})\n\n" if it["cover"] else ""
        parts.append(
            f"### {i}. {it['title']}\n\n"
            f"{img}"
            f"- 价格：**{fmt_price(cfg, it['price_min'], it['price_max'])}**\n"
            f"- 状态：{status_badge}\n"
            f"- 上架时间：{fmt_ts(it['up_at_ms'])}\n"
            f"- 库存：{it['quantity'] if it['quantity'] is not None else '未知'} 件"
            f" | 已售：{it['total_sold']} 件\n"
            f"- [👉 立即抢购]({it['url']})"
        )
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------- 仪表盘

def write_html(cfg, current, last_check):
    items = sorted(current.values(), key=lambda x: x.get("up_at_ms") or 0, reverse=True)
    cards = []
    for it in items:
        status_cls = "on" if it["status"] == "saleable" else "off"
        cover_html = (f'<img src="{saxutils.escape(it["cover"])}" alt="">'
                      if it["cover"] else '<div class="noimg">📕</div>')
        cards.append(f"""
<div class="card">
  {cover_html}
  <div class="body">
    <div class="title">{saxutils.escape(it["title"])}</div>
    <div><span class="badge {status_cls}">{saxutils.escape(it["status_str"])}</span></div>
    <div class="price">{fmt_price(cfg, it["price_min"], it["price_max"])}</div>
    <div class="meta">上架：{fmt_ts(it["up_at_ms"])}</div>
    <div class="meta">库存：{it["quantity"] if it["quantity"] is not None else "未知"} 件 · 已售 {it["total_sold"]} 件</div>
    <a class="buy" href="{saxutils.escape(it["url"])}" target="_blank">立即抢购 →</a>
  </div>
</div>""")
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>书店上新监控 - 当前在售</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background: #f5f6f8; color: #222; padding: 24px; }}
header {{ max-width: 1100px; margin: 0 auto 20px; display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }}
h1 {{ font-size: 22px; }}
.muted {{ color: #888; font-size: 13px; }}
.grid {{ max-width: 1100px; margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 16px; }}
.card {{ background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); display: flex; flex-direction: column; }}
.card img {{ width: 100%; aspect-ratio: 1/1; object-fit: cover; background: #eee; }}
.noimg {{ width: 100%; aspect-ratio: 1/1; display: flex; align-items: center; justify-content: center; font-size: 60px; background: #fafafa; }}
.body {{ padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 6px; flex: 1; }}
.title {{ font-size: 14px; line-height: 1.45; font-weight: 600; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 2.9em; }}
.price {{ color: #e4393c; font-weight: 700; font-size: 15px; }}
.badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }}
.badge.on {{ background: #e8f7ee; color: #0a8f4d; }}
.badge.off {{ background: #fdeaea; color: #d33; }}
.meta {{ color: #999; font-size: 12px; }}
.buy {{ display: block; margin-top: auto; text-align: center; background: #e4393c; color: #fff; padding: 8px; border-radius: 6px; text-decoration: none; font-size: 13px; }}
footer {{ max-width: 1100px; margin: 18px auto 0; color: #aaa; font-size: 12px; text-align: center; }}
</style>
</head>
<body>
<header>
  <h1>📚 {saxutils.escape(cfg["shop_name"])} · 在售商品</h1>
  <span class="muted">最后检查：{last_check} · 在售 {len(items)} 件 · 每分钟自动刷新</span>
</header>
<div class="grid">
  {''.join(cards) if cards else '<div class="card"><div class="body"><div class="meta">暂无在售商品</div></div></div>'}
</div>
<footer>本页面由监控脚本自动生成 · 发现新书会通过微信/桌面通知提醒</footer>
</body>
</html>"""
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html_doc)


# ---------------------------------------------------------------- 主流程

def main():
    # 云端长循环模式：python monitor.py --loop N 表示连续检查 N 轮，每轮间隔 60 秒
    if "--loop" in sys.argv:
        idx = sys.argv.index("--loop")
        try:
            n = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            n = 1
        rc = 0
        for i in range(n):
            t0 = time.time()
            try:
                rc = _main()
            except Exception as e:
                log(f"循环内异常: {e}")
                rc = 1
            if i < n - 1:
                time.sleep(max(1.0, 60 - (time.time() - t0)))
        return rc

    if not acquire_lock():
        log("已有实例在运行，本次跳过")
        return 0
    try:
        return _main()
    finally:
        release_lock()


def _main():
    # 日志轮转
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
    except OSError:
        pass

    cfg = load_config()
    state = load_state()
    state.setdefault("consecutive_failures", 0)

    # 测试模式：只发一条测试通知
    if "--test" in sys.argv:
        log("发送测试通知...")
        send_serverchan(cfg, "✅ 书店监控测试", "这是一条测试消息，收到说明微信推送配置成功！")
        send_toast(cfg, "✅ 书店监控测试", "这是一条测试弹窗，收到说明桌面通知正常！")
        log("测试通知发送完毕")
        return 0

    # 抓取商品（失败重试 2 次）
    items = None
    for attempt in (1, 2):
        try:
            items = fetch_all_goods(cfg["check_page_size"])
            break
        except Exception as e:
            log(f"抓取失败（第 {attempt} 次）: {e}")
            if attempt < 2:
                time.sleep(3)

    if items is None:
        state["consecutive_failures"] += 1
        n = state["consecutive_failures"]
        save_state(state)
        if n == cfg["notify"]["offline_warn_after_failures"]:
            send_serverchan(cfg, "⚠️ 书店监控异常",
                            f"已连续 **{n}** 次抓取失败（约 {n} 分钟）。\n\n"
                            "请检查电脑网络或是否被安全软件拦截。")
            send_toast(cfg, "⚠️ 书店监控异常", f"连续 {n} 次抓取失败，请检查网络")
        return 2

    state["consecutive_failures"] = 0
    current = {}
    for it in items:
        gid = it.get("goodsId")
        if gid is None:
            continue
        current[str(gid)] = summarize(it, cfg)

    known = state.get("goods") or {}
    first_run = not known and "last_check" not in state
    new_ids = [g for g in current if g not in known]
    removed_ids = [g for g in known if g not in current]
    new_items = [current[g] for g in new_ids]
    alert_items = [it for it in new_items if not matches_ignore(cfg, it["title"])]
    for it in new_items:
        if matches_ignore(cfg, it["title"]):
            log(f"新增商品但命中忽略词，不提醒: {it['title']}")

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if first_run:
        # 首次运行：建立基线，发一条启动消息验证推送通道
        lines = "\n".join(
            f"- {it['title']} — {fmt_price(cfg, it['price_min'], it['price_max'])}"
            for it in sorted(current.values(), key=lambda x: x.get("up_at_ms") or 0, reverse=True)[:10]
        )
        send_serverchan(
            cfg, "✅ 书店上新监控已启动",
            f"监控正式开始运行，每 **1 分钟** 检查一次 {cfg['shop_name']}。\n\n"
            f"当前在售商品 **{len(current)}** 件：\n\n{lines or '（暂无）'}\n\n"
            "发现新书上新后会第一时间推送，祝抢书顺利 🎉")
        send_toast(cfg, "✅ 书店上新监控已启动",
                   f"当前在售 {len(current)} 件，每分钟检查一次上新")
        log(f"首次运行：建立基线，当前在售 {len(current)} 件")
    else:
        if alert_items:
            log(f"发现新商品 {len(alert_items)} 件：{[it['title'] for it in alert_items]}")
            title = (f"📚 上新：{alert_items[0]['title']}" if len(alert_items) == 1
                     else f"📚 书店上新 {len(alert_items)} 件！")
            desp = "## 📚 检测到新书上新，速抢！\n\n" + build_new_books_desp(cfg, alert_items)
            send_serverchan(cfg, title, desp)
            toast_lines = "\n".join(
                f"{i}. {it['title']}  {fmt_price(cfg, it['price_min'], it['price_max'])}"
                for i, it in enumerate(alert_items[:5], 1))
            if len(alert_items) > 5:
                toast_lines += f"\n…等共 {len(alert_items)} 件，详见微信推送"
            send_toast(cfg, f"📚 书店上新 {len(alert_items)} 件！", toast_lines)
        if removed_ids and cfg["notify"].get("notify_sold_out"):
            removed = [known[g] for g in removed_ids]
            log(f"商品下架/售罄 {len(removed)} 件：{[it['title'] for it in removed]}")
            lines = "\n".join(
                f"- {it['title']}（{fmt_price(cfg, it['price_min'], it['price_max'])}）"
                for it in removed)
            send_serverchan(cfg, f"⚠️ 有商品下架/售罄（{len(removed)} 件）",
                            f"以下商品已从在售列表消失：\n\n{lines}")
        if not new_ids and not removed_ids:
            log("无变化")

    state["goods"] = current
    state["last_check"] = now_str
    save_state(state)
    with open(LAST_GOODS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    write_html(cfg, current, now_str)
    if first_run:
        log(f"检查完成：基线已建立，在售 {len(current)} 件（首次运行不提醒）")
    else:
        log(f"检查完成：在售 {len(current)} 件 | 新增 {len(alert_items)} 件提醒 | 下架 {len(removed_ids)} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
