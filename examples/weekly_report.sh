#!/bin/bash
# 示例：周报生成脚本

echo "生成周报..."
DATE=$(date +%Y-%m-%d)
WEEK_START=$(date -d "last monday" +%Y-%m-%d)

# 统计下载文件夹
python ~/.config/opencode/skills/file-organizer/scripts/stats.py ~/Downloads --format markdown --output ~/Reports/weekly-$DATE.md

# 发送邮件通知（需要配置）
# echo "周报已生成" | mail -s "周报 $WEEK_START - $DATE" admin@example.com

echo "周报生成完成: ~/Reports/weekly-$DATE.md"
