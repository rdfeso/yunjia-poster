# AI 运行说明

## 目标

生成北京云嘉律师事务所每日资讯长图海报：

- 国内新闻 10 条
- 国际新闻 6 条
- 阳历、农历日期与期号
- 固定 Logo、云宝页眉、招聘文案、二维码和免责声明

## 内容规范（整理阶段主动控制）

- **标题**：控制在 22 字左右（不超过 24 个字符）。
- **摘要**：控制在 65–70 字左右（不超过 70 个字符）。
- 整理文字时即应主动精简，避免依赖脚本截断，保证版面美观与阅读体验。

## 直接运行

在脚本包根目录执行：

```bash
chmod +x run.sh
./run.sh
```

默认抓取运行当天的公开 RSS 新闻，并输出：

```text
output/每日资讯-YYYY-MM-DD.html
output/每日资讯-YYYY-MM-DD.png
```

## 常用命令

```bash
# 指定日期
./run.sh --date 2026-06-01

# 离线测试：使用 config.json 内置示例新闻
./run.sh --no-fetch

# 只生成 HTML
./run.sh --html-only
```

## 环境要求

- Python 3.9+
- Google Chrome，默认路径：
  `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
- 网络可访问 RSS 来源；网络失败时脚本会自动使用备用内容补足版面

## 定制入口

- 新闻数量、RSS、文案：`config.json`
- HTML 样式：`templates/poster.html`
- 固定素材：`assets/`

## RSS 源说明

当前使用**中新网**（chinanews.com.cn）的实时 RSS，已过滤停更的人民日报源。脚本会自动按 `pubDate` 过滤，只保留**当天新闻**。如果当天 RSS 源无内容或抓取失败，会自动使用 `config.json` 中的 fallback 数据补足版面。

不要修改 `output/` 中的文件，它们是每次运行自动生成的成品。
