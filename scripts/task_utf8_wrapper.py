#!/usr/bin/env python3
"""
Task Scheduler UTF-8 Wrapper
用于在 Windows 上执行命令时设置正确的编码环境
"""
import sys
import os

# Windows 编码修复
if sys.platform == 'win32':
    import ctypes
    
    # 设置控制台代码页
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleCP(65001)
    kernel32.SetConsoleOutputCP(65001)
    
    # 设置环境变量
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8MODE'] = '1'
    os.environ['LC_ALL'] = 'C.UTF-8'
    os.environ['LANG'] = 'C.UTF-8'

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: task_utf8_wrapper.py <command>")
        sys.exit(1)
    
    user_command = sys.argv[1]
    
    import subprocess
    
    # 执行用户命令
    # 使用 shell=True 来正确处理命令和参数
    # CREATE_NO_WINDOW：显式禁止为子控制台程序创建窗口。不加这个标志时，
    # 手动 run-now（父进程带控制台）会在屏幕上弹出 cmd 窗口。
    _kw = {}
    if sys.platform == 'win32' and hasattr(subprocess, 'CREATE_NO_WINDOW'):
        _kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        user_command,
        shell=True,
        encoding='utf-8',
        errors='replace',
        **_kw
    )
    
    sys.exit(result.returncode)
