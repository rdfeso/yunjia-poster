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

from html.parser import HTMLParser

from pathlib import Path



import requests



ROOT = Path(__file__).resolve().parent

DATA_DIR = ROOT / "data"  # 去重数据目录（不被 gitignore，GitHub Actions 可用）

DATA_DIR.mkdir(exist_ok=True)



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



    # 0. 如果原始内容就是垃圾（版权声明、时间戳列表、标题重复等），直接放弃

    if _is_garbage_content(text, title):

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



    # 改进：不选第一句，而是给所有句子打分，选信息量最高的1-2句

    import re as _re

    sents = [s.strip() for s in _re.split(r"(?<=[。！？])", text) if s.strip()]



    def _score_sent(s: str) -> int:

        sc = 0

        # 包含数字/金额/百分比 → 高信息量（权重最高）

        if _re.search(r"\d+[万余亿千佰拾%\.\d]|第\d+|[0-9]", s):

            sc += 6

        # 包含法律关键词

        if any(kw in s for kw in ["法院", "判决", "检察", "起诉", "处罚", "违法", "赔偿", "仲裁", "诉讼", "合规"]):

            sc += 3

        # 包含地名（2-4个中文字符 + 省/市/区/县/镇）

        if _re.search(r"[\u4e00-\u9fff]{2,4}[省市区县镇村]", s):

            sc += 2

        # 句子长度适中（15-80字）得分高

        L = len(s)

        if 15 <= L <= 80:

            sc += 2

        elif L < 10:

            sc -= 3

        # 包含实质动词（判决、逮捕、通报、查处、起诉）→ 有实质动作

        if any(vb in s for vb in ["判决", "逮捕", "通报", "查处", "起诉", "责令", "罚款", "拘留", "冻结", "查封"]):

            sc += 2

        # 惩罚：包含套话/空话

        if any(kw in s for kw in ["聚焦", "助力", "备受关注", "隆重举行", "顺利举办", "圆满落幕"]):

            sc -= 4

        return sc



    scored = [(s, _score_sent(s)) for s in sents if len(s) >= 8]

    scored.sort(key=lambda x: x[1], reverse=True)



    # 选前1-2句，总字数不超过 limit

    summary_parts = []

    total = 0

    for s, sc in scored:

        if total + len(s) <= limit:

            summary_parts.append(s)

            total += len(s)

        if len(summary_parts) >= 2:

            break

    if summary_parts:

        candidate = "".join(summary_parts)

        if len(candidate) >= 20:

            return candidate[:limit]



    # 所有句子都不行，降级：取最长且有数字的句子

    for s in sorted(sents, key=len, reverse=True):

        if len(s) >= 20 and total + len(s) <= limit:

            return s[:limit]



    # 彻底失败：返回清洗后文本的前 limit 字

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





