#!/bin/bash
# 示例：系统维护脚本

echo "开始系统维护..."

# 1. 清理临时文件
find /tmp -type f -mtime +7 -delete 2>/dev/null
find ~/.cache -type f -mtime +30 -delete 2>/dev/null

# 2. 检查磁盘空间
DISK_USAGE=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
if [ $DISK_USAGE -gt 80 ]; then
    echo "警告: 磁盘使用率达到 ${DISK_USAGE}%"
    # 发送通知
fi

# 3. 备份重要配置
tar -czf ~/Backups/config-$(date +%Y%m%d).tar.gz ~/.config 2>/dev/null

echo "系统维护完成"
