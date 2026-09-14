#!/usr/bin/env python3
"""
YouTube Daily Downloader - UTF-8 启动器
在 Windows 上强制使用 UTF-8 编码环境
"""
import sys
import os

# 强制在启动时设置 UTF-8 环境
if sys.platform == 'win32':
    # 导入必要的模块
    import ctypes
    import subprocess
    
    # 1. 设置控制台代码页为 UTF-8
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
    
    # 2. 设置环境变量
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8MODE'] = '1'
    os.environ['PYTHONLEGACYWINDOWSSTDIO'] = 'utf-8'
    os.environ['LC_ALL'] = 'C.UTF-8'
    os.environ['LANG'] = 'C.UTF-8'
    
    # 3. 使用 subprocess.CREATE_NO_WINDOW 避免控制台编码问题
    creation_flags = subprocess.CREATE_NO_WINDOW
    
    # 4. 启动主程序，设置环境并使用 UTF-8 编码
    main_script = r'C:\Users\lizhi\.config\opencode\skills\task-scheduler\examples\youtube_daily.py'
    
    cmd = [
        sys.executable,
        '-X', 'utf8',  # 启用 Python UTF-8 模式
        '-c',
        f'''
import sys
import os
# 确保所有子进程使用 UTF-8
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8MODE"] = "1"
os.environ["LC_ALL"] = "C.UTF-8"

# 导入并运行主程序
import runpy
runpy.run_path({repr(main_script)}, run_name='__main__')
'''
    ]
    
    # 使用 CREATE_NO_WINDOW 和 UTF-8 环境运行
    result = subprocess.run(
        cmd,
        env={
            **os.environ,
            'PYTHONIOENCODING': 'utf-8',
            'PYTHONUTF8MODE': '1',
            'LC_ALL': 'C.UTF-8',
            'LANG': 'C.UTF-8',
        },
        encoding='utf-8',  # 确保主进程输出是 UTF-8
        creationflags=creation_flags,
    )
    sys.exit(result.returncode)

else:
    # 非 Windows 系统直接运行
    import runpy
    sys.exit(runpy.run_path(
        r'C:\Users\lizhi\.config\opencode\skills\task-scheduler\examples\youtube_daily.py',
        run_name='__main__'
    ))
