#!/usr/bin/env python3
"""云嘉每日资讯海报 - 抓取新闻 → 生成HTML → 导出PNG → 发送飞书"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value or "")
    value = html.unescape(value)
    value = re.sub(r"showPlayer\s*\(.*?\);?", "", value, flags=re.S)
    value = re.sub(r"\{[^}]*(?:script|video|src|poster|scriptId)[^}]*\}", "", value, flags=re.S | re.I)
    value = re.sub(r"\w+Url\s*:\s*['\"][^'\"]*['\"]", "", value, flags=re.I)
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"^[\s;,\)\}\]]+", "", value)
    # 去末尾装饰箭头：→ ↓ ← ↑ ➡ ⬇ 等（中新网标题常用）
    value = re.sub(r"[\s]*[→↓←↑➡⬇⬆↗↙↘↖][\s]*$", "", value)
    return re.sub(r"\s+", " ", value).strip()


def truncate(value: str, limit: int, smart: bool = True) -> str:
    """截断字符串到指定长度。smart=True 时优先在完整句子边界处截断，避免\"…\"尾巴。"""
    if len(value) <= limit:
        return value
    if smart:
        # 回溯到最近的自然断句标点（。！？；）
        for i in range(limit, max(limit - 20, 0), -1):
            if value[i - 1] in "。！？；?!":
                return value[:i]
    return value[: limit - 1].rstrip() + "…"


