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


def apply_env_overrides(config: dict) -> None:
    """用环境变量覆盖敏感配置（CI 环境注入 GitHub Secrets）。"""
    env_map = {
        "DEEPSEEK_API_KEY": ("deepseek_api_key", None),
        "FEISHU_APP_ID": ("feishu", "app_id"),
        "FEISHU_APP_SECRET": ("feishu", "app_secret"),
        "FEISHU_CHAT_ID": ("feishu", "chat_id"),
    }
    for env_key, (config_key, sub_key) in env_map.items():
        val = os.getenv(env_key, "").strip()
        if val:
            if sub_key is None:
                config[config_key] = val
            else:
                config.setdefault(config_key, {})[sub_key] = val


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
    """截断字符串到指定长度。smart=True 时优先在完整句子边界处截断，避免"…"尾巴。"""
    if len(value) <= limit:
        return value
    if smart:
        # 第一轮：回溯到强断句标点（。！？；）
        for i in range(limit, max(limit - 25, 0), -1):
            if value[i - 1] in "。！？；?!":
                return value[:i]
        # 第二轮：回溯到弱断句标点（，）
        for i in range(limit, max(limit - 25, 0), -1):
            if value[i - 1] == "，":
                return value[: i - 1] + "。"
        # 第三轮：前瞻找句号（允许略微超过 limit，最多多 10 字）
        for i in range(limit, min(limit + 10, len(value))):
            if value[i] in "。！？；?!":
                return value[: i + 1]
        # 第四轮：前瞻找逗号
        for i in range(limit, min(limit + 10, len(value))):
            if value[i] == "，":
                return value[:i] + "。"
        # 最后兜底：硬截断 + 句号，绝不用省略号
        return value[: limit - 1].rstrip("，、。！？；?!") + "。"
    return value[: limit - 1].rstrip("，、。！？；?!") + "。"


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
    # 文化/体育/健康
    "文化", "文物", "非遗", "体育", "赛事", "奥运", "健康", "医疗",
    "医保", "医院", "医生", "药品", "疫苗", "养生",
    # 科技/互联网/环保
    "AI", "人工智能", "算法", "互联网", "平台", "数据", "芯片", "5G",
    "环保", "碳中和", "减排", "绿色", "新能源", "电动汽车", "光伏",
]