def _score_article(title: str, summary: str, source: str = "", article: dict | None = None) -> int:

    """对文章打分，分数越高越优先选中。"""

    text = title + summary

    score = 0



    # 空摘要处理：

    # - 网页源文章（网易/新浪/搜狐等）：不扣分，后续 _enrich_article_content 会补充正文

    # - 中新网 RSS 空摘要：轻降权 -5

    # - 垃圾内容（版权声明/标题重复等）：重降权 -50

    is_web_source = source and source not in ("中新网", "人民网", "新华日报")

    if not summary or len(summary.strip()) < 5:

        if not is_web_source:

            score -= 5

    elif _is_garbage_content(summary, title):

        score -= 50

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

                         "票房", "夺冠", "首金", "开幕式", "闭幕式", "颁奖典礼", "编剧", "豆瓣",

                         "韩剧", "饭圈", "追星", "偶像剧", "综艺节目", "钟美美"]

    for w in _ENTERTAINMENT_KW:

        if w in title:

            score -= 10

            break

    # 软新闻/正能量宣传降权（无实质新闻价值）

    _SOFT_NEWS_KW = ["点赞", "研修班", "体验", "喜讯", "佳话", "暖心", "感动", "成功举办",

                     "圆满落幕", "圆满结束", "顺利举行", "幸福感", "获得感", "安全感", "蓬勃发展",

                     "谱写新篇", "昂扬奋进", "砥砺前行", "致敬", "感恩", "加油",

                     "涂层剥落", "倒影池", "纪念堂", "世界之最", "奇闻",

                     # 主题宣传/活动报道（无实质法律价值）

                     "主题宣传", "主题活动", "文化季", "宣传周", "启动仪式", "开幕仪式",

                     "完美收官", "盛大启幕", "火热进行", "顺利召开", "胜利召开",

                     ]

    for w in _SOFT_NEWS_KW:

        if w in title:

            score -= 8

            break

    # 体育新闻降权（律师所受众不需要）

    _SPORTS_KW = ["男篮", "女足", "排球", "乒乓球", "羽毛球", "游泳", "跳水", "体操",

                 "田径", "马拉松", "奥运", "世界杯", "亚运", "亚冠", "联赛", "中超",

                 "CBA", "NBA", "英超", "西甲", "德甲", "意甲", "欧冠", "总决赛",

                 "冠军", "夺冠", "晋级", "出线", "集训", "热身赛", "友谊赛",

                 "大名单", "首发", "替补", "球员", "教练", "主帅", "国家队",

                 ]

    for w in _SPORTS_KW:

        if w in title:

            score -= 12

            break

    # 标题党/段子/轻松一刻类降权

    _SPAM_TITLE_KW = ["一刻", "神回复", "段子", "糗事", "搞笑", "幽默", "笑死", "笑喷",

                      "笑哭", "整蛊", "恶搞", "放屁", "被坑", "活该", "笑翻了",

                      "段子手", "神评论", "沙雕", "社死", "破防了",

                      ]

    for w in _SPAM_TITLE_KW:

        if w in title:

            score -= 15

            break

    # 富化失败的网页源（SPA/slide/无内容）：大幅扣分，确保不会进入选稿

    if article and article.get("_enrich_failed"):

        score -= 30

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

        # 旅游产业动态（不是法律/民生话题）

        "旅游公路", "交旅融合", "旅游品牌", "全域旅游", "乡村旅游",

    ]

    for w in _HOLIDAY_SOFT_KW:

        if w in title:

            score -= 10

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

    # ====== 2026-07-03 新增 3 个护栏 ======

    # 1) 套壳 fallback 检测：标题本身是空话/主题词而非真实事件

    #    例：「消费者权益典型案例发布 霸王条款被集中曝光」——这是 AI 把主题词当成标题的伪稿

    _SHELL_TITLE_PATTERNS = [

        re.compile(r"^[\u4e00-\u9fff]{2,8}(典型案例|集中曝光|发布|通报|持续|引关注|频发|增多|成常态|渐成|观察|解读|分析|趋势)$"),

        re.compile(r"^(聚焦|关注|透视|解读)\S{4,12}$"),

    ]

    for pat in _SHELL_TITLE_PATTERNS:

        if pat.match(title):

            score -= 25

            break

    # 2) 同事件跨源检测：抓取到的 raw_content 里如果含"另据""此前""同一天"等转载标志词 + 与已选稿摘要里含相同地名/数字特征

    #    简化版：raw_content 或 _feed_text 含 "另据" "转载" "据中新社" 视为转载

    if article:

        feed = article.get("_feed_text", "") or article.get("raw_content", "")

        if re.search(r"^.{0,20}(另据|据中新社|新华社.*?电|本网综合|综合.*?报道)", feed[:80]):

            # 转载稿只给一次性入选机会（同事件其他转载稿会被 _same_topic 拦下）

            score -= 3

    # 3) 政府自纠/正面公关稿检测：「审核未通过」「主动整改」「及时回应」等词表

    #    ——这类稿子对律所无传播价值，应被替换为真正的批评性报道

    _GOV_PR_PATTERNS = [

        "已追责整改", "审核未通过", "已严肃处理", "已妥善处理", "主动回应",

        "及时回应", "积极回应", "回应关切", "高度重视", "已成立.*?专班",

    ]

    gov_pr_hits = sum(1 for p in _GOV_PR_PATTERNS if p in title or p in summary)

    if gov_pr_hits >= 1 and len(summary) < 80:

        # 短摘要 + 公关词 → 大幅降权（这类稿子说了等于没说）

        score -= 15

    # ====== 硬新闻指标（白名单）======
    # 核心修复：不再只靠黑名单堵，而是要求每条新闻必须包含至少1个"硬新闻动作"
    # 没有硬新闻指标的文章 → 直接 -15 分（低于 -3 阈值，自动淘汰）
    _HARD_NEWS_INDICATORS = [
        # 司法/执法动作
        "判决", "裁定", "逮捕", "起诉", "查处", "通报", "罚款", "拘留",
        "冻结", "查封", "赔偿", "退赔", "兑付", "追缴", "吊销", "撤销",
        "驳回", "认定", "宣判", "终审", "立案", "侦查", "约谈", "召回",
        "下架", "停业", "整顿", "判处", "羁押", "批捕", "抗诉", "再审",
        # 事故/灾难
        "事故", "坍塌", "爆炸", "火灾", "坠毁", "泄露", "中毒", "伤亡",
        "死亡", "牺牲", "身亡", "遇难", "遇难", "受伤", "被困", "失联",
        # 调查/曝光
        "调查", "曝光", "举报", "投诉", "查处", "涉嫌", "违法犯罪",
        "造假", "售假", "骗保", "欠薪", "克扣", "欺诈", "违约",
        # 政策发布（有具体措施的）
        "发布", "出台", "印发", "施行", "生效",
        # 处罚/执行
        "处罚", "没收", "罚没", "取缔", "关停", "拆除",
    ]
    _has_hard_news = any(ind in text for ind in _HARD_NEWS_INDICATORS)
    if not _has_hard_news:
        score -= 15  # 没有硬新闻指标 → 直接低分淘汰

    # 标题党/点击诱饵 → 硬性 -20（不是 -5/-8 的软降权）
    _CLICKBAIT_PATTERNS = [
        "看过来", "谁还能", "你知道吗", "告诉你", "震惊", "竟然", "居然",
        "难以置信", "不可思", "竟然是", "原来是", "万万没想到", "看完惊",
        "速看", "急转", "扩散", "别再", "别怪", "注意了",
        "？$",  # 以问号结尾的标题（多数是标题党）
    ]
    for pat in _CLICKBAIT_PATTERNS:
        if re.search(pat, title):
            score -= 20
            break

    # 分析/评论/趋势文 → 硬性 -20
    _ANALYSIS_PATTERNS = [
        "背后有何", "有何深意", "折射出", "新图景", "新趋势", "新格局",
        "加速上新", "勾勒", "描绘", "解读", "述评", "观察", "时评",
        "背后的", "意味着什么", "说明了什么", "怎么看", "怎么选",
        "释放什么信号", "传递什么", "何去何从", "路在何方",
    ]
    for pat in _ANALYSIS_PATTERNS:
        if pat in title:
            score -= 20
            break

    # 观点引用/论坛发言标题 → -15（"某某：不应/应/需..." 或 "出席论坛时称"）
    if re.search(r'^.{2,10}[:：].{0,5}(不应|应该|需要|必须|呼吁|建议|认为|表示)', title):
        score -= 15
    if "出席论坛" in title or "在.*论坛" in title or "论坛时" in (title + summary):
        score -= 10

    # 空泛政策标题（无具体措施的官样文章）→ -15
    _VAGUE_POLICY_PATTERNS = [
        "加快能源", "增加.*使用", "推进.*建设", "加强.*管理",
        "深化.*改革", "优化.*结构", "提升.*水平", "推动.*发展",
    ]
    for pat in _VAGUE_POLICY_PATTERNS:
        if re.search(pat, title):
            score -= 15
            break

    # 具体度加分：有数字/地名/人名的更有新闻价值
    # 扩展数字匹配：年份、金额、百分比、数量、日期等
    _has_digit = re.search(
        r'\d{4}年|\d+月\d+日?|\d+[年月日号]|'
        r'\d+\.?\d*[亿万千百]|\d+[余元]|'
        r'\d+%|\d+\.\d+%|'
        r'\d+[名例起件人次]|'
        r'\d{1,2}时\d{0,2}分?',
        title + summary
    )
    if _has_digit:
        score += 3

    # 地名加分（具体地点 = 更有新闻价值）

    if re.search(r"[省市区县]|北京|上海|广州|深圳|杭州|成都|武汉|南京|重庆|天津|上海|天津|重庆|哈尔滨|沈阳|西安|郑州|合肥", title + summary):

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





# 事件级指纹：从标题+正文里提取【地名前缀 + 2个核心名词】，用于跨源转载去重

# 例：「青海乐都发布通报：司机载乘14名村民在机耕道路上发生翻坠事故」

#     vs 「青海海东皮卡搭载十多名女工坠翻 8人身亡 家属发声」

#     → 都能抽出 "青海" + {"坠", "翻"} → 同事件

def _event_fingerprint(title: str, body: str = "") -> str:

    text = (title + " " + body)[:300]

    if not text:

        return ""

    # 提取所有连续2字中文

    import re as _re

    bigrams = _re.findall(r"[\u4e00-\u9fff]{2}", text)

    if not bigrams:

        return ""

    # 第一个非高频 bigram 视为"地名前缀"

    location_hint = ""

    for bg in bigrams[:3]:

        if bg not in _COMMON_BIGRAMS and bg not in {"事故", "通报", "回应", "声明", "官方", "披露", "报道"}:

            location_hint = bg

            break

    if not location_hint:

        return ""

    # 关键动词/名词（去除停用词后取前2个）

    _stop = _COMMON_BIGRAMS | {"新闻", "报道", "事件", "问题", "情况", "工作", "有关", "进行", "目前", "已经", "我们", "表示"}

    key_terms = [bg for bg in bigrams if bg not in _stop][:3]

    return f"{location_hint}|{'|'.join(key_terms[:2])}"