def text(node: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def summarize(raw: str, title: str = "", limit: int = 80) -> str:
    """从新闻正文/描述中提取关键信息生成摘要。

    改进点：
    1. 去电头、去署名
    2. 优先取第一个完整句子（以。结尾）
    3. 若清洗后过短（<10字），尝试从标题提取补充
    4. 若原文以截断符结尾，尝试回溯到上一个完整句子
    """
    text = strip_tags(raw)
    if not text:
        # 没有任何描述时，从标题生成摘要
        if title:
            t = re.sub(r"[|｜_【】\[\]:：—–-]+.*$", "", title).strip()
            return truncate(t, limit)
        return ""

    # 1. 去电头
    text = re.sub(
        r"^(新华社|中新网|中新社|人民网|央视新闻|央广网|经济日报|光明日报|"
        r"中央政法委|法治日报|工人日报|中国青年报)[^，。？]{0,15}电\s*",
        "", text
    )
    # 2. 去记者/通讯员署名（含"记者"关键词）
    text = re.sub(r"[（(](?:记者|通讯员|见习记者)[^）)]{0,20}[）)]\s*", "", text)
    # 2b. 去裸括号署名：开头处的 (中文名 中文名 ...) 或（中文名），不含其他内容
    text = re.sub(r"^[（(][\u4e00-\u9fff]{2,4}(?:\s+[\u4e00-\u9fff]{2,4}){0,3}[）)]\s*", "", text)
    # 3. 去快讯/直播前缀
    text = re.sub(r"^(新华社|中新网)快讯[：:]\s*", "", text)
    text = re.sub(r"^(快讯|最新|独家)[：:]\s*", "", text)
    # 4. 去"总台记者获悉"等引导语
    text = re.sub(r"^(总台记者|本报|本网).*?(获悉|了解到|消息)[，,]\s*", "", text)
    # 5. 去日期开头的冗余前缀
    text = re.sub(r"^\d{4}年\d{1,2}月\d{1,2}日\s*[，,]?\s*", "", text)
    # 5b. 去"X月X日"（无年份）或"X月X日，"前缀
    text = re.sub(r"^\d{1,2}月\d{1,2}日\s*[，,]?\s*", "", text)
    # 5c. 去"记者从XX获悉/了解到"等套话前缀 —— 这类开场白不承载信息
    text = re.sub(
        r"^(?:记者\d{0,2}[日月]?\s*从[^，。；]{2,40}(?:获悉|了解到)[，,]\s*"
        r"|据[^，。；]{2,40}(?:消息|通报)\s*(?:称|表示)?[，,]\s*"
        r"|[^，。；]{2,30}(?:局|厅|委|办|部|署|院)\d{0,2}[日月]?\s*(?:消息|通报)(?:称|表示)?[，,]\s*)",
        "", text
    )
    # 6. 去【栏目头】如【本期导读】【深度观察】【讲习所】等
    text = re.sub(r"^【[^】]{1,30}】\s*", "", text)
    # 7. 去"原标题：""来源："等冗余前缀
    text = re.sub(r"^(原标题|原题|来源)[：:]\s*", "", text)
    text = text.strip()

    if not text:
        # 清洗后为空，降级用标题
        if title:
            return truncate(title, limit)
        return ""

    # 8. 处理截断标记：若文本以…（或...）结尾，回溯到上一个完整句子
    if text.rstrip().endswith("…") or text.rstrip().endswith("..."):
        # 去除末尾的省略号
        stripped = re.sub(r"…\s*$", "", text)
        stripped = re.sub(r"\.{2,}\s*$", "", stripped)
        # 尝试回溯到最后一个。作为完整句子边界
        last_period = stripped.rfind("。")
        if last_period > 0:
            text = stripped[:last_period + 1]
        else:
            # 没有。则尝试用最后一个，断句
            last_comma = stripped.rfind("，")
            if last_comma > 10:  # 至少留一些有意义的内容
                text = stripped[:last_comma]
            else:
                text = stripped
        text = text.strip()

    if not text:
        if title:
            return truncate(title, limit)
        return ""

    # 优先取第一个完整句子
    sentences = re.split(r"(?<=。)", text)
    summary_parts = []
    total_len = 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if total_len + len(sent) <= limit:
            summary_parts.append(sent)
            total_len += len(sent)
        else:
            break
    if summary_parts:
        candidate = "".join(summary_parts)
        if len(candidate) <= limit:
            return candidate

    # 没有完整句子或超长，直接截断
    return truncate(text, limit)


# ---------------------------------------------------------------------------
# 内容优先级评分
# ---------------------------------------------------------------------------

# 社会/民生/法律 相关关键词 → 加分
_PRIORITY_KEYWORDS = [
    # 法律相关
    "法", "诉讼", "判决", "律师", "法院", "检察", "违法", "犯罪", "刑事",
    "合同", "侵权", "赔偿", "仲裁", "合规", "监管", "处罚", "执法", "司法",
    # 社会民生
    "社会", "民生", "医疗", "教育", "就业", "住房", "养老", "社保", "消费",
    "安全", "食品", "环境", "污染", "交通", "物业", "租房", "劳动者", "工资",
    # 商业经济
    "经济", "市场", "企业", "产业", "投资", "科技", "创新", "数字",
    "GDP", "营收", "利润", "增长", "贸易", "金融", "银行", "上市",
    "税", "出口", "进口", "制造", "供应链", "并购", "融资",
    # 热点类
    "调查", "曝光", "查处", "通报", "案件", "判决", "维权", "投诉",
]


def _score_article(title: str, summary: str) -> int:
    """对文章打分，分数越高越优先选中。"""
    text = title + summary
    score = 0
    for kw in _PRIORITY_KEYWORDS:
        if kw in text:
            score += 1
    # 纯公告/电讯类降权
    if re.match(r"^(新华社|中新网|人民日报|新华网).{0,5}(电|讯|消息)", title):
        score -= 3
    # 政论/评论/随笔/述评类降权（没具体事件，摘要只能是空话）
    _OPINION_PENALTY = ["随笔", "评论", "述评", "解读", "时评", "综述", "观察"]
    for w in _OPINION_PENALTY:
        if w in title:
            score -= 5
            break
    # 摘要空泛检测：全篇由套话词组成的扣分
    _FLUFF_WORDS = {"高质量", "深度融", "良性循环", "新格局", "推动", "赋能", "持续优化"}
    fluff_count = sum(1 for w in _FLUFF_WORDS if w in summary)
    if fluff_count >= 3 and len(summary) < 80:
        score -= 4
    return score


def select_articles(all_articles: list[dict], scope: str, target: int) -> list[dict]:
    """从抓到的文章中，按优先级选出 target 条。"""
    candidates = [a for a in all_articles if a.get("scope") == scope]
    if not candidates:
        return []
    for a in candidates:
        a["_score"] = _score_article(a.get("title", ""), a.get("summary", ""))
    candidates.sort(key=lambda x: x["_score"], reverse=True)
    return candidates[:target]


# ---------------------------------------------------------------------------
# RSS 抓取
# ---------------------------------------------------------------------------

def fetch_feed(source: dict, timeout: int = 10) -> list[dict]:
    req = urllib.request.Request(
        source["url"],
        headers={"User-Agent": "YunjiaDailyPoster/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        root = ET.fromstring(resp.read())

    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    now_dt = dt.datetime.now()
    today = now_dt.date()
    yesterday_date = today - dt.timedelta(days=1)
    recent_dates = {today.isoformat(), yesterday_date.isoformat()}
    recent_dates_cn = {today.strftime("%Y年%m月%d日"), yesterday_date.strftime("%Y年%m月%d日")}

    articles = []
    for item in items[: source.get("limit", 4)]:
        pub_date = text(item, ("pubDate", "published",
                               "{http://www.w3.org/2005/Atom}published",
                               "{http://www.w3.org/2005/Atom}updated"))
        if pub_date:
            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(pub_date)
                pub_date_date = pub_dt.date()
                if pub_date_date != today and pub_date_date != yesterday_date:
                    continue
            except Exception:
                if not any(d in pub_date for d in [*recent_dates, *recent_dates_cn]):
                    continue

        title = text(item, ("title", "{http://www.w3.org/2005/Atom}title"))
        summary = text(item, (
            "description", "summary",
            "{http://www.w3.org/2005/Atom}summary",
            "{http://www.w3.org/2005/Atom}content",
        ))
        link = text(item, ("link",))
        if not link:
            link_node = item.find("{http://www.w3.org/2005/Atom}link")
            link = link_node.get("href", "") if link_node is not None else ""
        if title:
            raw_text = summary or ""
            articles.append({
                "scope": source.get("scope", "domestic"),
                "category": source.get("category", "综合资讯"),
                "source": source.get("name", "网络"),
                "title": truncate(strip_tags(title), 36),
                "raw_content": strip_tags(raw_text),   # 保留原始RSS内容，供AI摘要和回退使用
                "summary": summarize(raw=raw_text, title=strip_tags(title), limit=65),
                "url": link,
            })
    return articles


def fetch_articles(config: dict) -> tuple[list[dict], list[str]]:
    """抓取所有RSS源，按优先级评分后选出目标数量文章。"""
    all_articles: list[dict] = []
    errors: list[str] = []
    for source in config.get("sources", []):
        try:
            all_articles.extend(fetch_feed(source))
        except Exception as exc:
            errors.append(f'{source.get("name", "未知来源")}: {exc}')

    # 去重：同一标题只保留第一条（不同频道可能抓到相同文章）
    seen: set[str] = set()
    deduped: list[dict] = []
    for a in all_articles:
        title = a.get("title", "")
        if title and title not in seen:
            seen.add(title)
            deduped.append(a)
    all_articles = deduped

    articles: list[dict] = []
    for scope, target in (
        ("domestic", config.get("domestic_count", 10)),
        ("international", config.get("international_count", 6)),
    ):
        selected = select_articles(all_articles, scope, target)
        # 如果选出的文章不足，补充 fallback
        if len(selected) < target:
            fallback = [
                a for a in config.get("fallback_articles", [])
                if a.get("scope") == scope
            ]
            existing_titles = {a.get("title") for a in selected}
            for fb in fallback:
                if fb.get("title") not in existing_titles:
                    selected.append(fb)
                if len(selected) >= target:
                    break
        articles.extend(selected[:target])
    return articles, errors


def fallback_articles(config: dict) -> list[dict]:
    fb = config.get("fallback_articles", [])
    return [
        *[a for a in fb if a.get("scope") == "domestic"][:config.get("domestic_count", 10)],
        *[a for a in fb if a.get("scope") == "international"][:config.get("international_count", 6)],
    ]


# ---------------------------------------------------------------------------
# HTML 生成
# ---------------------------------------------------------------------------

def file_uri(path_value: str) -> str:
    p = Path(path_value)
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve().as_uri()


def article_html(articles: list[dict]) -> str:
    cards = []
    for i, item in enumerate(articles, 1):
        cards.append(f"""
            <article class="news-card">
              <div class="news-index">{i:02d}</div>
              <div class="news-content">
                <div class="news-meta">
                  <span class="category">{html.escape(item.get("category", "综合资讯"))}</span>
                  <span class="source">来源：{html.escape(item.get("source", "网络"))}</span>
                </div>
                <h2>{html.escape(item["title"])}</h2>
                <p>{html.escape(item.get("summary", ""))}</p>
              </div>
            </article>""")
    return "\n".join(cards)


def build_html(config: dict, articles: list[dict], poster_date: dt.date) -> str:
    template = (ROOT / "templates" / "poster.html").read_text(encoding="utf-8")
    domestic = [a for a in articles if a.get("scope") == "domestic"]
    international = [a for a in articles if a.get("scope") == "international"]

    # 根据日期从毛主席语录中选取（同一日期始终返回同一条）
    quotes = config.get("mao_quotes", [])
    if quotes:
        idx = int(hashlib.md5(poster_date.isoformat().encode()).hexdigest(), 16) % len(quotes)
        daily_quote = quotes[idx]
    else:
        daily_quote = config.get("quote", "")

    quote_source = config.get("quote_source", "每日寄语")

    repl = {
        "{{LOGO_URI}}": file_uri(config["brand"]["logo"]),
        "{{QR_URI}}": file_uri(config["brand"]["qr_code"]),
        "{{HEADER_URI}}": file_uri(config["brand"]["header_image"]),
        "{{BRAND_NAME}}": html.escape(config["brand"]["name"]),
        "{{BRAND_SUBTITLE}}": html.escape(config["brand"]["subtitle"]),
        "{{SOLAR_DATE}}": poster_date.strftime("%Y年%m月%d日"),
        "{{WEEKDAY}}": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][poster_date.weekday()],
        "{{DATE_ISO}}": poster_date.isoformat(),
        "{{ISSUE}}": poster_date.strftime("%Y%m%d"),
        "{{DOMESTIC_ARTICLES}}": article_html(domestic),
        "{{INTERNATIONAL_ARTICLES}}": article_html(international),
        "{{RECRUITMENT}}": html.escape(config["recruitment"]),
        "{{QUOTE}}": html.escape(daily_quote),
        "{{QUOTE_SOURCE}}": html.escape(quote_source),
        "{{CONTACT_LABEL}}": html.escape(config["brand"].get("contact_label", "扫码关注")),
        "{{DISCLAIMER}}": html.escape(config["disclaimer"]),
    }
    for k, v in repl.items():
        template = template.replace(k, v)
    return template


# ---------------------------------------------------------------------------
# PNG 导出 (Playwright)
# ---------------------------------------------------------------------------

def export_png(html_path: Path, png_path: Path) -> None:
    """使用 Playwright 驱动系统 Edge 浏览器导出长图 PNG."""
    png_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt, browser_config in enumerate([
        {"name": "Edge", "channel": "msedge"},
        {"name": "Chromium", "channel": None},
    ]):
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                launch_args = {
                    "headless": True,
                    "args": ["--no-sandbox", "--disable-gpu", "--hide-scrollbars"],
                }
                if browser_config["channel"]:
                    launch_args["channel"] = browser_config["channel"]

                browser = p.chromium.launch(**launch_args)
                page = browser.new_page(
                    viewport={"width": 1080, "height": 2520},
                    device_scale_factor=1,
                )
                page.goto(html_path.resolve().as_uri(), wait_until="networkidle", timeout=30000)
                page.screenshot(path=str(png_path), full_page=True)
                browser.close()
                print(f"[PNG] 导出成功 ({browser_config['name']}): {png_path}")
                return
        except Exception as e:
            if attempt == 0:
                print(f"[PNG] {browser_config['name']} 失败 ({e}), 尝试 Chromium...")
            else:
                raise RuntimeError(f"PNG 导出失败 (Edge & Chromium 均不可用): {e}")


# ---------------------------------------------------------------------------
# 飞书集成
# ---------------------------------------------------------------------------

def _get_tenant_token(config: dict) -> str:
    """获取飞书 tenant_access_token"""
    feishu = config.get("feishu", {})
    app_id = feishu.get("app_id", "")
    app_secret = feishu.get("app_secret", "")

    if not app_id or not app_secret:
        raise RuntimeError("飞书配置缺失: 请在 config.json 中填写 feishu.app_id 和 feishu.app_secret")

    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data.get('msg', data)}")
    return data["tenant_access_token"]


def _upload_image(token: str, png_path: Path) -> str:
    """上传图片到飞书, 返回 image_key"""
    with open(png_path, "rb") as f:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            files={"image": (png_path.name, f, "image/png")},
            data={"image_type": "message"},
            timeout=30,
        )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"上传图片失败: {data.get('msg', data)}")
    image_key = data["data"]["image_key"]
    print(f"[飞书] 图片上传成功, image_key={image_key}")
    return image_key