def _score_article(title: str, summary: str) -> int:
    """对文章打分，分数越高越优先选中。"""
    text = title + summary
    score = 0
    for kw in _PRIORITY_KEYWORDS:
        if kw in text:
            score += 1
    # 法律/司法/维权内容额外加权（律所核心关注点）
    _LAW_BONUS_KW = ["法院", "判决", "检察", "起诉", "逮捕", "刑事", "民事", "律师",
                     "合同纠纷", "侵权", "赔偿", "仲裁", "诉讼", "立案", "庭审",
                     "司法解释", "执行", "强制执行", "维权", "投诉", "曝光", "查处", "通报"]
    for kw in _LAW_BONUS_KW:
        if kw in title:  # 仅限标题，正文提到不算
            score += 2
            break
    # 纯公告/电讯类降权
    if re.match(r"^(新华社|中新网|人民日报|新华网).{0,5}(电|讯|消息)", title):
        score -= 3
    # 政论/评论/随笔/述评类降权（没具体事件，摘要只能是空话）
    _OPINION_PENALTY = ["随笔", "评论", "述评", "解读", "时评", "综述", "观察", "社论", "特约", "编者按"]
    for w in _OPINION_PENALTY:
        if w in title:
            score -= 5
            break
    # 娱乐/八卦内容降权（不适合律所资讯海报）
    _ENTERTAINMENT_KW = ["综艺", "明星", "娱乐", "网红", "真人秀", "亲子综艺", "电视剧", "电影",
                         "票房", "夺冠", "首金", "开幕式", "闭幕式", "颁奖典礼"]
    for w in _ENTERTAINMENT_KW:
        if w in title:
            score -= 10
            break
    # 软新闻/正能量宣传降权（无实质新闻价值）
    _SOFT_NEWS_KW = ["点赞", "研修班", "体验", "喜讯", "佳话", "暖心", "感动", "成功举办",
                     "圆满落幕", "顺利举行", "幸福感", "获得感", "安全感", "蓬勃发展",
                     "谱写新篇", "昂扬奋进", "砥砺前行", "致敬", "感恩", "加油"]
    for w in _SOFT_NEWS_KW:
        if w in title:
            score -= 5
            break
    # 节日/旅游/软性消费内容降权（律所资讯受众不需要）
    # 注意：这里用 title 而非 text，避免误杀"旅游纠纷"等法律相关内容
    _HOLIDAY_SOFT_KW = [
        # 节日软文
        "端午", "清明", "春节", "元旦", "中秋", "国庆", "元宵", "重阳",
        "圣诞", "感恩节", "母亲节", "父亲节", "情人节", "七夕", "夏至",
        # 旅游/景区/节庆
        "景区", "游客", "民宿", "古镇", "夜游", "研学游", "亲子游", "自驾游",
        "旅游攻略", "打卡", "露营", "网红地", "旅游旺季",
        "盛会", "节庆", "文旅", "文化旅游", "追光", "白夜",
        # 动物/自然科普（无新闻价值）
        "藏羚羊", "产仔", "候鸟", "迁徙",
        # 食品软文（非食安问题）
        "挑桃子", "吃法", "食谱", "美食攻略",
        # 宣传/典礼/活动
        "达沃斯", "论坛参会", "揽客", "夏令营",
    ]
    for w in _HOLIDAY_SOFT_KW:
        if w in title:
            score -= 6
            break
    # 扶贫/乡村振兴/宣传类软文降权（正能量宣传，无实质法律/商业价值）
    _PROPAGANDA_KW = [
        "共富", "山海情", "乡村振兴故事", "扶贫故事", "爱心",
        "送温暖", "志愿者", "奉献精神", "老党员", "最美",
        "先进典型", "模范", "榜样", "感人事迹",
    ]
    for w in _PROPAGANDA_KW:
        if w in title:
            score -= 4
            break
    # 摘要空泛检测：套话词越多越空
    _FLUFF_WORDS = {
        "高质量", "深度融合", "良性循环", "新发展格局", "推动", "赋能", "持续优化",
        "扎实推进", "取得显著成效", "圆满完成", "全面提升", "统筹推进",
        "据介绍", "相关负责人表示", "值得一提的是", "近年来", "持续加强",
        "不断夯实", "有序有力有效", "坚持以", "深入贯彻落实",
    }
    fluff_count = sum(1 for w in _FLUFF_WORDS if w in summary)
    if fluff_count >= 2 and len(summary) < 100:
        score -= fluff_count * 2
    # 具体度加分：有数字/地名/人名的更有新闻价值
    if re.search(r"\d{4}年|\d+月|\d+日|\d+%|\d+\.?\d*亿|\d+\.?\d*万", title + summary):
        score += 3
    if re.search(r"[省市区县]|北京|上海|广州|深圳|杭州|成都|武汉|南京|重庆|天津", title + summary):
        score += 2
    return score


def _titles_too_similar(title1: str, title2: str, min_common: int = 10) -> bool:
    """判断两个标题是否同质：公共前缀 ≥ min_common 字符（中文新闻标题常用写法）。"""
    if not title1 or not title2:
        return False
    min_len = min(len(title1), len(title2))
    common = 0
    for i in range(min_len):
        if title1[i] == title2[i]:
            common += 1
        else:
            break
    return common >= min_common


# 高频通用词 2-gram，在话题去重中排除以避免误杀
_COMMON_BIGRAMS = {
    "中国", "国家", "全国", "国际", "社会", "发展", "经济",
    "市场", "企业", "产业", "科技", "文化", "教育", "医疗",
    "政府", "政策", "改革", "创新", "建设", "管理", "服务",
    "工作", "推进", "推动", "加强", "提升", "促进", "保障",
    "我们", "他们", "这个", "一个", "已经", "正在", "进行",
    "表示", "发布", "最新", "近日", "日前", "正式", "持续",
    "据介绍", "出台", "开展", "实施", "全面", "深入",
}