def _load_previous_articles(output_dir: Path, poster_date: dt.date, days: int = 7) -> list[dict]:

    """加载前 N 天的已推送文章 + 持久化拒绝列表，用于跨日去重。

    同时加载 published-articles-*.json（含 fallback 文章），确保 fallback 也参与跨日去重。

    所有去重文件存放在 DATA_DIR（不被 gitignore，GitHub Actions 可用）。

    """

    previous = []

    # 加载前 N 天文章（raw-articles = RSS/网页抓取的原始文章）

    for i in range(1, days + 1):

        prev_date = poster_date - dt.timedelta(days=i)

        prev_path = DATA_DIR / f"raw-articles-{prev_date.isoformat()}.json"

        if prev_path.exists():

            try:

                with prev_path.open("r", encoding="utf-8") as f:

                    prev_articles = json.load(f)

                previous.extend(prev_articles)

            except Exception:

                pass

    # 加载前 N 天的 published-articles（最终发布的文章，包含 fallback）

    for i in range(1, days + 1):

        prev_date = poster_date - dt.timedelta(days=i)

        pub_path = DATA_DIR / f"published-articles-{prev_date.isoformat()}.json"

        if pub_path.exists():

            try:

                with pub_path.open("r", encoding="utf-8") as f:

                    pub_articles = json.load(f)

                previous.extend(pub_articles)

            except Exception:

                pass

    # 加载持久化拒绝列表

    rejected_path = DATA_DIR / "rejected-articles.json"

    if rejected_path.exists():

        try:

            with rejected_path.open("r", encoding="utf-8") as f:

                rejected = json.load(f)

            previous.extend(rejected)

        except Exception:

            pass

    return previous





def _save_published_articles(output_dir: Path, poster_date: dt.date, articles: list[dict]) -> None:

    """保存最终发布的文章列表（含 fallback），供跨日去重使用。"""

    path = DATA_DIR / f"published-articles-{poster_date.isoformat()}.json"

    try:

        with path.open("w", encoding="utf-8") as f:

            json.dump(articles, f, ensure_ascii=False, indent=2)

        print(f"[发布] 保存 {len(articles)} 条已发布文章到 {path}")

    except Exception as e:

        print(f"[发布] 保存 published-articles 失败: {e}")





def _load_used_fallbacks(output_dir: Path) -> dict:

    """加载持久化的 fallback 使用记录，返回 {title: date_str} 字典。"""

    path = DATA_DIR / "used-fallback-articles.json"

    if path.exists():

        try:

            with path.open("r", encoding="utf-8") as f:

                return json.load(f)

        except Exception:

            pass

    return {}





def _save_used_fallbacks(output_dir: Path, used: dict) -> None:

    """保存 fallback 使用记录到磁盘。"""

    path = DATA_DIR / "used-fallback-articles.json"

    try:

        with path.open("w", encoding="utf-8") as f:

            json.dump(used, f, ensure_ascii=False, indent=2)

    except Exception:

        pass






def _is_stale_article(article: dict, poster_date: dt.date, max_days: int = 3) -> bool:
    """检查文章是否过期：标题或摘要中提到的日期距海报日期超过 max_days 天则为过期。"""
    import re as _re_stale
    text = article.get("title", "") + article.get("summary", "")
    # 匹配 X月X日 格式
    dates = _re_stale.findall(r'(\d{1,2})月(\d{1,2})日', text)
    for month, day in dates:
        try:
            # 用海报年份
            article_date = dt.date(poster_date.year, int(month), int(day))
            delta = (poster_date - article_date).days
            # 允许未来日期（如政策生效日）和近期日期
            # 但如果是过去的且超过 max_days 天，判为过期
            if 0 < delta <= 400:  # 排除跨年误判
                if delta >= max_days:
                    return True
        except ValueError:
            continue
    # 匹配"6月"这种只提到月份的（安全生产月等回顾性内容）
    month_only = _re_stale.findall(r'(\d{1,2})月(?:全国|期间|以来)', text)
    for month in month_only:
        try:
            m = int(month)
            if m < poster_date.month and (poster_date.month - m) >= 1:
                return True
        except ValueError:
            continue
    return False


def _pick_fallback(

    config: dict,

    scope: str,

    used_titles: set,

    used_fallbacks: dict,

    poster_date: dt.date,

    cooldown_days: int = 14,

    hard_min_days: int = 3,

) -> dict | None:

    """从 fallback 池中选一条未在冷却期内使用过的文章。

    优先选冷却期外的；若全部在冷却期内，退化为选最久未使用的（避免条数不足）。

    但硬性禁止选用 hard_min_days 天内使用过的（防止短期重复）。

    """

    fallback_pool = [

        fb for fb in config.get("fallback_articles", [])

        if fb.get("scope") == scope

        and _has_valid_summary(fb.get("summary", ""), fb.get("title", ""))

    ]

    if not fallback_pool:

        print("  [fallback] 警告: 所有 fallback 未通过质量校验，池为空")

    # 第一轮：找冷却期外的

    oldest_fb = None

    oldest_days = -1

    for fb in fallback_pool:

        title = fb.get("title", "")

        if title in used_titles:

            continue

        last_used = used_fallbacks.get(title)

        if not last_used:

            # 从未用过，最高优先

            used_fallbacks[title] = poster_date.isoformat()

            return fb

        try:

            last_date = dt.date.fromisoformat(last_used)

            days_since = (poster_date - last_date).days

            if days_since >= cooldown_days:

                used_fallbacks[title] = poster_date.isoformat()

                return fb

            # 硬性禁止：hard_min_days 天内用过的，不作为后备

            if days_since < hard_min_days:

                continue

            # 记录最久未用的，作为后备

            if days_since > oldest_days:

                oldest_days = days_since

                oldest_fb = fb

        except Exception:

            # 日期解析失败，当作未用过

            used_fallbacks[title] = poster_date.isoformat()

            return fb

    # 第二轮：全部在冷却期内，选最久未用的（有总比没有好）

    # 但已经在上面用 hard_min_days 过滤了，所以 oldest_fb 至少是 hard_min_days 天前的

    if oldest_fb:

        title = oldest_fb.get("title", "")

        used_fallbacks[title] = poster_date.isoformat()

        return oldest_fb

    # 所有 fallback 都在 hard_min_days 内用过，或者全在 used_titles 中

    # 最后兜底：忽略 hard_min_days，选最久未用的（宁可有重复也不能空）

    for fb in fallback_pool:

        title = fb.get("title", "")

        if title in used_titles:

            continue

        last_used = used_fallbacks.get(title)

        if not last_used:

            used_fallbacks[title] = poster_date.isoformat()

            return fb

        try:

            last_date = dt.date.fromisoformat(last_used)

            days_since = (poster_date - last_date).days

            if days_since > oldest_days:

                oldest_days = days_since

                oldest_fb = fb

        except Exception:

            used_fallbacks[title] = poster_date.isoformat()

            return fb

    if oldest_fb:

        title = oldest_fb.get("title", "")

        used_fallbacks[title] = poster_date.isoformat()

        return oldest_fb

    return None





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

        a["_score"] = _score_article(a.get("title", ""), a.get("summary", ""), a.get("source", ""), article=a)

    candidates.sort(key=lambda x: x["_score"], reverse=True)



    # 最低分数线：≥0 才入选，不再放宽到 -3
    # （得分<0 的文章通常是软文/空话/无硬新闻/标题党，不应入选）
    _MIN_SCORE = 0
    before_filter = len(candidates)
    _filtered = [c for c in candidates if c["_score"] >= _MIN_SCORE]
    if len(_filtered) < target:
        print(f"  [选稿] 高分(≥{_MIN_SCORE})文章不足({len(_filtered)}条)，用 fallback 补齐")
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

    _MAX_PER_COUNTRY = 1  # 国际新闻每个国家最多选1条（避免同一国家霸版）

    _COUNTRY_KEYWORDS = {

        "英国": ["英国", "伦敦", "英镑", "BBC"],

        "美国": ["美国", "华盛顿", "纽约", "美元", "白宫", "国会", "美联储"],

        "日本": ["日本", "东京", "日元", "安倍", "岸田"],

        "韩国": ["韩国", "首尔", "韩元"],

        "俄罗斯": ["俄罗斯", "莫斯科", "卢布", "普京"],

        "法国": ["法国", "巴黎", "马克龙"],

        "德国": ["德国", "柏林", "朔尔茨"],

        "印度": ["印度", "新德里", "莫迪"],

        "泰国": ["泰国", "曼谷"],

        "伊朗": ["伊朗", "德黑兰"],

        "以色列": ["以色列", "特拉维夫"],

        "欧盟": ["欧盟", "布鲁塞尔"],

    }

    # 事件级指纹集合：已选文章的"事件关键词"（地名前缀+核心名词），同事件跨源转载只选分数最高的那条

    _event_keys: set[str] = set()

    _used_countries: set[str] = set()

    for c in candidates:

        # 来源多样性：同一来源最多选 _MAX_PER_SOURCE 条

        _src = c.get("source", "unknown")

        if _source_counts.get(_src, 0) >= _MAX_PER_SOURCE:

            continue

        title = c.get("title", "")

        # 计算本条的事件指纹（粗略：地名前缀 + 头4个非高频字）

        ev_key = _event_fingerprint(title, c.get("_feed_text", "") or c.get("raw_content", ""))

        if ev_key and ev_key in _event_keys:

            continue

        # 国家多样性检查（仅国际）
        if scope == "international":
            _country = None
            for country, kws in _COUNTRY_KEYWORDS.items():
                if any(kw in title for kw in kws):
                    _country = country
                    break
            if _country and _country in _used_countries:
                continue
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

        # 记录已用国家
        if scope == "international":
            for country, kws in _COUNTRY_KEYWORDS.items():
                if any(kw in title for kw in kws):
                    _used_countries.add(country)
                    break

        if ev_key:

            _event_keys.add(ev_key)

        if len(selected) >= target:

            break

    return selected[:target]





