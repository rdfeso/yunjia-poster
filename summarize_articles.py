#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 总结 raw-articles → articles
用法: python summarize_articles.py
"""

import json
import os
import sys
import datetime
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"

# ── DeepSeek API ──────────────────────────────────────────────────────
DEEPSEEK_API_KEY = "sk-c42c1ef3963d41678b058ba046482058"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

def call_deepseek(prompt: str, max_tokens: int = 300) -> str:
    import urllib.request
    import urllib.error
    data = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  [DeepSeek错误] {e}")
        return ""

# ── 去电头/署名/引导语 ─────────────────────────────────────────────
STRIP_PATTERNS = [
    r'^中新网\s*\S+\s*电\s*',
    r'^新华社\s*\S+\s*电\s*',
    r'^中新社\s*\S+\s*电\s*',
    r'^本报\S+讯\s*',
    r'^记者\s*\S+\s*',
    r'^央视网消息[：:]\s*',
    r'^据央视网消息\s*',
    r'^据新华社报道\s*',
    r'^据中新网报道\s*',
    r'^[（(]?(中新网|新华社|央视网)[^）)]*[）)]?\s*',
]

def strip_lead(text: str) -> str:
    for pat in STRIP_PATTERNS:
        text = re.sub(pat, '', text, flags=re.MULTILINE)
    return text.strip()

# ── 选题评分 (国内：民生/法律/社会优先) ───────────────────────────
DOMESTIC_KEYWORDS = {
    "消费维权": "+2", "维权": "+1", "霸王条款": "+2", "预付": "+1",
    "劳动权益": "+2", "加班": "+1", "劳动仲裁": "+2", "工伤": "+2",
    "食品安全": "+2", "校园餐": "+2", "预制菜": "+1",
    "法治": "+2", "法院": "+1", "判了": "+2", "违法": "+1",
    "反腐": "+2", "纪委": "+2", "通报": "+1",
    "社会热点": "+1", "安全": "+1", "诈骗": "+2",
    "民生": "+1", "个税": "+1", "老旧小区": "+1",
    # 国内禁止关键词 → 大幅降分
    "外事": "-5", "接待": "-3", "来访": "-3", "政要": "-5",
    "习近平": "-5", "李强": "-5", "人大": "-3", "政协": "-3",
    "调研": "-2", "会议": "-1", "会晤": "-5",
}

INTERNATIONAL_KEYWORDS = {
    "冲突": "+2", "军事": "+2", "战争": "+2", "袭击": "+2",
    "选举": "+2", "大选": "+2",
    "外交": "+1", "制裁": "+2", "谈判": "+1",
    "特朗普": "+1", "美国": "+1", "伊朗": "+2", "中东": "+2",
    "菲律宾": "+1", "韩国": "+1", "日本": "+1", "欧盟": "+1",
    "经贸": "+1", "关税": "+2", "供应链": "+1",
}

def score_article(article: dict) -> int:
    title = article.get("title", "")
    summary = article.get("summary", "")
    category = article.get("category", "")
    scope = article.get("scope", "")
    text = title + " " + summary
    score = 0
    kw_map = DOMESTIC_KEYWORDS if scope == "domestic" else INTERNATIONAL_KEYWORDS
    for kw, val in kw_map.items():
        if kw in text:
            delta = int(val)
            score += delta
    if category in ("法治", "反腐", "消费维权", "劳动权益", "食品安全"):
        score += 2
    return score

# ── 辅助：LLM 判断新闻是否适合国内板块 ──────────────────────────
def is_suitable_domestic(title: str, summary: str) -> bool:
    """用 LLM 快速判断是否适合国内板块（民生/法律/社会）"""
    prompt = f"""请判断以下新闻是否适合放在「国内民生/法律/社会热点」板块（服务于律师事务所资讯海报）。

新闻标题：{title}
新闻摘要：{summary}

适合的条件：与国内民生、消费维权、劳动权益、食品安全、法律案例、反腐、社会热点相关。
不适合的条件：外国政要外事活动、纯时政会议/调研、国际外交/军事/选举。