def _same_topic(title1: str, title2: str, min_overlap: int = 2) -> bool:
    """通过 2-gram 重叠判断两标题是否同话题（事件级去重）。

    例如 "2026年高考语文全国卷作文试题解析" 和 "北大教授谈高考作文天津卷"
    共享 {"高考", "作文"} 两个 2-gram → 判为同话题。
    """
    if not title1 or not title2:
        return False

    def _bigrams(t: str) -> set[str]:
        # 去掉数字、标点、常见前缀
        t = re.sub(r"\d+", "", t)
        # 去除非中文字符（保留中文汉字）
        t = re.sub(r"[^\u4e00-\u9fff]", "", t)
        t = re.sub(r"^(最新|聚焦|关注|走进|原标题)\s*[：:]?", "", t)
        bg = set()
        for i in range(len(t) - 1):
            bg.add(t[i : i + 2])
        return bg - _COMMON_BIGRAMS

    b1 = _bigrams(title1)
    b2 = _bigrams(title2)
    return len(b1 & b2) >= min_overlap


def _reclassify_domestic_articles(articles: list[dict]) -> None:
    """将国际频道里实质关于中国的文章改分到国内。"""
    _patterns = [
        "在中国加速", "在中国成为", "读懂中国", "中国式",
        "中国针灸", "中国援", "中国医疗队",
    ]
    for a in articles:
        if a.get("scope") != "international":
            continue
        text = a.get("title", "") + a.get("raw_content", "")
        if any(p in text for p in _patterns):
            a["scope"] = "domestic"
            a["category"] = "综合资讯"


def _load_previous_articles(output_dir: Path, poster_date: dt.date, days: int = 2) -> list[dict]:
    """加载前 N 天的已推送文章 + 持久化拒绝列表，用于跨日去重。"""
    previous = []
    # 加载前 N 天文章
    for i in range(1, days + 1):
        prev_date = poster_date - dt.timedelta(days=i)
        prev_path = output_dir / f"raw-articles-{prev_date.isoformat()}.json"
        if prev_path.exists():
            try:
                with prev_path.open("r", encoding="utf-8") as f:
                    prev_articles = json.load(f)
                previous.extend(prev_articles)
            except Exception:
                pass
    # 加载持久化拒绝列表
    rejected_path = output_dir / "rejected-articles.json"
    if rejected_path.exists():
        try:
            with rejected_path.open("r", encoding="utf-8") as f:
                rejected = json.load(f)
            previous.extend(rejected)
        except Exception:
            pass
    return previous


def select_articles(
    all_articles: list[dict],
    scope: str,
    target: int,
    previous_articles: list[dict] | None = None,
) -> list[dict]:
    """选出 target 条，含同分类内去重 + 跨日去重（前2天已推送过的跳过）。"""
    candidates = [a for a in all_articles if a.get("scope") == scope]
    if not candidates:
        return []
    for a in candidates:
        a["_score"] = _score_article(a.get("title", ""), a.get("summary", ""))
    candidates.sort(key=lambda x: x["_score"], reverse=True)

    # 最低分数线：先按 ≥0 筛选，高分不足再放宽到 ≥-3
    # （得分≤-3 的文章通常是软文/空话/无实质内容）
    _MIN_SCORE = 0
    before_filter = len(candidates)
    _filtered = [c for c in candidates if c["_score"] >= _MIN_SCORE]
    if len(_filtered) < target:
        print(f"  [选稿] 高分(≥{_MIN_SCORE})文章不足({len(_filtered)}条)，放宽到≥-3")
        _MIN_SCORE = -3
        _filtered = [c for c in candidates if c["_score"] >= _MIN_SCORE]
    candidates = _filtered
    filtered_count = before_filter - len(candidates)
    if filtered_count:
        print(f"  [选稿] 淘汰低分文章 {filtered_count} 条 (得分<{_MIN_SCORE})")

    if not candidates:
        return []

    # 收集已用标题（同分类内已选 + 前2天已推送）
    used_titles: list[str] = []
    if previous_articles:
        used_titles.extend(
            a.get("title", "") for a in previous_articles
            if a.get("scope") == scope and a.get("title")
        )

    selected: list[dict] = []
    _source_counts: dict[str, int] = {}
    _MAX_PER_SOURCE = 3  # 每个来源最多选3条
    for c in candidates:
        # 来源多样性：同一来源最多选 _MAX_PER_SOURCE 条
        _src = c.get("source", "unknown")
        if _source_counts.get(_src, 0) >= _MAX_PER_SOURCE:
            continue
        title = c.get("title", "")
        too_similar = False
        for used in used_titles:
            if _titles_too_similar(title, used) or _same_topic(title, used):
                too_similar = True
                break
        if too_similar:
            continue
        selected.append(c)
        used_titles.append(title)
        _source_counts[_src] = _source_counts.get(_src, 0) + 1
        if len(selected) >= target:
            break
    return selected[:target]


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