# ---------------------------------------------------------------------------

# 网页源抓取（门户新闻首页）

# ---------------------------------------------------------------------------



class _WebLinkExtractor(HTMLParser):

    """从 HTML 中提取 <a> 标签的文本和 href。"""



    def __init__(self):

        super().__init__()

        self.links: list[tuple[str, str]] = []

        self._current_href: str | None = None

        self._current_text: list[str] = []



    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:

        if tag == "a":

            attr_dict = {k: v for k, v in attrs}

            self._current_href = attr_dict.get("href", "")

            self._current_text = []



    def handle_endtag(self, tag: str) -> None:

        if tag == "a" and self._current_href:

            text = "".join(self._current_text).strip()

            if text:

                self.links.append((self._current_href, text))

            self._current_href = None

            self._current_text = []



    def handle_data(self, data: str) -> None:

        if self._current_href is not None:

            self._current_text.append(data)





def _normalize_web_url(url: str, base_url: str) -> str:

    """补全相对路径并去掉常见跟踪参数。"""

    from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode



    if not url:

        return ""

    # 协议相对路径

    if url.startswith("//"):

        url = "https:" + url

    elif url.startswith("/") or not url.startswith("http"):

        url = urljoin(base_url, url)



    # 去掉常见跟踪参数（保留必要查询参数）

    parsed = urlparse(url)

    keep = {"id", "docid", "aid", "article_id"}

    query_params = parse_qsl(parsed.query)

    query = [(k, v) for k, v in query_params if k.lower() in keep]

    query_str = urlencode(query) if query else ""

    url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query_str, parsed.fragment))

    return url





# 不同门户的新闻详情页 URL 特征（满足任一即视为新闻）

_WEB_NEWS_URL_PATTERNS = [

    r"\.shtml$",

    r"\.html$",

    r"/article/\w+",

    r"/a/\d+_\d+",

    r"/a/\d+\?",

    r"/doc-[a-z0-9]+\.shtml",

    r"/detail-[a-z0-9_]+\.d",

    r"/news/article/\w+",

]



# 明显非新闻路径/域名，直接排除

_WEB_SKIP_URL_PATTERNS = [

    r"/search[/?]",

    r"/login[/?]",

    r"/register[/?]",

    r"/video[/?]",

    r"/photo[/?]",

    r"/slide[/?]",

    r"/album[/?]",

    r"/promotion[/?]",

    r"/track[/?]",

    r"track\.sohu\.com",

    r"s\.weibo\.com",

    r"open\.163\.com",

    r"/special[/?]",

    r"/gov[/?]",

    r"/zx[/?]",

    r"/data[/?]",

    r"/dy[/?]",  # 网易自媒体号

    r"/caozhi[/?]",  # 网易槽值

    r"/renjian[/?]",  # 网易人间

    r"/jiankang[/?]",  # 网易健康

]





def _is_news_url(url: str) -> bool:

    """判断 URL 是否像新闻详情页。"""

    if not url or not url.startswith("http"):

        return False

    for p in _WEB_SKIP_URL_PATTERNS:

        if re.search(p, url, re.I):

            return False

    return any(re.search(p, url, re.I) for p in _WEB_NEWS_URL_PATTERNS)





# 标题过滤：排除明显非新闻、导航、广告、娱乐八卦、政治评论号

_WEB_INVALID_TITLE_PATTERNS = [

    "点击查看", "查看更多", "更多推荐", "更多新闻", "登录", "注册", "下载",

    "订阅", "关注", "专题", "首页", "频道", "导航", "搜索", "返回", "顶部",

    "评论", "分享", "收藏", "举报", "我来说两句", "点击下载", "APP",

    "豆瓣", "小红书", "抖音", "快手", "饭圈", "追星", "偶像剧", "钧正平",

]





def _is_valid_news_title(title: str) -> bool:

    if not title:

        return False

    title = title.strip()

    if len(title) < 10 or len(title) > 80:

        return False

    if any(w in title for w in _WEB_INVALID_TITLE_PATTERNS):

        return False

    return True





def _classify_by_title(title: str, default_scope: str, default_category: str) -> tuple[str, str]:

    """根据标题关键词自动识别国内/国际。"""

    intl_keywords = [

        "国际", "美国", "俄罗斯", "乌克兰", "以色列", "伊朗", "朝鲜", "韩国",

        "日本", "欧盟", "北约", "中东", "亚太", "非盟", "拉美", "越南", "印度",

        "巴以", "俄乌", "美以", "特朗普", "拜登", "普京", "泽连斯基", "内塔尼亚胡",

        "巴基斯坦", "巴方", "奎达", "伊斯兰堡", "哈梅内伊", "德黑兰", "沙特",

        "阿联酋", "土耳其", "叙利亚", "阿富汗", "缅甸", "菲律宾", "新加坡",

        "金正恩", "尹锡悦", "岸田", "马克龙", "朔尔茨", "莫迪", "埃尔多安",

    ]

    if any(k in title for k in intl_keywords):

        return "international", "国际"

    return default_scope, default_category