def send_to_feishu(config: dict, png_path: Path, poster_date: dt.date) -> bool:
    """通过飞书 IM API 发送海报到指定群聊."""
    feishu = config.get("feishu", {})
    chat_id = os.environ.get("FEISHU_CHAT_ID") or feishu.get("chat_id", "")

    if not chat_id:
        raise RuntimeError("飞书 chat_id 未配置: 请在 config.json 中填写 feishu.chat_id")

    token = _get_tenant_token(config)
    image_key = _upload_image(token, png_path)

    date_str = poster_date.strftime("%Y年%m月%d日")
    text_resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": f"☀️ 云嘉每日资讯 — {date_str}\n今日海报已生成，请查收 ↓"}),
        },
        timeout=10,
    )
    text_data = text_resp.json()
    if text_data.get("code") != 0:
        print(f"[飞书] 文字消息发送失败: {text_data.get('msg', text_data)}")

    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "receive_id": chat_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        },
        timeout=10,
    )
    result = resp.json()
    if result.get("code") == 0:
        print("[飞书] 图片消息发送成功!")
        return True
    else:
        print(f"[飞书] 发送失败: {result}")
        return False


# ---------------------------------------------------------------------------
# AI 摘要 (DeepSeek)
# ---------------------------------------------------------------------------