def fetch_articles(
    config: dict,
    output_dir: Path | None = None,
    poster_date: dt.date | None = None,
) -> tuple[list[dict], list[str]]:
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

    # 内容修正：国际频道里实质是关于中国的文章，改分到国内
    _reclassify_domestic_articles(all_articles)

    articles: list[dict] = []
    # 跨日去重：加载前 2 天已推送文章
    prev_dir = output_dir or (ROOT / "output")
    prev_date = poster_date or dt.date.today()
    previous = _load_previous_articles(prev_dir, prev_date)
    for scope, target in (
        ("domestic", config.get("domestic_count", 10)),
        ("international", config.get("international_count", 6)),
    ):
        selected = select_articles(all_articles, scope, target, previous)
        # 如果选出的文章不足，补充 fallback（fallback 也要跨日去重）
        if len(selected) < target:
            fallback = [
                a for a in config.get("fallback_articles", [])
                if a.get("scope") == scope
            ]
            existing_titles = {a.get("title") for a in selected}
            # 把前2天的标题也加入排除列表
            existing_titles.update(
                a.get("title", "") for a in previous
                if a.get("scope") == scope and a.get("title")
            )
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
        "{{ADDRESS}}": html.escape(config["brand"].get("address", "")),
        "{{PHONE}}": html.escape(config["brand"].get("phone", "")),
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