def fetch_web_source(source: dict, timeout: int = 10) -> list[dict]:

    """抓取一个门户首页，返回新闻候选列表。



    只抓取标题和链接，正文由后续 _enrich_article_content 从 URL 补充。

    """

    import requests as req



    base_url = source["url"]

    resp = req.get(base_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=timeout)

    resp.encoding = resp.apparent_encoding or "utf-8"

    html = resp.text



    extractor = _WebLinkExtractor()

    extractor.feed(html)



    articles = []

    seen = set()

    default_scope = source.get("scope", "domestic")

    default_category = source.get("category", "综合资讯")

    source_name = source.get("name", "网络")

    limit = source.get("limit", 20)



    for href, title in extractor.links:

        href = _normalize_web_url(href, base_url)

        if not _is_news_url(href):

            continue

        if not _is_valid_news_title(title):

            continue

        if title in seen:

            continue

        seen.add(title)



        clean_title = strip_tags(title)

        clean_title = re.sub(r"\s+", " ", clean_title)



        scope, category = _classify_by_title(clean_title, default_scope, default_category)

        articles.append({

            "scope": scope,

            "category": category,

            "source": source_name,

            "title": truncate(clean_title, 36),

            "raw_content": "",  # 由 _enrich_article_content 从 URL 补充

            "summary": "",

            "url": href,

        })

        if len(articles) >= limit:

            break

    return articles





def fetch_web_sources(config: dict, timeout: int = 10) -> tuple[list[dict], list[str]]:

    """抓取所有网页源。"""

    all_articles: list[dict] = []

    errors: list[str] = []

    for source in config.get("web_sources", []):

        try:

            arts = fetch_web_source(source, timeout=timeout)

            all_articles.extend(arts)

            print(f"  [网页抓取] {source.get('name', '未知')} 抓到 {len(arts)} 条")

        except Exception as exc:

            errors.append(f'{source.get("name", "未知网页源")}: {exc}')

    return all_articles, errors





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

    """抓取所有源（RSS + 网页），按优先级评分后选出目标数量文章。"""

    all_articles: list[dict] = []

    errors: list[str] = []



    # 1. RSS 源

    for source in config.get("sources", []):

        try:

            all_articles.extend(fetch_feed(source))

        except Exception as exc:

            errors.append(f'{source.get("name", "未知来源")}: {exc}')



    # 2. 网页源（门户首页）

    web_articles, web_errors = fetch_web_sources(config)

    all_articles.extend(web_articles)

    errors.extend(web_errors)



    # 去重：同一标题只保留第一条（不同来源可能抓到相同文章）

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

        # 如果选出的文章不足，补充 fallback（fallback 也要跨日去重 + 持久化去重）

        if len(selected) < target:

            used_fb = _load_used_fallbacks(prev_dir)

            # existing_titles 不应包含前 N 天的 fallback 标题（由 used_fallbacks cooldown 管理）

            fb_titles_in_config = {fb.get("title", "") for fb in config.get("fallback_articles", [])}

            existing_titles = {a.get("title") for a in selected}

            existing_titles.update(

                a.get("title", "") for a in previous

                if a.get("scope") == scope

                and a.get("title")

                and a.get("title") not in fb_titles_in_config

            )

            while len(selected) < target:

                fb = _pick_fallback(config, scope, existing_titles, used_fb, prev_date)

                if fb:

                    selected.append(fb)

                    existing_titles.add(fb.get("title", ""))

                else:

                    break  # fallback 池已耗尽

            _save_used_fallbacks(prev_dir, used_fb)

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



def _has_valid_summary(summary: str, title: str = "") -> bool:

    """判断摘要是否有效（非空、非垃圾、非单纯标题重复、包含具体信息点）。



    硬性要求：必须包含至少1个【数据/地点/时间/具体机构名/具体人名/法律动作】。

    套话稿（"加强/推进/开展/取得成效"+ 无任何具体信息）一律判 false。

    """

    if not summary:

        return False

    s = summary.strip()

    if len(s) < 15:

        return False

    if _is_garbage_content(s, title):

        return False

    # 摘要只是标题的重复/截取，没有新增信息

    # 注意：标题在摘要开头是合法写法（标题+补充信息），只判"摘要完全等于标题"或"标题占摘要>80%"为无效

    if title:

        if s == title:

            return False

        # 标题在末尾且占摘要>70% → 视为截取

        if s.endswith(title) and len(title) / len(s) > 0.7:

            return False



    # === 新增：信息密度硬性校验（防套壳稿/伪稿）===

    import re as _re_qc

    # 1. 数字：金额/数量/日期/百分比/年龄/编号 等具体数字

    has_digit = bool(_re_qc.search(

        r"\d+([\.，,、年月日万亿千百%％]|[元角分秒]|[岁起人名个条项款位]|[A-Za-z\u4e00-\u9fff]|$)",

        s,

    ))

    # 2. 地名：省/市/区/县/镇/村/国/州/岛 等明确地理标记

    has_place = bool(_re_qc.search(r"[\u4e00-\u9fff]{2,5}(?:省|市|区|县|镇|村|国|州|府|岛|半岛|海峡|旗|盟)", s))

    # 3. 法律动作：判决/逮捕/查处 等实质动词

    has_legal_action = any(w in s for w in [

        "判决", "裁定", "逮捕", "起诉", "查处", "通报", "罚款", "拘留",

        "冻结", "查封", "赔偿", "退赔", "兑付", "追缴", "吊销", "撤销",

        "驳回", "认定", "宣判", "终审", "二审", "一审", "侦查", "审查",

        "约谈", "召回", "下架", "停业", "整顿", "停售", "监管", "合规",

    ])

    # 4. 机构/品牌/产品名：公司/集团/银行/医院/学校/法院/检察院 + 已知品牌词

    has_org_or_brand = bool(_re_qc.search(

        r"[\u4e00-\u9fffA-Za-z·]{1,8}(?:公司|集团|银行|医院|学校|法院|检察院|监委|局|委|办|部|厅|司|所|中心|大学|中学|小学|政府|党委|纪委|人大|政协|研究所|协会|基金会|合作社|工作专班|卫生院|药监局|消保委|保监会|证监会|银保监|市场监管)",

        s,

    )) or any(brand in s for brand in [

        "滴滴", "美团", "支付宝", "微信", "淘宝", "京东", "拼多多", "抖音",

        "腾讯", "阿里", "百度", "华为", "小米", "比亚迪", "特斯拉", "苹果",

        "茅台", "伊利", "蒙牛", "农夫山泉", "海底捞", "瑞幸", "星巴克",

        "中国移动", "中国电信", "中国联通", "中石油", "中石化", "国家电网",

        "赛格", "淘宝", "天猫", "小红书", "微博", "知乎", "B站", "哔哩哔哩",

    ])

    # 5. 数字+机构 组合（最强信号）：如"罚款200万元""判处3年""涉及15人"
    has_digit_with_unit = bool(_re_qc.search(
        r"\d+(\.\d+)?\s*(元|万元|亿元|亿美元|万|%|％|人|名|岁|天|起|件|条|款|项|倍|辆|吨|公斤|米|公里|平米|平方米|个|家|所|位|家|只|条|根|张|份|户|户主|家|公里|小时|分钟|秒)",
        s,
    ))

    # 6. 硬新闻动作：必须有具体事件动作（判决/逮捕/通报/查处/伤亡等）
    _HARD_NEWS_VERBS = [
        "判决", "裁定", "逮捕", "起诉", "查处", "通报", "罚款", "拘留",
        "冻结", "查封", "赔偿", "退赔", "兑付", "追缴", "吊销", "撤销",
        "驳回", "认定", "宣判", "立案", "侦查", "约谈", "召回", "下架",
        "停业", "整顿", "判处", "处罚", "没收", "取缔", "关停",
        "事故", "坍塌", "爆炸", "火灾", "坠毁", "泄露", "中毒", "伤亡",
        "死亡", "牺牲", "身亡", "遇难", "受伤", "失联",
        "调查", "曝光", "涉嫌", "造假", "售假", "骗保", "欠薪", "克扣",
        "欺诈", "违约", "发布", "出台", "印发", "施行", "生效",
    ]
    has_hard_news = any(v in s for v in _HARD_NEWS_VERBS)

    # 7. 全文都是套话主题词（"消费者权益""高质量发展"）而无事实 → 假稿信号
    is_thematic_only = bool(_re_qc.fullmatch(
        r"[\u4e00-\u9fff，。、：；！？\s]+",
        s,
    )) and not has_digit and not has_legal_action and not has_place and not has_org_or_brand

    # === 核心修改：白名单逻辑 ===
    # 必须命中至少 2 个具体信息点
    # 且其中至少 1 个必须是"硬新闻动作"或"具量纲数字"（防纯地名+日期的空稿）
    concrete_signals = sum([
        has_digit_with_unit, has_place, has_legal_action, has_org_or_brand, has_digit,
    ])
    if concrete_signals < 2:
        return False

    # 如果5个信号里没有硬新闻动作也没有具量纲数字 → 内容太软
    if not has_hard_news and not has_digit_with_unit:
        return False



    # 套话浓度过高：3 个以上套话词 + 信息点<2 → 视为空话

    _FLUFF = [

        "持续推进", "扎实推进", "有序开展", "深入实施", "全面提升", "统筹推进",

        "高质量发展", "新发展格局", "良性循环", "取得成效", "备受关注",

        "据悉", "据介绍", "相关负责人表示", "近年来", "持续加强", "不断夯实",

        "聚焦", "助力", "赋能", "推动", "亮相", "举办", "圆满", "持续优化",

        "新篇章", "高质量发展", "新局面", "新台阶", "开创新", "谱写",

        "进一步加强", "切实抓好", "落地见效", "见成效",

    ]

    fluff_count = sum(1 for w in _FLUFF if w in s)

    if fluff_count >= 2 and concrete_signals < 2:

        return False

    if fluff_count >= 4:

        return False



    return True