def _enrich_article_content(article: dict) -> str:
    """当 RSS 摘要不足时，尝试从原文 URL 获取完整内容。

    返回用于 AI 摘要的文本内容。优先使用 raw_content（原始RSS），
    回退到已处理的 summary，二者都不足时 fetch 原文。
    """
    import requests as req

    title = article.get("title", "")
    # 优先用 raw_content（fetch_feed 保留的原始 RSS 描述），其次用已处理 summary
    raw = article.get("raw_content", "") or article.get("summary", "")
    url = article.get("url", "")

    text = raw if raw else title

    # 判断是否不足：字数不够或与标题高度重复
    need_fetch = False
    if len(text) < 30:
        need_fetch = True
    elif len(text) < 60:
        common = sum(1 for c in text if c in title)
        if common > len(text) * 0.6:
            need_fetch = True

    if not need_fetch or not url:
        return text

    try:
        resp = req.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text

        # 去掉 script/style/标签/HTML实体
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S)
        html = re.sub(r"<[^>]+>", "", html)
        html = re.sub(r"&[a-z]+;", " ", html)

        # 提取行，跳过导航垃圾（短行、单字符、常见导航词）
        _NAV_GARBAGE = {"首页", "新闻", "财经", "法治", "社会", "生活", "关于", "登录", "注册",
                        "导航", "搜索", "菜单", "返回", "顶部", "版权", "声明", "广告"}
        lines = []
        for l in html.split("\n"):
            s = l.strip()
            if not s or len(s) < 4 or s in _NAV_GARBAGE:
                continue
            lines.append(s)

        body = "\n".join(lines)

        # 定位正文起点：跳到第一个 ≥40 字符的段落（导航通常短，正文段落长）
        start = 0
        for i, line in enumerate(lines):
            if len(line) >= 40:
                start = i
                break
        body = "\n".join(lines[start:])

        # 取前 800 字
        if len(body) > 800:
            body = body[:800]
        if len(body) > len(text) + 20:
            return f"{title}\n{body}"
    except Exception:
        pass

    return text


