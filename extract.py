import re

with open('output/每日资讯-2026-06-04.html', encoding='utf-8') as f:
    html = f.read()

# 提取所有 news-item 的标题和摘要
# 匹配 news-title 和 news-content
pattern_title = r'class="news-title">(.*?)</div>'
pattern_content = r'class="news-content"><p>(.*?)</p>'

titles = re.findall(pattern_title, html)
contents = re.findall(pattern_content, html)

print(f"共找到 {len(titles)} 个标题, {len(contents)} 条摘要")
print()

# 国内新闻：前10条（按代码，国内取10条）
domestic_count = 10
print("=== 国内新闻 ===")
for i in range(min(domestic_count, len(titles))):
    print(f"\n--- 国内 #{i+1} ---")
    print(f"标题: {titles[i].strip()}")
    if i < len(contents):
        print(f"摘要: {contents[i].strip()}")
    else:
        print("摘要: (无)")