def _is_garbage_content(text: str, title: str = "") -> bool:

    """判断提取到的内容是否是垃圾/无效内容。



    常见垃圾模式：

    - 版权声明

    - 大量时间戳+作者名组合（如评论区/推荐列表）

    - 标题重复多次

    - 几乎没有中文字符

    """

    if not text:

        return True



    # 1. 版权声明

    if re.search(r"Copyright|All Rights Reserved|版权所有|新浪公司|搜狐新闻", text, re.I):

        return True



    # 2. 标题重复多次（重复3次以上视为垃圾）

    if title and text.count(title) >= 3:

        return True



    # 3. 大量时间戳+作者名组合（一行一个时间戳）

    # 如 "凉了时光人 2026-06-25 03:15:32" 这种格式

    timestamp_author_patterns = re.findall(

        r"[\u4e00-\u9fff]{2,8}\s+\d{4}[-./]\d{1,2}[-./]\d{1,2}\s+\d{1,2}:\d{2}(:\d{2})?",

        text

    )

    if len(timestamp_author_patterns) >= 5:

        return True



    # 4. 有效中文字符比例过低（<30%）

    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", text))

    total_chars = len(text.replace(" ", ""))

    if total_chars > 20 and cn_chars / total_chars < 0.3:

        return True



    # 5. 内容几乎全是发布信息（发布于、文章数等）

    pub_meta_patterns = re.findall(r"发布于|文章\s*\d+|Copyright|SINA|SOHU|All Rights", text, re.I)

    if len(pub_meta_patterns) >= 3 and cn_chars < 100:

        return True



    return False





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



    # 判断是否不足：字数不够、段落太少、与标题高度重复、或 raw 为空

    need_fetch = False

    para_count = len([p for p in re.split(r"[。！？\n]", text) if len(p.strip()) > 10])

    if not raw and url:

        need_fetch = True

    elif len(text) < 80:

        need_fetch = True

    elif para_count < 2:

        # 只有一段（通常是电头导语），正文大概率不完整

        need_fetch = True

    elif len(text) < 150:

        common = sum(1 for c in text if c in title)

        if common > len(text) * 0.5:

            need_fetch = True



    if not need_fetch or not url:

        return text



    # 对已知无法提取正文的页面类型直接放弃（标记为富化失败，避免 AI 拿到空文幻觉）

    SPA_SKIP = [

        "video.sina.com.cn",         # 新浪视频

        "slide.news.sina.com.cn",    # 新浪 slide 图片轮播

        "k.sina.com.cn/article_",    # 新浪 k 站 SPA 文章

        "ent.sina.com.cn/video",     # 新浪娱乐视频

        "slide.sports.sina.com.cn",  # 新浪体育图集

        "baijiahao.baidu.com",       # 百家号 SPA

    ]

    for pat in SPA_SKIP:

        if pat in url:

            article["_enrich_failed"] = True

            return ""



    try:

        resp = req.get(url, timeout=10, headers={

            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        })

        resp.encoding = resp.apparent_encoding or "utf-8"

        html = resp.text



        # 先按网站特性提取（最可靠）

        content_html = ""

        body = ""



        # 中新网：正文在 <!--正文start--> 与 <!--正文end--> 之间的 <div class="left_zw"> 中

        if "chinanews.com" in url:

            # 方法1：直接用正文注释定位

            m = re.search(

                r'<!--正文start-->(.*?)<!--正文end-->',

                html, re.S | re.I

            )

            if m:

                content_html = m.group(1)

            # 方法2：定位 left_zw 容器（方法1失败时备用）

            if not content_html:

                m = re.search(

                    r'<div[^>]+class="left_zw"[^>]*>(.*?)</div>\s*<div[^>]+class="clear"',

                    html, re.S | re.I

                )

                if m:

                    content_html = m.group(1)



        # 搜狐文章：<article> 标签

        if not content_html and "sohu.com/a/" in url:

            m = re.search(r"<article[^>]*>(.*?)</article>", html, re.S)

            if m:

                content_html = m.group(1)



        # 网易文章：<div class="post_text"> 或 <div id="articleContent">

        if not content_html and ("163.com" in url):

            m = re.search(r'<div class="post_text[^"]*">(.*?)</div>\s*<div', html, re.S)

            if not m:

                m = re.search(r'<div id="articleContent">(.*?)</div>\s*(?:<div|<script)', html, re.S)

            if m:

                content_html = m.group(1)



        # 新浪新闻：<div class="article-content"> 或 <div id="artibody">

        if not content_html and ("sina.com.cn" in url):

            m = re.search(r'<div class="article-content[^"]*">(.*?)</div>\s*(?:<div|<script)', html, re.S)

            if not m:

                m = re.search(r'<div id="artibody">(.*?)</div>\s*(?:<div|<script)', html, re.S)

            if m:

                content_html = m.group(1)



        if content_html:

            # 清理脚本/样式

            body = re.sub(r"<script[^>]*>.*?</script>", "", content_html, flags=re.S)

            body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.S)

            # 只保留 <p> 标签里的文字

            ps = re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)

            clean_ps = [re.sub(r"<[^>]+>", "", p).strip() for p in ps]

            clean_ps = [p for p in clean_ps if len(p) >= 10 and re.search(r"[\u4e00-\u9fff]", p)]

            if len(clean_ps) >= 2:

                body = " ".join(clean_ps)

                if len(body) > 1000:

                    body = body[:1000]

                if not _is_garbage_content(body, title):

                    return f"{title}\n{body}"



        # 通用高质量方案：找所有含中文的 <p> 标签，取连续密度最高的区域

        all_paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S)

        good_paras = []

        for p_html in all_paras:

            text_only = re.sub(r"<[^>]+>", "", p_html).strip()

            if len(text_only) >= 25 and re.search(r"[\u4e00-\u9fff]", text_only):

                good_paras.append(text_only)



        if len(good_paras) >= 3:

            ps_iter = list(re.finditer(r"<p[^>]*>(.*?)</p>", html, re.S))

            good_indices = []

            for i, m in enumerate(ps_iter):

                text_only = re.sub(r"<[^>]+>", "", m.group(1)).strip()

                if len(text_only) >= 25 and re.search(r"[\u4e00-\u9fff]", text_only):

                    good_indices.append(i)



            if len(good_indices) >= 3:

                best_start = 0

                best_count = 1

                cur_start = 0

                cur_count = 1

                for i in range(1, len(good_indices)):

                    if good_indices[i] - good_indices[i-1] <= 3:

                        cur_count += 1

                    else:

                        if cur_count > best_count:

                            best_count = cur_count

                            best_start = cur_start

                        cur_start = i

                        cur_count = 1

                if cur_count > best_count:

                    best_count = cur_count

                    best_start = cur_start



                selected = good_indices[best_start:best_start + best_count]

                body = " ".join(re.sub(r"<[^>]+", "", ps_iter[i].group(1)) for i in selected)

                body = re.sub(r"\s+", " ", body).strip()

                if len(body) > 200:

                    if len(body) > 1000:

                        body = body[:1000]

                    if not _is_garbage_content(body, title):

                        return f"{title}\n{body}"



        # 以上失败，降级到全页清标签方案

        html2 = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S)

        html2 = re.sub(r"<style[^>]*>.*?</style>", "", html2, flags=re.S)

        html2 = re.sub(r"<[^>]+>", "", html2)

        html2 = re.sub(r"&[a-z]+;", " ", html2)

        _NAV_GARBAGE = {"首页", "新闻", "财经", "法治", "社会", "生活", "关于", "登录", "注册",

                        "导航", "搜索", "菜单", "返回", "顶部", "版权", "声明", "广告"}

        lines = []

        for l in html2.split("\n"):

            s = l.strip()

            if not s or len(s) < 4 or s in _NAV_GARBAGE:

                continue

            lines.append(s)

        if lines:

            start = 0

            for i, line in enumerate(lines):

                if len(line) >= 30:

                    start = i

                    break

            body = "\n".join(lines[start:])

            if len(body) > 800:

                body = body[:800]

            if len(body) > len(text) + 20:

                if not _is_garbage_content(body, title):

                    return f"{title}\n{body}"

    except Exception:

        pass



    # fetch 失败时：如果有原始 RSS 摘要则返回它，否则标记富化失败并返回空

    if raw:

        return raw

    article["_enrich_failed"] = True

    return ""





