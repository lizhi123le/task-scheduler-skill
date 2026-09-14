#!/usr/bin/env python3
"""
Windows UTF-8 编码修复模块
在导入任何其他模块之前设置正确的编码环境
"""
import sys
import os

# 必须在最开始设置环境变量
if sys.platform == 'win32':
    # Windows 编码修复：设置环境变量
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8MODE'] = '1'
    os.environ['PYTHONLEGACYWINDOWSSTDIO'] = 'utf-8'
    os.environ['LC_ALL'] = 'C.UTF-8'
    os.environ['LANG'] = 'C.UTF-8'
    
    # 设置控制台代码页
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleOutputCP(65001)
    kernel32.SetConsoleCP(65001)

# 创建安全的 subprocess 调用函数
import subprocess
import tempfile

def run_subprocess_utf8(cmd, timeout=30):
    """
    安全运行子进程，避免 Windows 编码问题
    """
    # 创建环境变量副本
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8MODE'] = '1'
    env['LC_ALL'] = 'C.UTF-8'
    
    # 创建临时文件存储输出
    with tempfile.NamedTemporaryFile(mode='w+b', suffix='.log', delete=False, encoding=None) as stdout_file:
        stdout_path = stdout_file.name
    
    with tempfile.NamedTemporaryFile(mode='w+b', suffix='.log', delete=False, encoding=None) as stderr_file:
        stderr_path = stderr_file.name
    
    try:
        # 使用 start-up-info 确保正确的编码
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE
        
        # 启动进程
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            startupinfo=startup_info,
            universal_newlines=False,  # 二进制模式
        )
        
        # 等待进程完成
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        
        # 读取原始字节
        stdout_bytes = process.stdout.read()
        stderr_bytes = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
        
        # 尝试多种编码
        def decode_bytes(data):
            if not data:
                return ''
            for encoding in ['utf-8', 'utf-8-sig', 'gbk', 'latin1']:
                try:
                    return data.decode(encoding)
                except (UnicodeDecodeError, LookupError):
                    continue
            # 最后手段：忽略错误
            return data.decode('utf-8', errors='replace')
        
        return subprocess.CompletedProcess(
            cmd,
            process.returncode,
            decode_bytes(stdout_bytes),
            decode_bytes(stderr_bytes)
        )
        
    finally:
        # 清理临时文件
        try:
            os.unlink(stdout_path)
        except:
            pass
        try:
            os.unlink(stderr_path)
        except:
            pass

# 导出函数
__all__ = ['run_subprocess_utf8']
