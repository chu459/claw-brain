"""提取Brain对话日志，分析可用于微调的数据"""
import json, glob

all_entries = []

# 1. sessions 目录
for f in glob.glob('sessions/sess_*.json'):
    try:
        with open(f, encoding='utf-8') as fp:
            data = json.load(fp)
        for entry in data.get('brain_log', []):
            entry['_source'] = f
            all_entries.append(entry)
    except:
        pass

# 2. state_logs 文件
for f in glob.glob('state_logs*.json'):
    try:
        with open(f, encoding='utf-8') as fp:
            data = json.load(fp)
        for entry in data.get('brain_log', []):
            entry['_source'] = f
            all_entries.append(entry)
    except:
        pass

print(f"总条数: {len(all_entries)}")

has_thought = [e for e in all_entries if e.get('thought', '').strip()]
has_action = [e for e in all_entries if e.get('action', '').strip()]
both = [e for e in all_entries if e.get('thought', '').strip() and e.get('action', '').strip()]
print(f"有thought: {len(has_thought)}")
print(f"有action: {len(has_action)}")
print(f"同时有thought+action: {len(both)}")

# 按内容关键词分类
keywords_categories = {
    '低价值-PPT': ['PPT', 'ppt', '幻灯片', '演示文稿'],
    '低价值-简历': ['简历', 'resume', '求职'],
    '低价值-文案': ['文案', '写作', '文章', '博客', 'blog'],
    '低价值-翻译': ['翻译', 'translate'],
    '低价值-Logo': ['logo', 'Logo', '标志设计'],
    '高价值-自动化': ['自动化', 'automat', '监控', '定时', 'cron', '爬虫', 'scraper'],
    '高价值-Agent': ['agent', '智能体', 'bot', '机器人'],
    '高价值-平台运营': ['闲鱼', '拼多多', '淘宝', '上架', '发布商品', '运营'],
    '探索-调研': ['搜索', '调研', '搜索', '查', '了解', '分析'],
    '探索-验证': ['验证', '测试', '确认', '截图', '打开'],
}

print("\n=== 关键词分类统计 ===")
for cat, kws in keywords_categories.items():
    count = 0
    for e in all_entries:
        text = (e.get('thought', '') + ' ' + e.get('action', '')).lower()
        if any(kw.lower() in text for kw in kws):
            count += 1
    if count > 0:
        print(f"  {cat}: {count}条")

# 展示一些典型样本
print("\n=== 典型低价值样本 ===")
low_value_kws = ['PPT', 'ppt', '简历', 'resume', '文案', '翻译', 'Logo', 'logo']
for e in all_entries:
    text = e.get('thought', '') + ' ' + e.get('action', '')
    if any(kw in text for kw in low_value_kws):
        print(f"  thought: {e.get('thought', '')[:120]}")
        print(f"  action: {e.get('action', '')[:100]}")
        print(f"  status: {e.get('status', '')}")
        print("  ---")
        break

print("\n=== 典型高价值样本 ===")
high_value_kws = ['自动化', '监控', 'agent', '智能体', '闲鱼', '运营']
for e in all_entries:
    text = e.get('thought', '') + ' ' + e.get('action', '')
    if any(kw in text for kw in high_value_kws):
        print(f"  thought: {e.get('thought', '')[:120]}")
        print(f"  action: {e.get('action', '')[:100]}")
        print(f"  status: {e.get('status', '')}")
        print("  ---")
        break

# 统计各 status 分布
status_dist = {}
for e in all_entries:
    s = e.get('status', 'unknown')
    status_dist[s] = status_dist.get(s, 0) + 1
print(f"\n=== Status 分布 ===")
for s, c in sorted(status_dist.items(), key=lambda x: -x[1]):
    print(f"  {s}: {c}")

# 看几个有 update_memory 的样本（这些是Brain自己反思的结果）
with_memory = [e for e in all_entries if e.get('update_memory', '').strip()]
print(f"\n=== 有 update_memory 的条数: {len(with_memory)} ===")
for e in with_memory[:3]:
    print(f"  thought: {e.get('thought', '')[:100]}")
    print(f"  update_memory: {e.get('update_memory', '')[:200]}")
    print("  ---")
