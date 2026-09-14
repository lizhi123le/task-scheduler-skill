#!/bin/bash
# 示例：每日自动整理下载文件夹

echo "开始整理下载文件夹..."
DATE=$(date +%Y-%m-%d)

# 整理文件
python ~/.config/opencode/skills/file-organizer/scripts/organize.py ~/Downloads

# 记录日志
echo "[$DATE] 整理完成" >> ~/logs/daily_organize.log

# 清理旧日志（保留30天）
find ~/logs -name "*.log" -mtime +30 -delete

echo "每日整理任务完成"