def _ai_summarize_one(title: str, text: str, limit: int, api_key: str) -> str:
    """对单条新闻调用 AI 生成摘要，返回摘要字符串（失败返回空字符串）。"""
    import requests as req

    prompt = (
        '你是云嘉律师事务所每日资讯海报的新闻编辑。请为以下新闻写一条摘要。\n'
        f'摘要严格不超过{limit}字，结尾必须是完整句号（禁止用省略号截断）。\n'
        '必须有实质信息量：\n'
        '· 法律相关性优先：涉及法院、检察院、仲裁、监管、合规、诉讼、合同、劳动、消费者权益的内容，必须点明法律要点\n'
        '· 包含关键细节：事件结果、处罚金额、涉及人数、生效时间、具体数据等\n'
        '· 禁止套话开头（"据介绍""相关负责人表示""近年来"等）\n'
        '· 禁止照搬标题，必须补充标题之外的新信息\n'
        '· 禁止空话（"持续推进""有序开展""取得成效"等无实质内容）\n'
        f'· 输出前自检字数，摘要不超过{limit}字——宁可短也要完整\n\n'
        f'标题：{title}\n'
        f'内容：{text}\n\n'
        '请直接输出摘要正文，不要加序号、不要加任何前缀或说明。'
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
                "max_tokens": 500,
                "temperature": 0.1,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return ""
        summary = resp.json()["choices"][0]["message"]["content"].strip()
        # 清理 AI 可能加的前缀
        summary = re.sub(r"^(摘要[：:]\s*)", "", summary)
        summary = summary.rstrip("…。，、；：！？")
        if len(summary) > limit:
            summary = truncate(summary + "。", limit)
        else:
            summary = truncate(summary, limit)
        if not summary.endswith("。"):
            summary += "。"
        return summary if len(summary) >= 40 else ""
    except Exception:
        return ""



def ai_summarize_batch(articles: list[dict], limit: int = 70, api_key: str = "") -> None:
    """用 DeepSeek API 对一批新闻逐条做 AI 摘要（每条独立调用，杜绝串稿）。

    与 summarize() 不同，这个函数让 AI 理解新闻后用自己的话浓缩成 limit 字以内的摘要，
    而不是摘抄原文开头。
    当 RSS 摘要不足以支撑 AI 理解时，会自动从原文 URL 获取完整内容。
    """
    import time

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

    success = 0
    for i, a in enumerate(articles):
        title = a.get("title", "")
        text = a.get("_feed_text", "")
        summary = _ai_summarize_one(title, text, limit, api_key)
        if summary and len(summary) >= 40:
            a["summary"] = summary
            a["_ai_summary"] = True
            success += 1
        if i < len(articles) - 1:
            time.sleep(0.3)  # 避免触发速率限制

    print(f"  [AI摘要] 完成 {success}/{len(articles)} 条")




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
    apply_env_overrides(config)  # CI 环境变量覆盖
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
        # 内容修正：国际频道里实质是关于中国的文章，改分到国内
        _reclassify_domestic_articles(articles)
        # AI 智能摘要（--articles-json 模式也要跑）
        api_key = config.get("deepseek_api_key", "").strip()
        if api_key:
            print("[摘要] AI 智能摘要...")
            for a in articles:
                a["raw_summary"] = a.get("summary", "")
            ai_summarize_batch(articles, limit=70, api_key=api_key)
        else:
            print("[摘要] 无 API key，跳过 AI 摘要")
        # 按分类重选（含分类内去重 + 跨日去重），不足用 fallback 补齐
        previous = _load_previous_articles(output_dir, poster_date)
        selected: list[dict] = []
        for scope, target in (
            ("domestic", config.get("domestic_count", 10)),
            ("international", config.get("international_count", 6)),
        ):
            s = select_articles(articles, scope, target, previous)
            if len(s) < target:
                fallback = [
                    a for a in config.get("fallback_articles", [])
                    if a.get("scope") == scope
                ]
                existing_titles = {a.get("title") for a in s}
                existing_titles.update(
                    a.get("title", "") for a in previous
                    if a.get("scope") == scope and a.get("title")
                )
                for fb in fallback:
                    if fb.get("title") not in existing_titles:
                        s.append(fb)
                    if len(s) >= target:
                        break
            selected.extend(s[:target])
        articles = selected
        print(f"[选稿] 去重后国内={len([a for a in articles if a.get('scope')=='domestic'])} 条，国际={len([a for a in articles if a.get('scope')=='international'])} 条")
    else:
        articles, errors = (
            (fallback_articles(config), [])
            if args.no_fetch
            else fetch_articles(config, output_dir, poster_date)
        )
        # 自动摘要：有 API key 用 AI，没 key 回退规则清洗
        if not args.no_fetch:
            api_key = config.get("deepseek_api_key", "").strip()
            if api_key:
                print("[摘要] AI 智能摘要...")
                for a in articles:
                    a["raw_summary"] = a.get("summary", "")
                ai_summarize_batch(articles, limit=70, api_key=api_key)
                # AI 未覆盖的文章回退到规则清洗
                fallback_count = 0
                for a in articles:
                    if not a.get("_ai_summary"):
                        raw = a.get("raw_content", "") or a.get("raw_summary", "") or a.get("summary", "")
                        improved = summarize(raw=raw, title=a.get("title", ""), limit=65)
                        if improved and len(improved) >= 40:
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
                    if improved and len(improved) >= 40:
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
