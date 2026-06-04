# 云嘉每日资讯海报

每天抓取公开 RSS 资讯，按国内新闻 10 条、国际新闻 6 条生成排版好的 HTML，并用 Chrome 导出为 `1080 × 2240` PNG 长图海报。

## 使用

```bash
./run.sh
```

输出文件位于 `output/`。

常用参数：

```bash
# 指定日期
python3 generate_poster.py --date 2026-06-01

# 不联网，使用 config.json 中的示例资讯
python3 generate_poster.py --no-fetch

# 只生成 HTML，便于调试样式
python3 generate_poster.py --no-fetch --html-only
```

## 交给其他 AI 运行

将 `云嘉每日资讯脚本包.zip` 解压后，让 AI 在目录内执行：

```bash
./run.sh
```

详细说明见 `AGENT_GUIDE.md`。

## 每日自动执行

在 macOS 或 Linux 中运行 `crontab -e`，添加：

```cron
0 8 * * * cd "/Users/mac/Desktop/云嘉律所/002视频类项目/云嘉每日资讯" && /usr/bin/python3 generate_poster.py >> poster.log 2>&1
```

## 定制

- 正式 Logo 使用 `assets/yunjia-logo.png`，并在模板中组合显示“北京云嘉律师事务所”和“云起龙骧 至道嘉猷”。
- 正式二维码已接入 `assets/wechat-qr.jpg`，支持继续替换为 PNG、JPG、SVG。
- 品牌 IP 云宝已生成页眉专用横幅 `assets/yunbao-header-v1.png`，并自然融入顶部蓝色背景。
- 在 `config.json` 中编辑资讯源、招聘信息、心灵鸡汤和免责声明。
- RSS 获取失败时，脚本会自动用 `fallback_articles` 中的内容补足版面。
