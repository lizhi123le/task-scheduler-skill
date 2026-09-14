#!/usr/bin/env python3
"""
YouTube Downloader UTF-8 启动器
专门用于解决 Windows 编码问题
"""
import sys
import os

# Windows 编码修复
if sys.platform == 'win32':
    import ctypes
    
    # 设置控制台代码页为 UTF-8
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleCP(65001)
    kernel32.SetConsoleOutputCP(65001)
    
    # 设置环境变量
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8MODE'] = '1'
    os.environ['LC_ALL'] = 'C.UTF-8'

# 导入并运行主程序
import runpy

# 运行 youtube_daily.py
main_script = r"C:\Users\lizhi\.config\opencode\skills\task-scheduler\examples\youtube_daily.py"

try:
    exit_code = runpy.run_path(main_script, run_name='__main__')
except SystemExit as e:
    sys.exit(e.code)
except Exception as e:
    print(f"错误: {e}")
    sys.exit(1)