只回答"适合"或"不适合"，不要解释。"""
    result = call_deepseek(prompt, max_tokens=10)
    return "适合" in result

def is_suitable_international(title: str, summary: str) -> bool:
    """用 LLM 判断新闻是否适合国际板块"""
    prompt = f"""请判断以下新闻是否适合放在「国际时政/外交/军事」板块。

新闻标题：{title}
新闻摘要：{summary}

适合的条件：国际时政、外交、军事冲突、外国选举、大国博弈。
不适合的条件：纯国内民生/法律事件的海外报道。

只回答"适合"或"不适合"，不要解释。"""
    result = call_deepseek(prompt, max_tokens=10)
    return "适合" in result

# ── AI 总结单条新闻 ────────────────────────────────────────────────
def ai_summarize(article: dict) -> dict:
    title = article.get("title", "").strip()
    summary = article.get("summary", "").strip()
    category = article.get("category", "")
    scope = article.get("scope", "")

    # 去掉HTML标签
    summary = re.sub(r'<[^>]+>', '', summary)
    summary = strip_lead(summary)

    prompt = f"""你是一个为律师事务所资讯海报撰写新闻摘要的编辑。

请对以下新闻进行智能总结，生成：
1. 一个精简后的标题（≤32字，保留核心信息，不可截断导致语义不全）
2. 一段充实的摘要（55-70字，必须有信息量，包含关键事实、数据、法律要点或社会意义，不能空洞）

禁忌：
- 不要出现「近期」「日前」「今日」「昨日」等时效锚点词
- 不要有记者署名、电头、引导语
- 不要写成标题的重复或扩展，摘要要有独立信息量

新闻标题：{title}
新闻摘要/正文：{summary}
新闻分类：{category}
新闻板块：{"国内" if scope == "domestic" else "国际"}