def _ai_summarize_one(title: str, text: str, limit: int, api_key: str) -> str:

    """对单条新闻调用 AI 生成摘要，返回摘要字符串（失败返回空字符串）。"""

    import requests as req



    prompt = (
        '你是新闻编辑。把新闻浓缩成不超过%d字的摘要。\n'
        '【必须包含】(1)谁做了什么(机构+动作) (2)关键数字(金额/人数/百分比) (3)地点。\n'
        '【动作词】判决/逮捕/通报/查处/罚款/赔偿/下架/召回/立案/牺牲/身亡/事故/爆炸/造假/欠薪\n'
        '【禁止】套话(聚焦/助力/赋能/推进/打造/新图景)、省略号、照搬标题、空话(取得成效/备受关注)\n'
        '【自检】去掉数字后摘要还有事实吗？没有就重写。没有数字的摘要直接输出"无实质信息"。\n'
        '标题：%s\n内容：%s\n直接输出摘要正文。'
    ) % (limit, title, text)



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

        

        # 拒绝AI的"质量不足/无实质信息"返回

        if "无实质信息" in summary or "质量不足" in summary or "无实质" in summary or len(summary) < 10:

            return ""

        

        if len(summary) > limit:

            summary = truncate(summary + "。", limit)

        else:

            summary = truncate(summary, limit)

        if not summary.endswith("。"):

            summary += "。"

        return summary if len(summary) >= 20 else ""

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

        raw_path = DATA_DIR / f"raw-articles-{poster_date.isoformat()}.json"

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

            ai_summarize_batch(articles, limit=80, api_key=api_key)

            # 过期文章过滤
            _before = len(articles)
            articles = [a for a in articles if not _is_stale_article(a, poster_date)]
            _stale = _before - len(articles)
            if _stale:
                print(f"  [选稿] 过滤 {_stale} 条过期文章")

            # AI 未覆盖的文章回退到规则清洗（与 fetch 模式保持一致）

            fallback_count = 0

            for a in articles:

                if not a.get("_ai_summary"):

                    raw = (a.get("raw_content", "")

                          or a.get("_feed_text", "")

                          or a.get("raw_summary", "")

                          or a.get("summary", ""))

                    if not raw:

                        raw = a.get("title", "")

                    improved = summarize(raw=raw, title=a.get("title", ""), limit=65)

                    if improved and len(improved) >= 20:

                        a["summary"] = improved

                        fallback_count += 1

            if fallback_count:

                print(f"  [摘要] {fallback_count} 条 AI 未覆盖，已用规则清洗补齐")

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

                used_fb_aj = _load_used_fallbacks(output_dir if args.output_dir else (ROOT / "output"))

                # existing_titles 只包含：

                # 1. 当前已选的 articles（Phase 1 内选的真实文章）

                # 2. 前 N 天已推送的真实文章（按 source 不属于"fallback"或 config 来源的）

                # 注意：不要把前 N 天的 fallback 标题加进去——fallback 跨日去重完全由

                # used_fallbacks 的 cooldown 机制管理（_pick_fallback 第一轮 + 第二轮都会参考）。

                # 之前把前 N 天 fallback 标题也加进 existing_titles，会导致 cooldown 已过的

                # fallback 仍然被 used_titles 第一轮 continue 永久阻挡，pool 越来越小。

                fb_titles_in_config = {fb.get("title", "") for fb in config.get("fallback_articles", [])}

                existing_titles = {a.get("title") for a in s}

                existing_titles.update(

                    a.get("title", "") for a in previous

                    if a.get("scope") == scope

                    and a.get("title")

                    and a.get("title") not in fb_titles_in_config

                )

                while len(s) < target:

                    fb = _pick_fallback(config, scope, existing_titles, used_fb_aj, poster_date)

                    if fb:

                        s.append(fb)

                        existing_titles.add(fb.get("title", ""))

                    else:

                        break

                _save_used_fallbacks(output_dir if args.output_dir else (ROOT / "output"), used_fb_aj)

            selected.extend(s[:target])



        # 最终质量兜底：把空/垃圾摘要的文章替换为 fallback

        used_fb = _load_used_fallbacks(output_dir if args.output_dir else (ROOT / "output"))

        # 预收集 selected 中所有标题，防止 Phase 2 重复选 Phase 1 已选的 fallback

        all_selected_titles = {a.get("title", "") for a in selected}

        # 排除前 N 天的 fallback 标题（由 used_fallbacks cooldown 单独管理）

        fb_titles_in_config = {fb.get("title", "") for fb in config.get("fallback_articles", [])}

        final_clean: list[dict] = []

        replaced_count = 0

        kept_original = 0

        for a in selected:

            if _has_valid_summary(a.get("summary", ""), a.get("title", "")):

                final_clean.append(a)

            else:

                scope = a.get("scope", "")

                used_titles = {x.get("title", "") for x in final_clean}

                used_titles.update(all_selected_titles)  # 包含 Phase 1 已选的 fallback

                used_titles.update(

                    p.get("title", "") for p in previous

                    if p.get("scope") == scope

                    and p.get("title")

                    and p.get("title") not in fb_titles_in_config

                )

                fb = _pick_fallback(config, scope, used_titles, used_fb, poster_date)

                if fb:

                    final_clean.append(fb)

                    replaced_count += 1

                else:

                    # fallback 池已耗尽（30天内全用过），保留原文章 + 规则摘要

                    raw = (a.get("raw_content", "") or a.get("_feed_text", "")

                           or a.get("raw_summary", "") or a.get("summary", "")

                           or a.get("title", ""))

                    improved = summarize(raw=raw, title=a.get("title", ""), limit=65)

                    a["summary"] = improved or a.get("title", "")

                    final_clean.append(a)

                    kept_original += 1

        _save_used_fallbacks(output_dir if args.output_dir else (ROOT / "output"), used_fb)

        if replaced_count:

            print(f"[选稿] {replaced_count} 条空/垃圾摘要被 fallback 替换")

        if kept_original:

            print(f"[选稿] {kept_original} 条 fallback 池耗尽，保留原文章+规则摘要")

        articles = final_clean

        print(f"[选稿] 去重后国内={len([a for a in articles if a.get('scope')=='domestic'])} 条，国际={len([a for a in articles if a.get('scope')=='international'])} 条")

    else:

        articles, errors = (

            (fallback_articles(config), [])

            if args.no_fetch

            else fetch_articles(config, output_dir, poster_date)

        )

        # 保存 raw-articles 文件，供跨日去重使用

        if not args.no_fetch:

            raw_path = DATA_DIR / f"raw-articles-{poster_date.isoformat()}.json"

            try:

                with raw_path.open("w", encoding="utf-8") as f:

                    json.dump(articles, f, ensure_ascii=False, indent=2)

                print(f"[抓取] 保存 {len(articles)} 条原始文章到 {raw_path}")

            except Exception as e:

                print(f"[抓取] 保存 raw-articles 失败: {e}")

        # 自动摘要：有 API key 用 AI，没 key 回退规则清洗

        if not args.no_fetch:

            api_key = config.get("deepseek_api_key", "").strip()

            if api_key:

                print("[摘要] AI 智能摘要...")

                for a in articles:

                    a["raw_summary"] = a.get("summary", "")

                ai_summarize_batch(articles, limit=80, api_key=api_key)

                # AI 未覆盖的文章回退到规则清洗

                fallback_count = 0

                for a in articles:

                    if not a.get("_ai_summary"):

                        raw = (a.get("raw_content", "")

                              or a.get("_feed_text", "")

                              or a.get("raw_summary", "")

                              or a.get("summary", ""))

                        if not raw:

                            print(f"  [摘要调试] 空raw: {a.get('title','')[:30]}")

                        improved = summarize(raw=raw, title=a.get("title", ""), limit=65)

                        if improved and len(improved) >= 20:

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



            # 最终质量兜底：把空/垃圾摘要的文章替换为 fallback（与 --articles-json 路径一致）

            prev_dir_q = output_dir if args.output_dir else (ROOT / "output")

            previous_q = _load_previous_articles(prev_dir_q, poster_date)

            used_fb = _load_used_fallbacks(prev_dir_q)

            # 预收集所有文章标题，防止 Phase 2 重复选 Phase 1 已选的 fallback

            all_articles_titles = {a.get("title", "") for a in articles}

            final_clean: list[dict] = []

            replaced_count = 0

            kept_original = 0

            for a in articles:

                if _has_valid_summary(a.get("summary", ""), a.get("title", "")):

                    final_clean.append(a)

                else:

                    scope = a.get("scope", "")

                    used_titles = {x.get("title", "") for x in final_clean}

                    used_titles.update(all_articles_titles)  # 包含 Phase 1 已选的 fallback

                    used_titles.update(p.get("title", "") for p in previous_q if p.get("scope") == scope)

                    fb = _pick_fallback(config, scope, used_titles, used_fb, poster_date)

                    if fb:

                        final_clean.append(fb)

                        replaced_count += 1

                    else:

                        # fallback 池已耗尽，保留原文章 + 规则摘要

                        raw = (a.get("raw_content", "") or a.get("_feed_text", "")

                               or a.get("raw_summary", "") or a.get("summary", "")

                               or a.get("title", ""))

                        improved = summarize(raw=raw, title=a.get("title", ""), limit=65)

                        a["summary"] = improved or a.get("title", "")

                        final_clean.append(a)

                        kept_original += 1

            _save_used_fallbacks(prev_dir_q, used_fb)

            if replaced_count:

                print(f"[选稿] {replaced_count} 条空/垃圾摘要被 fallback 替换")

            if kept_original:

                print(f"[选稿] {kept_original} 条 fallback 池耗尽，保留原文章+规则摘要")

            articles = final_clean

            print(f"[选稿] 质量兜底后国内={len([a for a in articles if a.get('scope')=='domestic'])} 条，国际={len([a for a in articles if a.get('scope')=='international'])} 条")



    # 保存最终发布的文章（含 fallback），供跨日去重使用

    _save_published_articles(output_dir, poster_date, articles)



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

