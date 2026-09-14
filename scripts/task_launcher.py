#!/usr/bin/env python3
"""
Task Scheduler UTF-8 启动器
在执行任何命令前设置正确的 UTF-8 环境
"""
import sys
import os

# Windows 编码修复 - 必须在最前面
if sys.platform == 'win32':
    import ctypes
    
    # 1. 设置控制台代码页
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleCP(65001)
    kernel32.SetConsoleOutputCP(65001)
    
    # 2. 设置环境变量
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8MODE'] = '1'
    os.environ['PYTHONLEGACYWINDOWSSTDIO'] = 'utf-8'
    os.environ['LC_ALL'] = 'C.UTF-8'
    os.environ['LANG'] = 'C.UTF-8'

# 运行用户命令
if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: task_launcher.py <命令> [参数...]")
        sys.exit(1)
    
    command = sys.argv[1]
    args = sys.argv[2:]
    
    import subprocess
    
    # 准备环境
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8MODE'] = '1'
    env['LC_ALL'] = 'C.UTF-8'
    
    # 构建命令
    if sys.platform == 'win32':
        # Windows: 使用 chcp 设置编码，然后执行命令
        full_command = f'chcp 65001 >nul && "{command}"'
        for arg in args:
            full_command += f' "{arg}"'
        
        result = subprocess.run(
            full_command,
            shell=True,
            env=env,
            encoding='utf-8',
            errors='replace'
        )
    else:
        # Linux/macOS
        cmd_list = [command] + args
        result = subprocess.run(
            cmd_list,
            env=env
        )
    
    sys.exit(result.returncode)