请严格按以下格式输出（两行）：
标题：xxx
摘要：xxx"""

    result = call_deepseek(prompt, max_tokens=200)
    new_title = title
    new_summary = summary[:70]

    for line in result.splitlines():
        line = line.strip()
        if line.startswith("标题："):
            t = line[3:].strip()
            if 0 < len(t) <= 32:
                new_title = t
        elif line.startswith("摘要："):
            s = line[3:].strip()
            # 去掉可能的时效词
            s = re.sub(r'(近期|日前|今日|昨日|刚刚|发文|出台|即日起|正式实施|进入关键期)', '', s)
            if 20 <= len(s) <= 70:
                new_summary = s

    # fallback: 如果 AI 没返回合理结果，用规则精简
    if len(new_title) > 32:
        new_title = new_title[:30] + "…"
    if len(new_summary) > 70:
        new_summary = new_summary[:68] + "…"
    # 确保摘要不太短（至少30字），否则用 AI 再补一次
    if len(new_summary) < 30 and summary:
        new_summary = summary[:70].rstrip("…。") + "。"

    return {
        "scope": scope,
        "category": category,
        "source": article.get("source", "中新网"),
        "title": new_title,
        "summary": new_summary,
        "url": article.get("url", ""),
    }

# ── 主流程 ──────────────────────────────────────────────────────────
def main():
    today = datetime.date.today()
    date_str = today.strftime("%Y-%m-%d")
    raw_path = OUTPUT_DIR / f"raw-articles-{date_str}.json"
    out_path = OUTPUT_DIR / f"articles-{date_str}.json"
    config_path = SCRIPT_DIR / "config.json"

    if not raw_path.exists():
        print(f"[错误] 找不到 {raw_path}，请先运行 --fetch-only")
        sys.exit(1)

    with open(raw_path, "r", encoding="utf-8") as f:
        raw_articles = json.load(f)

    print(f"[总结] 共 {len(raw_articles)} 条原始文章，开始 AI 总结...")

    # 读取 config fallback
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    fallback_pool = config.get("fallback_articles", [])

    # 分离国内/国际
    domestic_raw = [a for a in raw_articles if a.get("scope") == "domestic"]
    international_raw = [a for a in raw_articles if a.get("scope") == "international"]

    print(f"  RSS国内: {len(domestic_raw)} 条, 国际: {len(international_raw)} 条")

    # ── 国内：AI 总结 + 筛选 ──
    print("\n[国内] AI 总结...")
    domestic_summarized = []
    for i, art in enumerate(domestic_raw):
        print(f"  [{i+1}/{len(domestic_raw)}] {art['title'][:25]}...")
        summarized = ai_summarize(art)
        # LLM 判断适用性
        if is_suitable_domestic(summarized["title"], summarized["summary"]):
            domestic_summarized.append(summarized)
        else:
            print(f"    → 不适合国内，跳过")

    # 按评分排序
    for a in domestic_summarized:
        a["_score"] = score_article(a)
    domestic_summarized.sort(key=lambda x: x["_score"], reverse=True)

    # 去重（标题相似度）
    seen_titles = set()
    domestic_deduped = []
    for a in domestic_summarized:
        t_key = a["title"][:10]  # 前10字去重
        if t_key not in seen_titles:
            seen_titles.add(t_key)
            domestic_deduped.append(a)

    # 取前10，不够补 fallback
    domestic_final = domestic_deduped[:10]
    if len(domestic_final) < 10:
        need = 10 - len(domestic_final)
        print(f"  [补充] 国内缺少 {need} 条，从 fallback 池补充...")
        fallback_dom = [a for a in fallback_pool if a.get("scope") == "domestic"]
        existing_titles = {a["title"] for a in domestic_final}
        for fb in fallback_dom:
            if fb["title"] not in existing_titles and need > 0:
                domestic_final.append({
                    "scope": fb["scope"],
                    "category": fb["category"],
                    "source": fb["source"],
                    "title": fb["title"],
                    "summary": fb["summary"],
                    "url": "",
                })
                existing_titles.add(fb["title"])
                need -= 1

    # ── 国际：AI 总结 + 筛选 ──
    print("\n[国际] AI 总结...")
    international_summarized = []
    for i, art in enumerate(international_raw):
        print(f"  [{i+1}/{len(international_raw)}] {art['title'][:25]}...")
        summarized = ai_summarize(art)
        if is_suitable_international(summarized["title"], summarized["summary"]):
            international_summarized.append(summarized)
        else:
            print(f"    → 不适合国际，跳过")

    for a in international_summarized:
        a["_score"] = score_article(a)
    international_summarized.sort(key=lambda x: x["_score"], reverse=True)

    seen_titles_intl = set()
    international_deduped = []
    for a in international_summarized:
        t_key = a["title"][:10]
        if t_key not in seen_titles_intl:
            seen_titles_intl.add(t_key)
            international_deduped.append(a)

    international_final = international_deduped[:6]
    if len(international_final) < 6:
        need = 6 - len(international_final)
        print(f"  [补充] 国际缺少 {need} 条，从 fallback 池补充...")
        fallback_intl = [a for a in fallback_pool if a.get("scope") == "international"]
        existing_titles = {a["title"] for a in international_final}
        for fb in fallback_intl:
            if fb["title"] not in existing_titles and need > 0:
                international_final.append({
                    "scope": fb["scope"],
                    "category": fb["category"],
                    "source": fb["source"],
                    "title": fb["title"],
                    "summary": fb["summary"],
                    "url": "",
                })
                existing_titles.add(fb["title"])
                need -= 1

    # 合并输出
    final_articles = domestic_final[:10] + international_final[:6]

    # 清理 _score 字段
    for a in final_articles:
        a.pop("_score", None)

    print(f"\n[完成] 国内 {len(domestic_final[:10])} 条 + 国际 {len(international_final[:6])} 条")
    print(f"[写入] {out_path}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(final_articles, f, ensure_ascii=False, indent=2)

    # 打印预览
    print("\n═══ 预览 ═══")
    for i, a in enumerate(final_articles):
        print(f"[{i+1}] [{a['scope']}][{a['category']}] {a['title']}")
        print(f"    → {a['summary']}")

if __name__ == "__main__":
    main()
