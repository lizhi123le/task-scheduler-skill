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
    #
    # capture_output=True 是**必须**的：不捕获时 stdout/stderr 走「继承」路径，
    # 而继承来的管道句柄在计划任务/输出重定向场景下不可靠 —— 实测所有任务的
    # execution_logs.output 恒为 0（连 `cmd /c echo X` 都收不到任何输出），
    # 于是「任务日志」形同虚设。显式捕获后再转发给本进程的 stdout/stderr，
    # 由调用方（schedule.py 的 capture_output）收进数据库。
    _kw = {}
    if sys.platform == 'win32' and hasattr(subprocess, 'CREATE_NO_WINDOW'):
        _kw['creationflags'] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        user_command,
        shell=True,
        encoding='utf-8',
        errors='replace',
        capture_output=True,
        **_kw
    )

    # 原样转发（不做编码转换：本进程已按 UTF-8 解码，且父进程也按 UTF-8 读）
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError):
        pass                      # pythonw 下可能没有可用的 std 流

    sys.exit(result.returncode)