def ai_summarize_batch(articles: list[dict], limit: int = 68, api_key: str = "") -> None:
    """用 DeepSeek API 对一批新闻做真正的 AI 摘要。

    与 summarize() 不同，这个函数让 AI 理解新闻后用自己的话浓缩成 limit 字以内的摘要，
    而不是摘抄原文开头。
    当 RSS 摘要不足以支撑 AI 理解时，会自动从原文 URL 获取完整内容。
    """
    import requests as req

    if not articles or not api_key:
        return

    # 先为摘要不足的文章补充原文内容
    enriched_count = 0
    for a in articles:
        raw = a.get("raw_summary", "") or a.get("summary", "")
        old_text = strip_tags(raw) if raw else ""
        new_text = _enrich_article_content(a)
        if len(new_text) > len(old_text) + 20:
            a["_enriched"] = True
            enriched_count += 1
        a["_feed_text"] = new_text

    if enriched_count:
        print(f"  [AI摘要] {enriched_count} 条内容不足，已从原文补充")

    parts = []
    for i, a in enumerate(articles):
        title = a.get("title", "")
        text = a.get("_feed_text", "")
        parts.append(f"[{i + 1}] 标题：{title}\n内容：{text}")

    prompt = (
        f"你是一位为律师事务所资讯海报服务的新闻编辑。请为以下 {len(articles)} 条新闻各写一条摘要。\n"
        f"每条摘要至少50字、不超过{limit}字（少于50字不合格）。必须有实质信息量：包含事件结果、法律要点、处罚金额、涉及人数等关键细节。\n"
        "要求：\n"
        "1. 用自己的话概括，补充标题中未提及的关键信息\n"
        "2. 禁止使用\"据悉\"\"记者获悉\"\"X月X日\"等套话开头\n"
        "3. 不要重复标题已有信息，要写出标题没说的核心内容\n"
        "4. 严格按格式输出：序号. 摘要内容\n\n"
        + "\n\n".join(parts)
    )

    try:
        resp = req.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 3000,
                "temperature": 0.3,
            },
            timeout=45,
        )
        if resp.status_code != 200:
            print(f"  [AI摘要] API 错误 {resp.status_code}: {resp.text[:100]}")
            return

        content = resp.json()["choices"][0]["message"]["content"].strip()
        for line in content.split("\n"):
            m = re.match(r"^(\d+)[\.、)]\s*(.+)", line.strip())
            if m:
                idx = int(m.group(1)) - 1
                summary = m.group(2).strip()
                if 0 <= idx < len(articles) and len(summary) >= 45:
                    articles[idx]["summary"] = truncate(summary, limit)
                    articles[idx]["_ai_summary"] = True
        print(f"  [AI摘要] 完成 {sum(1 for a in articles if a.get('_ai_summary'))}/{len(articles)} 条")
    except Exception as e:
        print(f"  [AI摘要] 调用失败: {e}")
        print("  将回退到规则摘要...")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="云嘉每日资讯海报 - 抓取/生成/导出/推送")
    parser.add_argument("--date", help="海报日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--no-fetch", action="store_true", help="只使用示例资讯")
    parser.add_argument("--html-only", action="store_true", help="只生成 HTML")
    parser.add_argument("--no-png", action="store_true", help="不导出 PNG")
    parser.add_argument("--no-feishu", action="store_true", help="不发送飞书")
    parser.add_argument("--feishu-only", action="store_true", help="仅发送已有 PNG 到飞书")
    parser.add_argument("--fetch-only", action="store_true",
                        help="仅抓取RSS并保存原始文章到JSON（供AI摘要后使用）")
    parser.add_argument("--articles-json", type=str, default="",
                        help="使用指定JSON文件中的文章数据生成海报（跳过RSS抓取）")
    parser.add_argument("--output-dir", type=str, default="",
                        help="指定输出目录路径（默认使用脚本同目录下 output/）")
    args = parser.parse_args()

    poster_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    config = load_json(ROOT / "config.json")
    output_dir = Path(args.output_dir) if args.output_dir else (ROOT / "output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # --fetch-only: 仅抓取RSS，保存原始数据供AI处理
    if args.fetch_only:
        articles, errors = fetch_articles(config)
        raw_path = output_dir / f"raw-articles-{poster_date.isoformat()}.json"
        with raw_path.open("w", encoding="utf-8") as f:
            json.dump(articles, f, ensure_ascii=False, indent=2)
        print(f"[抓取] 保存 {len(articles)} 条原始文章到 {raw_path}")
        if errors:
            for e in errors:
                print(f"  - {e}")
        return

    # --feishu-only: 跳过生成，直接发送已有文件
    if args.feishu_only:
        basename_str = f"每日资讯-{poster_date.isoformat()}"
        png_path = ROOT / "output" / f"{basename_str}.png"
        if not png_path.exists():
            print(f"错误: PNG 文件不存在: {png_path}")
            sys.exit(1)
        try:
            send_to_feishu(config, png_path, poster_date)
        except Exception as e:
            print(f"飞书发送失败: {e}")
            sys.exit(1)
        return

    # 1. 抓取/获取文章
    if args.articles_json:
        articles_path = Path(args.articles_json)
        if not articles_path.exists():
            print(f"错误: 文章数据文件不存在: {articles_path}")
            sys.exit(1)
        with articles_path.open("r", encoding="utf-8") as f:
            articles = json.load(f)
        print(f"[文章] 从 {articles_path} 加载 {len(articles)} 条文章")
    else:
        articles, errors = (
            (fallback_articles(config), [])
            if args.no_fetch
            else fetch_articles(config)
        )
        # 自动摘要：有 API key 用 AI，没 key 回退规则清洗
        if not args.no_fetch:
            api_key = config.get("deepseek_api_key", "").strip()
            if api_key:
                print("[摘要] AI 智能摘要...")
                for a in articles:
                    a["raw_summary"] = a.get("summary", "")
                ai_summarize_batch(articles, limit=68, api_key=api_key)
                # AI 未覆盖的文章回退到规则清洗
                fallback_count = 0
                for a in articles:
                    if not a.get("_ai_summary"):
                        raw = a.get("raw_content", "") or a.get("raw_summary", "") or a.get("summary", "")
                        improved = summarize(raw=raw, title=a.get("title", ""), limit=65)
                        if improved and len(improved) >= 8:
                            a["summary"] = improved
                            fallback_count += 1
                if fallback_count:
                    print(f"  [摘要] {fallback_count} 条 AI 未覆盖，已用规则清洗补齐")
            else:
                print("[摘要] 规则清洗摘要...")
                for a in articles:
                    raw_summary = a.get("summary", "")
                    raw_title = a.get("title", "")
                    improved = summarize(raw=raw_summary or raw_title, title=raw_title, limit=65)
                    if improved and len(improved) >= 8:
                        a["summary"] = improved

    # 2. 生成 HTML
    basename_str = f"每日资讯-{poster_date.isoformat()}"
    html_path = output_dir / f"{basename_str}.html"
    png_path = output_dir / f"{basename_str}.png"

    html_content = build_html(config, articles, poster_date)
    html_path.write_text(html_content, encoding="utf-8")
    print(f"[HTML] {html_path}")

    if not args.articles_json and errors:
        print("部分资讯源获取失败，已使用备用内容：")
        for e in errors:
            print(f"  - {e}")

    if args.html_only:
        return

    # 3. 导出 PNG
    if not args.no_png:
        try:
            export_png(html_path, png_path)
        except Exception as e:
            print(f"PNG 导出失败: {e}")
            if not args.no_feishu:
                print("跳过飞书发送（无 PNG 文件）")
            sys.exit(1)

    # 4. 发送飞书
    if not args.no_feishu and png_path.exists():
        try:
            send_to_feishu(config, png_path, poster_date)
        except Exception as e:
            print(f"飞书发送失败: {e}")
            sys.exit(1)

    print("全部完成!")


if __name__ == "__main__":
    main()
