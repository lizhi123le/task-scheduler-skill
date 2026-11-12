#!/usr/bin/env python3
"""
Task Scheduler v2.0 - 跨平台定时任务管理器（改进版）
修复内容：
1. Windows 编码问题
2. 系统调度器实现
3. 配置文件验证
4. macOS plist 时间硬编码
5. 任务依赖执行
6. Windows 原生通知
7. 任务重试机制
8. 结构化日志
"""

# Windows 编码修复（必须放在最前面）
import sys
import os

if sys.platform == 'win32':
    # 设置环境变量
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8MODE'] = '1'
    
    # 设置 Windows 控制台编码
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except:
        pass

import json
import argparse
import re
import subprocess
import platform
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field, fields
from enum import Enum
import threading
import traceback
import calendar
import copy

# Windows 编码修复
if sys.platform == 'win32':
    import ctypes
    # 设置 Windows 控制台为 UTF-8
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleCP(65001)
    kernel32.SetConsoleOutputCP(65001)


def _fmt_ts(dt: datetime) -> str:
    """统一入库时间戳格式（本机时间，秒精度）

    原来各写入点直接用 `datetime.isoformat()`，会带上微秒
    （如 2026-09-14T07:06:13.901157）且无时区标注；而 Windows 任务计划程序
    一侧是秒精度的本机时间，两边对不上，排查时容易误判成时间错位。
    统一为 `%Y-%m-%dT%H:%M:%S`（仍可被 datetime.fromisoformat 解析）。

    定义在模块级而非某个类里：它被 DatabaseManager.log_execution 与
    TaskScheduler._execute_task 两处使用，放在任一类的内部都会让另一处
    拿不到（曾因此导致"记录日志失败: no attribute '_fmt_ts'"）。
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class Platform(Enum):
    """操作系统平台"""
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


class TaskStatus(Enum):
    """任务状态"""
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"
    RUNNING = "running"
    FAILED = "failed"


@dataclass
class Task:
    """定时任务数据结构"""
    id: int
    name: str
    command: str
    schedule_type: str
    schedule_value: str
    status: str = "active"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    run_count: int = 0
    fail_count: int = 0
    group: Optional[str] = None
    depends_on: Optional[str] = None
    working_dir: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    notify_on_success: Optional[str] = None
    notify_on_fail: Optional[str] = None
    slack_webhook: Optional[str] = None
    email: Optional[str] = None
    retry_count: int = 3  # 失败重试次数
    retry_delay: int = 60  # 重试间隔（秒）


# 「本次调用没有提供该字段」的哨兵。update_task 用它区分“没传”和“传了空值/None”：
# 前者保持原值，后者才是真的要清空（例如 --group "" 清掉分组）。
_UNSET = object()


class SafePrint:
    """安全的打印类，处理编码问题"""
    @staticmethod
    def print(message: str, **kwargs):
        # 无控制台场景（pythonw / 计划任务隐藏窗口）下 sys.stdout 可能为 None
        # 或句柄无效，此时 print() 会抛 AttributeError/OSError；不兜住会让整个
        # 任务执行中途崩溃（比"有窗口"更糟）。这里统一降级为静默丢弃。
        try:
            if sys.stdout is None:
                return
            print(message, **kwargs)
        except UnicodeEncodeError:
            # 移除 emoji 和特殊字符
            safe_msg = message.encode('ascii', 'ignore').decode('ascii')
            try:
                print(safe_msg, **kwargs)
            except (AttributeError, OSError, ValueError):
                pass
        except (AttributeError, OSError, ValueError):
            # stdout 已关闭 / 句柄无效 / 解释器正在关闭
            pass
    
    @staticmethod
    def success(msg: str):
        SafePrint.print(f"[OK] {msg}")
    
    @staticmethod
    def error(msg: str):
        SafePrint.print(f"[ERROR] {msg}")
    
    @staticmethod
    def warn(msg: str):
        SafePrint.print(f"[WARN] {msg}")
    
    @staticmethod
    def info(msg: str):
        SafePrint.print(f"[INFO] {msg}")


_WIN_GUI_LAUNCHERS = ("wscript", "cscript", "mshta")


def split_win_command(command: str):
    """按 Windows 命令行习惯切分命令（保留引号内空格，剥掉外层引号）。"""
    import re
    tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', command)
    out = []
    for t in tokens:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'"):
            t = t[1:-1]
        out.append(t)
    return out


def gui_launcher_argv(command: str):
    """命令若只是启动一个 GUI 子系统程序（wscript/cscript/mshta），返回 argv。

    Windows 上这类命令既不需要 shell 解析，也不需要 task_utf8_wrapper 的
    python.exe + cmd.exe 中转：那条链路即使加了 CREATE_NO_WINDOW，仍会创建
    隐藏控制台 + conhost，在部分机器上会出现一闪而过的窗口。直接以
    CREATE_NO_WINDOW 启动 GUI 可执行文件，屏幕上不会有任何窗口出现。

    返回 None 表示不是这类命令，调用方继续走原来的 wrapper 路径。
    """
    if sys.platform != 'win32':
        return None
    try:
        argv = split_win_command(command)
    except Exception:
        return None
    if len(argv) < 2:
        return None
    base = argv[0].strip().lower().rsplit('\\', 1)[-1]
    if base.endswith('.exe'):
        base = base[:-4]
    if base not in _WIN_GUI_LAUNCHERS:
        return None
    return argv


class WindowsNotifier:
    """Windows 原生通知

    重要：schedule.py 由 pythonw.exe / 计划任务启动时**没有控制台**。此时用
    subprocess 起 powershell.exe（控制台子系统程序）而不加 CREATE_NO_WINDOW，
    Windows 会为它分配一个新的控制台 —— 在 Win11 默认终端下就是一个实实在在
    弹出的 Windows Terminal 窗口（实测每 5 分钟的守护任务都会弹一次，因为脚本里
    还有 Start-Sleep 6，窗口会停留好几秒）。因此这里必须用「完全无窗口」方式启动。
    """
    @staticmethod
    def _hidden_kwargs():
        """无窗口启动子进程所需的参数（仅 Windows 有效）"""
        kw = {}
        if sys.platform == 'win32':
            if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                kw['creationflags'] = subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, 'STARTUPINFO'):
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0  # SW_HIDE
                kw['startupinfo'] = si
        return kw

    @staticmethod
    def notify(title: str, message: str, icon_type: str = "info"):
        """发送 Windows 通知（不产生任何窗口）"""
        if sys.platform != 'win32':
            return
        
        try:
            # SystemIcons 与 ToolTipIcon 的枚举名不同：
            # SystemIcons = Information|Warning|Error，ToolTipIcon = Info|Warning|Error。
            # 旧代码把 "Information" 传给 ToolTipIcon，PowerShell 直接抛错，气泡其实没弹出来。
            sys_icon = {"info": "Information", "warning": "Warning", "error": "Error"}
            tip_icon = {"info": "Info", "warning": "Warning", "error": "Error"}
            icon = sys_icon.get(icon_type, "Information")
            tip = tip_icon.get(icon_type, "Info")

            # PowerShell 单引号字符串：内部单引号写两遍，避免标题/正文里的引号把脚本截断
            def _ps(text) -> str:
                return "'" + str(text).replace("'", "''") + "'"

            ps_script = f'''
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::{icon}
$notify.Visible = $true
$notify.ShowBalloonTip(5000, {_ps(title)}, {_ps(message)}, [System.Windows.Forms.ToolTipIcon]::{tip})
Start-Sleep -Seconds 6
$notify.Dispose()
'''
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-Command", ps_script],
                capture_output=True,
                timeout=15,
                encoding='utf-8',
                errors='replace',
                **WindowsNotifier._hidden_kwargs()
            )
        except Exception as e:
            SafePrint.warn(f"通知发送失败: {e}")


class DatabaseManager:
    """SQLite 数据库管理器"""
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 任务表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    command TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_value TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    created_at TEXT,
                    last_run TEXT,
                    next_run TEXT,
                    run_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    group_name TEXT,
                    depends_on TEXT,
                    working_dir TEXT,
                    env_vars TEXT,
                    notify_on_success TEXT,
                    notify_on_fail TEXT,
                    slack_webhook TEXT,
                    email TEXT,
                    retry_count INTEGER DEFAULT 3,
                    retry_delay INTEGER DEFAULT 60
                )
            ''')
            
            # 执行日志表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER,
                    task_name TEXT,
                    executed_at TEXT,
                    status TEXT,
                    duration REAL,
                    output TEXT,
                    error TEXT,
                    retry_attempt INTEGER DEFAULT 0,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                )
            ''')
            
            conn.commit()
    
    def save_task(self, task: Task) -> bool:
        """保存任务到数据库"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO tasks (
                        id, name, command, schedule_type, schedule_value,
                        status, created_at, last_run, next_run, run_count,
                        fail_count, group_name, depends_on, working_dir,
                        env_vars, notify_on_success, notify_on_fail,
                        slack_webhook, email, retry_count, retry_delay
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    task.id, task.name, task.command, task.schedule_type,
                    task.schedule_value, task.status, task.created_at,
                    task.last_run, task.next_run, task.run_count,
                    task.fail_count, task.group, task.depends_on,
                    task.working_dir, json.dumps(task.env_vars),
                    task.notify_on_success, task.notify_on_fail,
                    task.slack_webhook, task.email, task.retry_count,
                    task.retry_delay
                ))
                conn.commit()
                return True
        except Exception as e:
            SafePrint.error(f"保存任务失败: {e}")
            return False
    
    def load_tasks(self) -> List[Task]:
        """从数据库加载所有任务"""
        tasks = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM tasks')
                rows = cursor.fetchall()
                
                for row in rows:
                    task = Task(
                        id=row[0],
                        name=row[1],
                        command=row[2],
                        schedule_type=row[3],
                        schedule_value=row[4],
                        status=row[5],
                        created_at=row[6],
                        last_run=row[7],
                        next_run=row[8],
                        run_count=row[9],
                        fail_count=row[10],
                        group=row[11],
                        depends_on=row[12],
                        working_dir=row[13],
                        env_vars=json.loads(row[14]) if row[14] else {},
                        notify_on_success=row[15],
                        notify_on_fail=row[16],
                        slack_webhook=row[17],
                        email=row[18],
                        retry_count=row[19] or 3,
                        retry_delay=row[20] or 60
                    )
                    tasks.append(task)
        except Exception as e:
            SafePrint.error(f"加载任务失败: {e}")
        
        return tasks
    
    def delete_task(self, task_id: int) -> bool:
        """删除任务"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            SafePrint.error(f"删除任务失败: {e}")
            return False
    
    def renumber_ids(self, mapping: Dict[int, int]) -> bool:
        """按 {旧id: 新id} 重写 tasks.id 与 execution_logs.task_id。

        必须两阶段：先把所有要挪的行改成负数占位，再统一落最终值。
        直接 `UPDATE tasks SET id=新 WHERE id=旧` 会在"目标号还被别人占着"
        时撞主键（例如 3->2 而 2 尚未腾出）；而 tasks.name 是 UNIQUE，
        用 INSERT OR REPLACE 绕过主键冲突会顺手删掉另一行 —— 会静默丢任务。
        负数占位不占用任何正号，因此能安全地做原地置换。
        """
        moves = {old: new for old, new in mapping.items() if old != new}
        if not moves:
            return True
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                for old in moves:
                    cursor.execute('UPDATE tasks SET id = -? WHERE id = ?', (old, old))
                for old, new in moves.items():
                    cursor.execute('UPDATE tasks SET id = ? WHERE id = -?', (new, old))
                for old, new in moves.items():
                    cursor.execute(
                        'UPDATE execution_logs SET task_id = ? WHERE task_id = ?', (new, old))
                conn.commit()
                return True
        except Exception as e:
            SafePrint.error(f"重编号写库失败: {e}")
            return False
    
    def log_execution(self, task_id: int, task_name: str, status: str,
                     duration: float, output: str, error: str,
                     retry_attempt: int = 0):
        """记录执行日志（output/error 兜底为空串：无控制台运行时 stdout 可能为 None）"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO execution_logs 
                    (task_id, task_name, executed_at, status, duration, output, error, retry_attempt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (task_id, task_name, _fmt_ts(datetime.now()),
                     status, duration, output if output is not None else "",
                     error if error is not None else "", retry_attempt))
                conn.commit()
        except Exception as e:
            SafePrint.error(f"记录日志失败: {e}")
    
    def get_logs(self, task_name: Optional[str] = None, limit: int = 50,
                 status: Optional[str] = None, today: bool = False) -> List[Dict]:
        """获取执行日志"""
        logs = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                query = 'SELECT * FROM execution_logs WHERE 1=1'
                params = []
                
                if task_name:
                    query += ' AND task_name = ?'
                    params.append(task_name)
                
                if status:
                    query += ' AND status = ?'
                    params.append(status)
                
                if today:
                    query += " AND date(executed_at) = date('now', 'localtime')"
                
                query += ' ORDER BY executed_at DESC LIMIT ?'
                params.append(limit)
                
                cursor.execute(query, params)
                rows = cursor.fetchall()
                
                for row in rows:
                    logs.append(dict(row))
        except Exception as e:
            SafePrint.error(f"获取日志失败: {e}")
        
        return logs


class TaskScheduler:
    """跨平台任务调度器（改进版）"""
    
    def __init__(self):
        self.platform = self._detect_platform()
        self.config_dir = self._get_config_dir()
        self.config_file = self.config_dir / "config.json"
        self.log_dir = self.config_dir / "logs"
        self.db_path = self.config_dir / "scheduler.db"
        self.db = DatabaseManager(self.db_path)
        self.tasks: List[Task] = []
        self._ensure_directories()
        self._load_tasks()
    
    def _detect_platform(self) -> Platform:
        """检测操作系统平台"""
        system = platform.system().lower()
        if system == "windows":
            return Platform.WINDOWS
        elif system == "darwin":
            return Platform.MACOS
        else:
            return Platform.LINUX
    
    def _get_config_dir(self) -> Path:
        """获取配置目录"""
        if self.platform == Platform.WINDOWS:
            config_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
            return config_dir / "task-scheduler"
        else:
            config_dir = Path.home() / ".config"
            return config_dir / "task-scheduler"
    
    def _ensure_directories(self):
        """确保目录存在"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_tasks(self):
        """从数据库加载任务"""
        self.tasks = self.db.load_tasks()
    
    def _get_next_id(self) -> int:
        """获取下一个任务ID（单调递增，永不复用）

        Windows 侧的任务实体名是 `TaskScheduler-<id>-<name>`，把 id 编进了名字里。
        若 id 会被复用（早期的 max(id)+1 在删除任务后就会复用），那么一次部分失败的
        删除（例如 DB 行已删但 schtasks /Delete 没成功）留下的旧实体，会与新任务
        产生同 id 的残留，日后排查时极易误判。

        因此用「高水位线」保证 id 单调：取文件里记录的上一个已发号与现存最大 id
        的较大者 +1，并把新号写回文件。不使用数据库自增，是为了不动既有表结构。
        """
        counter_file = self.config_dir / "id_counter"
        last = 0
        try:
            if counter_file.exists():
                last = int(counter_file.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            last = 0
        current_max = max((task.id for task in self.tasks), default=0)
        nxt = max(last, current_max) + 1
        try:
            # 原子写：先写临时文件再替换，避免中途崩溃留下半个数字
            tmp = counter_file.with_suffix(".tmp")
            tmp.write_text(str(nxt), encoding="utf-8")
            os.replace(tmp, counter_file)
        except OSError as e:
            SafePrint.warn(f"无法持久化 id 高水位线({e})；本次 id={nxt}，重启后可能复用")
        return nxt
    
    @staticmethod
    def _fmt_ts(dt: datetime) -> str:
        """兼容保留：委托给模块级 _fmt_ts（格式定义只应有一处）"""
        return _fmt_ts(dt)

    def _windows_next_run(self, task: Task) -> Optional[str]:
        """以 Windows 的 StartBoundary + DaysInterval 为权威源推算 next_run。

        为什么不从 created_at 推：Windows 的网格锚点其实是 **StartBoundary**（整点），
        而任务的 created_at 带时分秒，两者网格对不齐 —— 重建任务后曾出现 DB 记
        2026-09-16 而 Windows 实为 2026-09-15 的差一天偏差。这里直接读 XML 的
        权威输入复算，保证 DB 与系统一致（该值用于展示与丢失检测）。
        读不到时返回 None，由调用方回退到 _calculate_next_run。
        """
        if self.platform != Platform.WINDOWS:
            return None
        if task.schedule_type == "startup":
            # ONLOGON 触发器没有 StartBoundary 网格，交给 Windows 自己决定
            return None
        task_name = f"TaskScheduler-{task.id}-{task.name.replace(' ', '_')}"
        try:
            r = subprocess.run(['schtasks', '/Query', '/TN', task_name, '/XML'],
                               capture_output=True, encoding='utf-8', errors='replace')
            if r.returncode != 0 or not r.stdout:
                return None
            m = re.search(r"<StartBoundary>([^<]+)</StartBoundary>", r.stdout)
            if not m:
                return None
            anchor = datetime.fromisoformat(m.group(1))
            di = re.search(r"<DaysInterval>(\d+)</DaysInterval>", r.stdout)
            now = datetime.now()
            if di:                                   # every_days：按 DaysInterval 递推
                step = timedelta(days=max(1, int(di.group(1))))
                cand = anchor
                if cand <= now:
                    cand += step * ((now - cand) // step + 1)
                return cand.isoformat()
            # daily/weekly/monthly：XML 无 Interval 节点时退回"取时刻、今天或明天"
            cand = now.replace(hour=anchor.hour, minute=anchor.minute,
                               second=0, microsecond=0)
            if cand <= now:
                cand += timedelta(days=1)
            return cand.isoformat()
        except Exception:                                          # noqa: BLE001
            return None

    def _sync_next_run_with_windows(self, task: Task) -> bool:
        """把 DB 的 next_run 对齐到 Windows 权威值；有变化则写库。返回是否已更新。

        暂停的任务没有触发计划，不回填：否则库里会留下“暂停中却仍有 next_run”
        的陈旧值（显示层虽已屏蔽，但导出/直接读库会被误导）。
        """
        if task.status != "active":
            return False
        wn = self._windows_next_run(task)
        if not wn or wn == task.next_run:
            return False
        task.next_run = wn
        self.db.save_task(task)
        return True

    def _calculate_next_run(self, task: Task) -> Optional[str]:
        """计算下次执行时间"""
        now = datetime.now()
        
        try:
            if task.schedule_type == "startup":
                # 开机/登录触发没有日历网格，无法推算具体时间点
                return None

            if task.schedule_type == "hourly":
                next_run = now + timedelta(hours=1)
                return next_run.replace(second=0, microsecond=0).isoformat()
            
            elif task.schedule_type == "daily":
                time_str = task.schedule_value
                hour, minute = map(int, time_str.split(':'))
                next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_run <= now:
                    next_run += timedelta(days=1)
                return next_run.isoformat()
            
            elif task.schedule_type == "every_days":
                days_part, time_str = task.schedule_value.split('|')
                days = max(1, int(days_part))
                hour, minute = map(int, time_str.split(':'))
                # Windows 的 DaysInterval 以任务创建时的 StartBoundary 为锚点按 N 天
                # 递推，所以这里也用 created_at 的日期做网格，而不是按 last_run 推算
                # （last_run 含手动执行，会把下次执行时间算晚，DB 与系统任务就对不上了）
                step = timedelta(days=days)
                anchor = now
                if task.created_at:
                    try:
                        anchor = datetime.fromisoformat(task.created_at)
                    except ValueError:
                        anchor = now
                candidate = datetime(anchor.year, anchor.month, anchor.day, hour, minute, 0, 0)
                if candidate <= now:
                    # 直接跳到 now 之后最近的网格点，避免逐日循环
                    jumps = (now - candidate) // step + 1
                    candidate += step * jumps
                while candidate <= now:
                    candidate += step
                return candidate.isoformat()
            
            elif task.schedule_type == "weekly":
                parts = task.schedule_value.split()
                day_map = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
                target_day = day_map.get(parts[0].lower(), 0)
                time_str = parts[1]
                hour, minute = map(int, time_str.split(':'))
                
                days_ahead = target_day - now.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                
                next_run = now + timedelta(days=days_ahead)
                next_run = next_run.replace(hour=hour, minute=minute, second=0, microsecond=0)
                return next_run.isoformat()
            
            elif task.schedule_type == "monthly":
                parts = task.schedule_value.split()
                day = int(parts[0])
                time_str = parts[1]
                hour, minute = map(int, time_str.split(':'))
                
                # 处理 29/30/31 日在短月份不存在的情况：顺延到下一个月
                for month_offset in range(0, 13):
                    year = now.year
                    month = now.month + month_offset
                    while month > 12:
                        month -= 12
                        year += 1
                    max_day = calendar.monthrange(year, month)[1]
                    if day > max_day:
                        continue
                    candidate = datetime(year, month, day, hour, minute, 0, 0)
                    if candidate <= now:
                        continue
                    return candidate.isoformat()
                return None
            
            elif task.schedule_type == "interval":
                value = task.schedule_value
                if value.endswith('m'):
                    minutes = int(value[:-1])
                    next_run = now + timedelta(minutes=minutes)
                elif value.endswith('h'):
                    hours = int(value[:-1])
                    next_run = now + timedelta(hours=hours)
                elif value.endswith('d'):
                    days = int(value[:-1])
                    next_run = now + timedelta(days=days)
                else:
                    next_run = now + timedelta(hours=1)
                return next_run.isoformat()
            
        except Exception as e:
            SafePrint.warn(f"无法计算下次执行时间: {e}")
        
        return None
    
    def add_task(self, name: str, command: str, schedule_type: str,
                 schedule_value: str, **kwargs) -> Optional[Task]:
        """添加新任务"""
        # 检查名称是否已存在
        if any(task.name == name for task in self.tasks):
            SafePrint.error(f"任务 '{name}' 已存在")
            return None
        
        task = Task(
            id=self._get_next_id(),
            name=name,
            command=command,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            **kwargs
        )
        
        task.next_run = self._calculate_next_run(task)
        
        # 保存到数据库
        if not self.db.save_task(task):
            SafePrint.error("保存任务到数据库失败")
            return None
        
        self.tasks.append(task)
        
        # 设置系统级调度器（含回读校验，创建没落盘时会自动重试一次）
        scheduler_result = self._ensure_system_task(task)
        if not scheduler_result:
            SafePrint.warn("系统调度器设置失败，但任务已保存到数据库")
        
        return task
    
    def update_task(self, name: str, *, new_name=_UNSET, command=_UNSET,
                    schedule_type=_UNSET, schedule_value=_UNSET, status=_UNSET,
                    group=_UNSET, depends_on=_UNSET, working_dir=_UNSET,
                    env_vars=_UNSET, notify_on_success=_UNSET,
                    notify_on_fail=_UNSET, slack_webhook=_UNSET,
                    email=_UNSET, retry_count=_UNSET, retry_delay=_UNSET,
                    dry_run: bool = False) -> bool:
        """就地修改既有任务（任一字段），改完重建系统计划任务并重算 next_run。

        只动传入的字段，未传的保持原值；`_UNSET` 哨兵用于区分“没传”与“传了空值”。
        与 remove + add 的区别：保住 id 与 run_count/last_run 历史，系统任务实体名
        也不漂移（`--name` 改名的除外，那种先删旧实体再建新的）。

        - 换调度（daily → every_days 等）时重算 next_run；every_days 以旧 next_run
          的日期网格 + 新的 N 天步长递推，保住“每 N 天”的相位，不整体挪到今天。
        - 暂停的任务改完仍是暂停：实体照建但禁用，库里不留 next_run。
        - 改名会连带改掉其它任务的 depends_on 引用。
        """
        task = next((t for t in self.tasks if t.name == name), None)
        if task is None:
            SafePrint.error(f"任务 '{name}' 不存在")
            return False

        draft = copy.copy(task)          # 全部改动先落在副本上，失败不污染内存
        changes: List[str] = []

        def _set(field_name: str, label: str, value) -> None:
            if getattr(draft, field_name) != value:
                changes.append(f"{label}: {getattr(draft, field_name)} -> {value}")
                setattr(draft, field_name, value)

        if new_name is not _UNSET:
            if not isinstance(new_name, str) or not new_name.strip():
                SafePrint.error("新任务名不能为空")
                return False
            new_name = new_name.strip()
            if new_name != draft.name:
                if any(t.name == new_name for t in self.tasks):
                    SafePrint.error(f"任务 '{new_name}' 已存在")
                    return False
                _set('name', '名称', new_name)

        if command is not _UNSET:
            if not isinstance(command, str) or not command.strip():
                SafePrint.error("命令不能为空")
                return False
            _set('command', '命令', command.strip())

        # 可选字符串字段：传空字符串或 None 表示清空
        for field_name, label, value in (
                ('group', '分组', group),
                ('depends_on', '依赖', depends_on),
                ('working_dir', '工作目录', working_dir),
                ('notify_on_success', '成功通知', notify_on_success),
                ('notify_on_fail', '失败通知', notify_on_fail),
                ('slack_webhook', 'Slack Webhook', slack_webhook),
                ('email', '通知邮箱', email)):
            if value is _UNSET:
                continue
            _set(field_name, label, (value or '').strip() or None)

        if env_vars is not _UNSET:
            if not isinstance(env_vars, dict):
                SafePrint.error("环境变量应为 KEY=VALUE 形式")
                return False
            _set('env_vars', '环境变量', {str(k): str(v) for k, v in env_vars.items()})

        for field_name, label, value in (
                ('retry_count', '重试次数', retry_count),
                ('retry_delay', '重试间隔(秒)', retry_delay)):
            if value is _UNSET:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                SafePrint.error(f"{label}必须是整数")
                return False
            if number < 0:
                SafePrint.error(f"{label}不能为负数")
                return False
            _set(field_name, label, number)

        if status is not _UNSET:
            if status not in ('active', 'paused'):
                SafePrint.error("--status 只能是 active 或 paused")
                return False
            _set('status', '状态', status)

        if (schedule_type is _UNSET) != (schedule_value is _UNSET):
            SafePrint.error("修改调度需要同时给出类型与值")
            return False
        if schedule_type is not _UNSET:
            err = _validate_schedule_value(schedule_type, schedule_value)
            if err:
                SafePrint.error(f"调度时间无效: {err}")
                return False
            old_desc = format_schedule_str(task)
            new_desc = format_schedule_str(Task(id=task.id, name=task.name,
                                                command=task.command,
                                                schedule_type=schedule_type,
                                                schedule_value=schedule_value))
            if (schedule_type, schedule_value) != (task.schedule_type,
                                                   task.schedule_value):
                changes.append(f"调度: {old_desc} -> {new_desc}")
                draft.schedule_type = schedule_type
                draft.schedule_value = schedule_value

        if not changes:
            SafePrint.info(f"任务 '{task.name}' 没有需要修改的字段")
            return True

        # ---- 重算 next_run ----
        old_next_run = task.next_run
        schedule_changed = (draft.schedule_type, draft.schedule_value) != (
            task.schedule_type, task.schedule_value)
        if draft.status == 'paused':
            draft.next_run = None
        elif schedule_changed:
            if draft.schedule_type == 'every_days' and old_next_run:
                draft.next_run = self._every_days_next_run(draft, old_next_run)
            else:
                draft.next_run = self._calculate_next_run(draft)
        elif task.status == 'paused':
            # 从暂停恢复：重新排期，不沿用暂停时被清空的 next_run
            draft.next_run = self._calculate_next_run(draft)

        if dry_run:
            SafePrint.info("[DRY-RUN] 将修改:")
            for line in changes:
                SafePrint.info(f"  - {line}")
            if draft.next_run:
                SafePrint.info(f"  - 下次执行: {draft.next_run[:16]}")
            return True

        if not self.db.save_task(draft):
            SafePrint.error("保存修改失败")
            return False

        # 改名后系统实体名也变了：先删旧实体，再按新名字重建
        if draft.name != task.name:
            self._remove_system_scheduler(task)

        anchor = None
        if (draft.status == 'active' and draft.next_run
                and draft.schedule_type in ('daily', 'every_days', 'weekly', 'monthly')):
            try:
                anchor = datetime.fromisoformat(draft.next_run)
            except ValueError:
                anchor = None

        system_ok = self._ensure_system_task(draft, anchor=anchor)

        # 暂停任务：实体处于禁用态，库里不留 next_run（与 pause_task 语义一致）
        if draft.status == 'paused' and draft.next_run is not None:
            draft.next_run = None
            self.db.save_task(draft)

        self.tasks[self.tasks.index(task)] = draft

        if draft.name != task.name:
            for other in self.tasks:
                if other is not draft and other.depends_on == task.name:
                    other.depends_on = draft.name
                    self.db.save_task(other)
                    SafePrint.info(f"已同步 '{other.name}' 的依赖引用: "
                                   f"{task.name} -> {draft.name}")

        for line in changes:
            if system_ok:
                SafePrint.success(line)
            else:
                SafePrint.info(f"  - {line}")

        snapshot = self._windows_entity_snapshot(draft)
        if system_ok:
            if snapshot:
                SafePrint.info(f"系统计划任务: {snapshot}")
            SafePrint.success(f"任务 '{draft.name}' 已更新"
                              + (f"（下次执行 {draft.next_run[:16]}）" if draft.next_run else ""))
            return True

        # 系统侧没能同步：库里已改，明确报错而不是静默“成功”
        if snapshot:
            SafePrint.info(f"当前系统计划任务: {snapshot}")
        SafePrint.error("系统计划任务未能同步（数据库已更新）；"
                        "请用管理员 PowerShell 执行 `schedule repair` 重建")
        return False

    def _every_days_next_run(self, task: Task, old_next_run: str) -> Optional[str]:
        """every_days 改时间时，沿用旧 next_run 的日期网格 + 新调度的时间。

        直接调 _calculate_next_run 会按 created_at 重算网格，相位可能整体挪位
        （14 天体检之类的任务被挪到明天）。这里以旧 next_run 的日期为锚点，按
        新的 N 天步长递推到 now 之后。
        """
        try:
            old = datetime.fromisoformat(old_next_run)
            days_part, time_part = task.schedule_value.split('|')
            step = timedelta(days=max(1, int(days_part)))
            hour, minute = map(int, time_part.split(':'))
        except (TypeError, ValueError):
            return self._calculate_next_run(task)
        candidate = old.replace(hour=hour, minute=minute, second=0, microsecond=0)
        now = datetime.now()
        while candidate <= now:
            candidate += step
        return candidate.isoformat()

    def _setup_system_scheduler(self, task: Task,
                                anchor: Optional[datetime] = None) -> bool:
        """设置系统级定时任务

        anchor：仅 Windows 的日历型任务用得上 —— 钉住 StartBoundary 以保住
        “每 N 天”的相位（update/repair 重建时传入新的下次执行时间）。
        macOS/Linux 由各自触发器定义，忽略该参数。
        """
        try:
            if self.platform == Platform.WINDOWS:
                return self._setup_windows_task(task, anchor=anchor)
            elif self.platform == Platform.MACOS:
                return self._setup_macos_task(task)
            else:
                return self._setup_linux_task(task)
        except Exception as e:
            SafePrint.error(f"系统调度器设置失败: {e}")
            return False
    
    def _setup_windows_task(self, task: Task, anchor: Optional[datetime] = None,
                            sync_db: bool = True) -> bool:
        """设置 Windows Task Scheduler

        anchor：可选的网格锚点。重建任务时传入库里记录的下次执行时间，
        就能保住“每 N 天”任务的相位（否则 StartBoundary 会被重置成今天，
        14 天体检之类的任务会被整体挪走）。

        sync_db：是否在建好后把 Windows 网格回填进库。renumber 期间必须传
        False —— 那时库里还是旧 id，回填会按新 id 再插一行（name 唯一，
        INSERT OR REPLACE 会顺手删掉旧行），造成"序号还没改、行先丢了"。
        """
        try:
            task_name = self._windows_task_name(task)
            
            # 删除已存在的任务
            subprocess.run(['schtasks', '/Delete', '/TN', task_name, '/F'],
                         capture_output=True, encoding='utf-8', errors='replace')
            
            # 构建 schtasks 命令
            schedule_map = {
                'hourly': ['/SC', 'HOURLY'],
                'daily': ['/SC', 'DAILY'],
                'every_days': ['/SC', 'DAILY'],
                'weekly': ['/SC', 'WEEKLY'],
                'monthly': ['/SC', 'MONTHLY'],
                'startup': ['/SC', 'ONLOGON']
            }
            
            if task.schedule_type == 'interval':
                ival = task.schedule_value
                if ival.endswith('m'):
                    schedule_cmd_parts = ['/SC', 'MINUTE', '/MO', ival[:-1]]
                elif ival.endswith('h'):
                    schedule_cmd_parts = ['/SC', 'HOURLY', '/MO', ival[:-1]]
                else:
                    schedule_cmd_parts = ['/SC', 'DAILY', '/MO', (ival.rstrip('d') or '1')]
            else:
                schedule_cmd_parts = schedule_map.get(task.schedule_type, ['/SC', 'DAILY'])
            cmd_parts = ['schtasks', '/Create', '/TN', task_name] + schedule_cmd_parts
            
            # 添加时间参数
            if task.schedule_type == 'daily':
                cmd_parts.extend(['/ST', task.schedule_value])
            elif task.schedule_type == 'every_days':
                days_part, time_part = task.schedule_value.split('|')
                cmd_parts.extend(['/MO', days_part, '/ST', time_part])
            elif task.schedule_type == 'weekly':
                parts = task.schedule_value.split()
                day = parts[0][:3].upper()
                cmd_parts.extend(['/D', day, '/ST', parts[1]])
            elif task.schedule_type == 'monthly':
                parts = task.schedule_value.split()
                day = parts[0]
                cmd_parts.extend(['/D', day, '/ST', parts[1]])
            elif task.schedule_type == 'startup':
                # 登录后延迟 10 秒再拉起：等网络栈与用户 PATH（node）就绪
                cmd_parts.extend(['/DELAY', '0000:10'])
            
            # 构建执行命令：入口只应有一处定义，统一交给 _windows_run_cmd
            run_cmd = self._windows_run_cmd(task)
            
            cmd_parts.extend(['/TR', run_cmd, '/F'])
            
            # 执行命令
            result = subprocess.run(cmd_parts, capture_output=True, text=True, encoding='utf-8', errors='replace')
            
            if result.returncode == 0:
                SafePrint.success(f"Windows Task Scheduler 任务已创建: {task_name}")
                # 先钉网格相位，再改电源设置（后者会 Set-ScheduledTask，顺序无关但先做更直观）
                if anchor is not None:
                    self._set_windows_start_boundary(task_name, anchor)
                # schtasks 会继承 Windows 默认设置（用电池不启动、错过不补跑），
                # 必须显式改写，否则任务会"已启用但到点不执行"。
                # 设置没落盘就别报成功：调用方据此决定是否重试/报错。
                settings_ok = self._apply_windows_task_settings(
                    task, enabled=(task.status == "active"))
                # 让 DB 的 next_run 对齐 Windows 权威网格（StartBoundary + DaysInterval）。
                # 用 created_at 推算会与系统差一天（整点锚点 vs 带时分秒的创建时刻）。
                # 暂停的任务没有触发计划，绝不把网格回填进库（否则库里会留下
                # “暂停中却仍有 next_run”的陈旧值，与 pause_task 的语义矛盾）。
                if sync_db:
                    if task.status == "active":
                        if self._sync_next_run_with_windows(task):
                            SafePrint.info(f"next_run 已按 Windows 网格校准为 {task.next_run}")
                    elif task.next_run is not None:
                        task.next_run = None
                        self.db.save_task(task)
                return settings_ok
            else:
                SafePrint.error(f"Windows 任务创建失败: {result.stderr}")
                return False
                
        except Exception as e:
            SafePrint.error(f"Windows 任务设置失败: {e}")
            return False

    @staticmethod
    def _iso_duration_seconds(value: str) -> Optional[int]:
        """把 Task Scheduler 的 ISO8601 时长（PT10M / PT1H30M / PT30S）解析成秒。

        解析不出来返回 None（校验时按“不符合”处理）。
        """
        if not value:
            return None
        m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value.strip())
        if not m or not any(m.groups()):
            return None
        hours, minutes, seconds = (int(x) if x else 0 for x in m.groups())
        return hours * 3600 + minutes * 60 + seconds

    def _windows_restart_plan(self, task: Task) -> Tuple[int, int]:
        """任务在 Windows 侧的失败重启策略：(次数, 间隔秒)。

        次数取自 task.retry_count；间隔取自 task.retry_delay，但 Windows 的
        重启间隔**最小 1 分钟**，更小的值向上取整到 60 秒，否则
        Set-ScheduledTask 直接报错。次数为 0 表示不配置失败重启。
        """
        count = max(0, int(task.retry_count or 0))
        if count == 0:
            return 0, 0
        return count, max(60, int(task.retry_delay or 60))

    def _apply_windows_task_settings(self, task: Task, enabled: bool = True) -> bool:
        """修正 Windows 任务的电源、补偿与失败重启设置

        `schtasks /Create` 生成的任务会带上 Windows 的默认设置：
        - DisallowStartIfOnBatteries=True   用电池时直接不启动
        - StopIfGoingOnBatteries=True       切到电池就把任务杀掉
        - StartWhenAvailable=False          到点时机器关机/睡眠就永久丢弃该次执行

        三者叠加的结果就是"任务状态是已启用，但到点不跑"，任务计划程序里
        显示最后一次结果为 0x800710E0（操作员或管理员拒绝了请求）。
        这里统一改成：允许电池启动、切电池不终止、错过的执行尽快补跑。

        同时把任务的失败重试参数写进「如果任务失败，尝试重新启动」：
        次数 = retry_count、间隔 = retry_delay（最少 60 秒）。这样任务计划程序里
        能看到重试配置；实际重试仍由技能内部执行（internal-run 正常失败时以 0
        退出），只有进程异常崩溃（非 0 退出）才会触发系统级重启，不会双重重试。

        注意 `Set-ScheduledTask -Settings` 会连 Settings.Enabled 一起覆盖成默认
        True，所以必须按调用方给的状态显式 Enable/Disable，否则暂停的任务会被
        静默启用，与数据库中的 paused 状态漂移。
        """
        if self.platform != Platform.WINDOWS:
            return True

        task_name = self._windows_task_name(task)
        restart_count, restart_seconds = self._windows_restart_plan(task)
        restart_arg = ""
        if restart_count:
            restart_arg = (f" -RestartCount {restart_count} "
                           f"-RestartInterval (New-TimeSpan -Seconds {restart_seconds})")
        else:
            restart_arg = " -RestartCount 0"

        safe_name = task_name.replace("'", "''")
        state_cmd = 'Enable-ScheduledTask' if enabled else 'Disable-ScheduledTask'
        ps_script = (
            "$ErrorActionPreference='Stop';"
            "$name = '" + safe_name + "';"
            "$s = New-ScheduledTaskSettingsSet "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable "
            "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 72)"
            + restart_arg + ";"
            "Set-ScheduledTask -TaskName $name -Settings $s | Out-Null;"
            f"{state_cmd} -TaskName $name | Out-Null;"
            # 回读校验：命令返回 0 只说明调用没报错，不等于设置真的落盘。
            # 这里把实际生效值读回来，失败即判为未生效，避免"任务已建但设置没应用"。
            "$t = Get-ScheduledTask -TaskName $name;"
            "$x = [xml](Export-ScheduledTask -TaskName $name);"
            "$r = $x.Task.Settings.RestartOnFailure;"
            "$rst = 'none';"
            "if ($r -and $r.Interval) { $rst = [string]$r.Interval + '/' + [string]$r.Count };"
            "$want = 'DIB=' + [string]$x.Task.Settings.DisallowStartIfOnBatteries + "
            "' SIB=' + [string]$x.Task.Settings.StopIfGoingOnBatteries + "
            "' SWA=' + [string]$x.Task.Settings.StartWhenAvailable + "
            "' MIP=' + [string]$x.Task.Settings.MultipleInstancesPolicy + "
            "' RST=' + $rst;"
            "'VERIFY state=' + [string]$t.State + ' ' + $want"
        )
        try:
            result = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_script],
                capture_output=True, text=True, encoding='utf-8', errors='replace')
        except FileNotFoundError:
            SafePrint.warn("未找到 powershell，未能修正 Windows 任务电源设置")
            return False

        # 校验回读结果：设置项都符合预期才算成功
        verify_line = ""
        for line in (result.stdout or "").splitlines():
            if line.strip().startswith("VERIFY "):
                verify_line = line.strip()
                break

        bad = []
        if not verify_line:
            bad.append("未取得回读结果")
        else:
            parts = dict(
                (kv.split("=", 1) for kv in verify_line.split()[1:] if "=" in kv)
            )
            if parts.get("DIB") != "false":
                bad.append(f"DisallowStartIfOnBatteries={parts.get('DIB')}")
            if parts.get("SIB") != "false":
                bad.append(f"StopIfGoingOnBatteries={parts.get('SIB')}")
            if parts.get("SWA") != "true":
                bad.append(f"StartWhenAvailable={parts.get('SWA')}")
            if parts.get("MIP") != "IgnoreNew":
                bad.append(f"MultipleInstancesPolicy={parts.get('MIP')}")
            # 失败重启：次数/间隔都要和库里一致
            rst = parts.get("RST") or "none"
            got_interval, _, got_count = rst.partition("/")
            if restart_count:
                got_seconds = self._iso_duration_seconds(got_interval)
                if got_count != str(restart_count) or got_seconds != restart_seconds:
                    bad.append(
                        f"失败重启 {rst} != {restart_seconds}/{restart_count}")
            elif got_count not in ("", "0"):
                bad.append(f"失败重启 {rst}（期望关闭）")
            # 状态校验：Enable/Disable 的意图是「别把状态弄反」，而不是「必须在跑」。
            # ⚠️ 已启用但当前空闲的 ScheduledTask，其 State 是 `Ready`
            # （只有正在执行的那几秒才是 `Running`）。此前把期望硬写成 `Running`，
            # 于是每次 add/enable 都会误报「电源设置未生效」——实测踩到（2026-09-15）。
            st = parts.get("state")
            if enabled:
                if not st or st == "Disabled":
                    bad.append(f"state={st}（已启用任务不应是 Disabled）")
            elif st != "Disabled":
                bad.append(f"state={st}（期望 Disabled）")

        if not bad:
            SafePrint.success(
                f"Windows 任务设置已回读校验通过: {task_name}"
                + (f" [{verify_line}]" if verify_line else "")
            )
            return True

        detail = "; ".join(bad)
        err = (result.stderr or "").strip()
        SafePrint.warn(
            f"Windows 任务电源设置**未生效**（任务可能'已启用但到点不跑'）: "
            f"{task_name} -> {detail}"
            + (f" | stderr: {err}" if err else "")
            + (f" | rc={result.returncode}" if result.returncode != 0 else "")
        )
        if "Access is denied" in err or "拒绝访问" in err or "0x80070005" in err:
            SafePrint.warn(
                "提示: 该实体由更高权限的上下文创建，当前进程改不动（连删除也会被拒）；"
                "请在管理员 PowerShell 里执行 `schedule repair`，"
                "或 `schedule update <任务名> --retry-count N --retry-delay S` 重写这组设置"
            )
        return False
    
    # ---------------- 自检 / 修复（系统任务被误删后一键重建） ----------------

    def _windows_task_name(self, task: Task) -> str:
        """系统任务实体名。把库里的 id 编进名字，才能反查出「这条实体属于谁」。"""
        return f"TaskScheduler-{task.id}-{task.name.replace(' ', '_')}"

    def _windows_run_cmd(self, task: Task) -> str:
        """计划任务里存的唯一入口：pythonw schedule.py internal-run <id>

        用 pythonw.exe（无控制台）而非 python.exe：计划任务由 Task Scheduler
        启动时没有控制台，python.exe 会弹出一个命令窗口并在任务执行期间一直
        亮着。找不到 pythonw 时回退 python.exe（仅美观受影响）。

        【注意】pythonw 下 sys.executable 也是 pythonw.exe，会让子进程一并变成
        无窗口进程而丢失管道输出 —— 故 _execute_task 里另有"子进程强制用
        python.exe"的处理。

        业务命令只存在数据库里，系统任务只记 id：换脚本路径、改命令都不必动
        Windows 侧，`repair` 也能据此判断实体是否过期。
        """
        python_exe = sys.executable
        pythonw_exe = Path(python_exe).with_name("pythonw.exe")
        launcher_exe = str(pythonw_exe) if pythonw_exe.exists() else python_exe
        script_path = Path(__file__).resolve()
        return f'"{launcher_exe}" "{script_path}" internal-run {task.id}'

    @staticmethod
    def _norm_cmd(s: str) -> str:
        """比较入口命令时忽略大小写、引号与空白差异（schtasks 会重排空格）。"""
        return re.sub(r"\s+", " ", (s or "").replace('"', " ")).strip().lower()

    def _query_windows_task(self, task_name: str) -> Optional[Dict[str, str]]:
        """读取系统任务现状；不存在返回 None。

        只解析 ASCII 字段（时间/命令/设置），不碰中文名称，避开控制台编码坑。
        """
        r = subprocess.run(['schtasks', '/Query', '/TN', task_name, '/XML'],
                           capture_output=True, encoding='utf-8', errors='replace')
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        xml = r.stdout

        def grab(pattern: str, default: str = "") -> str:
            m = re.search(pattern, xml)
            return m.group(1) if m else default

        # 失败重启策略只在 <RestartOnFailure> 块里取：<Interval> 在 <Repetition>
        # 里也有，必须限定作用域，否则会拿错值。
        restart_block = re.search(r"<RestartOnFailure>(.*?)</RestartOnFailure>", xml, re.S)
        restart_xml = restart_block.group(1) if restart_block else ""

        def grab_restart(pattern: str) -> str:
            m = re.search(pattern, restart_xml)
            return m.group(1) if m else ""

        return {
            "command": grab(r"<Command>([^<]*)</Command>"),
            "arguments": grab(r"<Arguments>([^<]*)</Arguments>"),
            "start_boundary": grab(r"<StartBoundary>([^<]*)</StartBoundary>"),
            "days_interval": grab(r"<DaysInterval>(\d+)</DaysInterval>"),
            "dib": grab(r"<DisallowStartIfOnBatteries>([^<]*)</DisallowStartIfOnBatteries>").lower(),
            "sib": grab(r"<StopIfGoingOnBatteries>([^<]*)</StopIfGoingOnBatteries>").lower(),
            "swa": grab(r"<StartWhenAvailable>([^<]*)</StartWhenAvailable>").lower(),
            "mip": grab(r"<MultipleInstancesPolicy>([^<]*)</MultipleInstancesPolicy>"),
            "restart_interval": grab_restart(r"<Interval>([^<]*)</Interval>"),
            "restart_count": grab_restart(r"<Count>(\d+)</Count>"),
        }

    def _query_windows_state(self, task_name: str) -> str:
        """取任务状态（Ready/Running/Disabled）；读不到返回空串。

        不用 schtasks 的 Status 字段：它是本地化文本（中文系统上是“已禁用”），
        而 Get-ScheduledTask 返回枚举名，跨语言环境稳定。
        """
        safe = task_name.replace("'", "''")
        ps = ("$ErrorActionPreference='Stop';"
              f"$t = Get-ScheduledTask -TaskName '{safe}';"
              "[string]$t.State")
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
        except FileNotFoundError:
            return ""
        if r.returncode != 0:
            return ""
        out = (r.stdout or "").strip()
        return out.splitlines()[-1].strip() if out else ""

    def _set_windows_start_boundary(self, task_name: str, anchor: datetime) -> bool:
        """把触发器 StartBoundary 钉到指定时间，保住“每 N 天”的相位。

        不用 schtasks /SD：它按本地化日期格式解析，中文系统上 MM/DD/YYYY 会翻车；
        PowerShell 直接写 ISO 时间，跨语言环境一致。写完回读校验。
        """
        safe = task_name.replace("'", "''")
        iso = anchor.strftime("%Y-%m-%dT%H:%M:%S")
        ps = ("$ErrorActionPreference='Stop';"
              f"$n = '{safe}';"
              "$t = Get-ScheduledTask -TaskName $n;"
              f"$t.Triggers[0].StartBoundary = '{iso}';"
              "Set-ScheduledTask -TaskName $n -Trigger $t.Triggers | Out-Null;"
              "(Get-ScheduledTask -TaskName $n).Triggers[0].StartBoundary")
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
        except FileNotFoundError:
            SafePrint.warn("未找到 powershell，无法校准触发时间")
            return False
        out = (r.stdout or "").strip()
        got = out.splitlines()[-1].strip() if out else ""
        if got.startswith(iso):
            return True
        SafePrint.warn(f"触发时间校准失败: 期望 {iso}，实际 {got or '?'}"
                       + (f" | {(r.stderr or '').strip()}" if r.returncode != 0 else ""))
        return False

    def _repair_anchor(self, task: Task) -> Optional[datetime]:
        """用库里 next_run 反推一个不早于现在的网格锚点。

        只对日历型调度有意义：daily 用当天/明天，every_days 按 DaysInterval 递推。
        startup/hourly/interval 没有日历网格，返回 None 交给系统自己排。
        """
        if task.schedule_type not in ("daily", "every_days", "weekly", "monthly"):
            return None
        if not task.next_run:
            return None
        try:
            anchor = datetime.fromisoformat(task.next_run)
        except ValueError:
            return None
        now = datetime.now()
        if task.schedule_type == "every_days":
            try:
                step = max(1, int(task.schedule_value.split("|")[0]))
            except (ValueError, IndexError):
                step = 1
            while anchor <= now:
                anchor += timedelta(days=step)
        if anchor <= now:
            return None
        return anchor

    def _windows_task_problems(self, task: Task, info: Dict[str, str],
                               state: str) -> Tuple[List[str], List[str]]:
        """把系统实体与数据库对不上的地方分类：需整条重建 / 只需改设置。"""
        rebuild: List[str] = []
        fix: List[str] = []

        want_cmd = self._windows_run_cmd(task)
        actual_cmd = f"{info.get('command', '')} {info.get('arguments', '')}".strip()
        if self._norm_cmd(actual_cmd) != self._norm_cmd(want_cmd):
            rebuild.append("入口命令不符")

        sb = info.get("start_boundary", "")
        got_time = sb[11:16] if len(sb) >= 16 else ""
        if task.schedule_type == "daily":
            if got_time != task.schedule_value.strip():
                rebuild.append(f"触发时间 {got_time or '?'} != {task.schedule_value.strip()}")
        elif task.schedule_type == "every_days":
            try:
                days_part, time_part = task.schedule_value.split("|")
            except ValueError:
                days_part, time_part = "1", ""
            if (info.get("days_interval") or "") != str(days_part):
                rebuild.append(f"间隔 {info.get('days_interval') or '?'} 天 != {days_part}")
            if time_part and got_time != time_part:
                rebuild.append(f"触发时间 {got_time or '?'} != {time_part}")
        elif task.schedule_type in ("weekly", "monthly"):
            parts = task.schedule_value.split()
            if len(parts) == 2 and got_time != parts[1]:
                rebuild.append(f"触发时间 {got_time or '?'} != {parts[1]}")
        # startup(ONLOGON) 没有日历网格，不校验时间

        if info.get("dib") != "false":
            fix.append(f"DisallowStartIfOnBatteries={info.get('dib') or '?'}")
        if info.get("sib") != "false":
            fix.append(f"StopIfGoingOnBatteries={info.get('sib') or '?'}")
        if info.get("swa") != "true":
            fix.append(f"StartWhenAvailable={info.get('swa') or '?'}")
        if info.get("mip") != "IgnoreNew":
            fix.append(f"MultipleInstancesPolicy={info.get('mip') or '?'}")

        # 失败重启策略：必须与库里的 retry_count / retry_delay 一致
        # （间隔在 Windows 侧最小 1 分钟，_windows_restart_plan 已按此取整）
        restart_count, restart_seconds = self._windows_restart_plan(task)
        got_count = info.get("restart_count") or ""
        got_interval = info.get("restart_interval") or ""
        if restart_count:
            if got_count != str(restart_count):
                fix.append(f"失败重启次数 {got_count or '未设置'} != {restart_count}")
            elif self._iso_duration_seconds(got_interval) != restart_seconds:
                fix.append(f"失败重启间隔 {got_interval or '未设置'} != {restart_seconds} 秒")
        elif got_count not in ("", "0"):
            fix.append(f"失败重启 {got_interval or '?'}/{got_count} != 关闭")

        if state:
            if task.status == "active" and state == "Disabled":
                fix.append("系统里是 Disabled，但库里是 active")
            elif task.status != "active" and state != "Disabled":
                fix.append(f"系统里是 {state}，但库里是 paused")
        return rebuild, fix

    def _windows_entity_problems(self, task: Task) -> List[str]:
        """回读系统实体，返回与库里记录不一致的项（空列表 = 一致）。

        Windows 上 schtasks /Create 返回 0 只说明命令没报错；实际可能因权限、
        杀软拦截或注册表写入失败而**没有真正落盘**，此时实体仍是旧的（或压根
        没有）。update 改完调度后必须回读一次，才能保证“改了就是改了”。
        """
        if self.platform != Platform.WINDOWS:
            return []
        tname = self._windows_task_name(task)
        info = self._query_windows_task(tname)
        if info is None:
            return ["系统里不存在"]
        rebuild, fix = self._windows_task_problems(
            task, info, self._query_windows_state(tname))
        return rebuild + fix

    def _ensure_system_task(self, task: Task,
                            anchor: Optional[datetime] = None) -> bool:
        """建/重建系统计划任务并回读校验，不一致时自动重建一次。

        返回 False 表示两次尝试后系统实体与数据库仍对不上 —— 调用方必须
        把这个结果当失败（打印原因、必要时退出码非 0），而不是“建过了就算”。
        """
        ok = self._setup_system_scheduler(task, anchor=anchor)
        problems = self._windows_entity_problems(task)
        if problems or not ok:
            if problems:
                SafePrint.warn(f"系统计划任务与数据库不一致（{'；'.join(problems)}），自动重建一次")
            else:
                SafePrint.warn("系统计划任务创建报告失败，自动重试一次")
            ok = self._setup_system_scheduler(task, anchor=anchor)
            problems = self._windows_entity_problems(task)
        if problems:
            SafePrint.error(f"系统计划任务仍未同步: {'；'.join(problems)}")
            return False
        return ok

    def _windows_entity_snapshot(self, task: Task) -> str:
        """一行摘要（触发时间/状态），给 update 用作用户可见的回读证据。"""
        if self.platform != Platform.WINDOWS:
            return ""
        tname = self._windows_task_name(task)
        info = self._query_windows_task(tname)
        if info is None:
            return "系统里不存在"
        state = self._query_windows_state(tname) or "?"
        restart_count, restart_seconds = self._windows_restart_plan(task)
        restart = (f"{restart_seconds}秒x{restart_count}" if restart_count else "关闭")
        return (f"{tname} | 触发={info.get('start_boundary') or '?'} "
                f"| 间隔={info.get('days_interval') or '-'}天 | 状态={state} "
                f"| 失败重启={restart}")

    def _windows_orphan_ids(self) -> List[int]:
        """系统里存在、数据库里已经没有的 TaskScheduler-<id>-* 的 id。

        只按 ASCII 前缀 `TaskScheduler-<数字>-` 匹配，不依赖中文名称解码。
        """
        known = {t.id for t in self.tasks}
        r = subprocess.run(['schtasks', '/Query', '/FO', 'CSV', '/NH'],
                           capture_output=True, encoding='utf-8', errors='replace')
        ids = set()
        for line in (r.stdout or "").splitlines():
            m = re.search(r"TaskScheduler-(\d+)-", line)
            if m and int(m.group(1)) not in known:
                ids.add(int(m.group(1)))
        return sorted(ids)

    def _windows_delete_tasks_by_ids(self, ids: List[int]) -> bool:
        """按 id 删除孤儿实体。在 PowerShell 里完成匹配与删除，避免名称回传的编码问题。"""
        if not ids:
            return True
        ps = ("$ids = @(" + ",".join(str(i) for i in ids) + ");"
              "Get-ScheduledTask -TaskName 'TaskScheduler-*' -ErrorAction SilentlyContinue |"
              " Where-Object { $m = [regex]::Match($_.TaskName,'^TaskScheduler-([0-9]+)-');"
              " $m.Success -and ($ids -contains [int]$m.Groups[1].Value) } |"
              " Unregister-ScheduledTask -Confirm:$false")
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
        except FileNotFoundError:
            return False
        return r.returncode == 0

    @staticmethod
    def _print_repair_table(rows: List[Tuple[str, str, str]]):
        SafePrint.print("\n" + "=" * 84)
        SafePrint.print(f"{'任务':<26}{'结果':<12}说明")
        SafePrint.print("-" * 84)
        for name, action, detail in rows:
            SafePrint.print(f"{name:<26}{action:<12}{detail}")
        SafePrint.print("=" * 84)

    def repair_tasks(self, dry_run: bool = False, prune: bool = False,
                     only: Optional[str] = None) -> bool:
        """按数据库重建/校正系统计划任务（杀毒误删后的一键恢复）。

        判定规则：
        - 实体缺失、入口命令不符、触发器与库里调度对不上 → 整条重建
        - 只有电源/补偿设置或启用状态不符 → 原地修正，不重建（保留历史与统计）
        - 库里没有对应记录的孤儿实体 → 默认只报告，--prune 才删
        """
        targets = [t for t in self.tasks if not only or t.name == only]
        if only and not targets:
            SafePrint.error(f"任务 '{only}' 不存在")
            return False

        rows: List[Tuple[str, str, str]] = []
        all_ok = True

        if self.platform != Platform.WINDOWS:
            for task in targets:
                if dry_run:
                    rows.append((task.name, "跳过", "非 Windows 平台，未检查"))
                    continue
                ok = self._setup_system_scheduler(task)
                rows.append((task.name, "已重建" if ok else "失败", "" if ok else "系统调度器设置失败"))
                all_ok = all_ok and ok
            self._print_repair_table(rows)
            return all_ok

        for task in targets:
            tname = self._windows_task_name(task)
            info = self._query_windows_task(tname)

            if info is None:
                if dry_run:
                    rows.append((task.name, "将重建", "系统里不存在"))
                    all_ok = False
                    continue
                ok = self._setup_windows_task(task, anchor=self._repair_anchor(task))
                rows.append((task.name, "已重建" if ok else "失败",
                             "系统里不存在" if ok else "重建失败，见上方错误"))
                all_ok = all_ok and ok
                continue

            state = self._query_windows_state(tname)
            rebuild, fix = self._windows_task_problems(task, info, state)

            if rebuild:
                if dry_run:
                    rows.append((task.name, "将重建", "；".join(rebuild)))
                    all_ok = False
                    continue
                ok = self._setup_windows_task(task, anchor=self._repair_anchor(task))
                rows.append((task.name, "已重建" if ok else "失败", "；".join(rebuild)))
                all_ok = all_ok and ok
                continue

            if fix:
                if dry_run:
                    rows.append((task.name, "将修正", "；".join(fix)))
                    all_ok = False
                    continue
                ok = self._apply_windows_task_settings(task, enabled=(task.status == "active"))
                rows.append((task.name, "已修正" if ok else "失败", "；".join(fix)))
                all_ok = all_ok and ok
                continue

            if not dry_run:
                if task.status == "active":
                    self._sync_next_run_with_windows(task)
                elif task.next_run is not None:
                    # 暂停任务不该有 next_run，顺手清掉陈旧值
                    task.next_run = None
                    self.db.save_task(task)
            rows.append((task.name, "正常", ""))

        orphan_ids = self._windows_orphan_ids()
        for oid in orphan_ids:
            label = f"TaskScheduler-{oid}-*"
            if prune and not dry_run:
                ok = self._windows_delete_tasks_by_ids([oid])
                rows.append((label, "已删孤儿" if ok else "删除失败", "系统里有、库里没有"))
                all_ok = all_ok and ok
            else:
                rows.append((label, "孤儿", "系统里有、库里没有（--prune 可删）"))

        self._print_repair_table(rows)
        return all_ok

    # ---------------- 序号压实（1..N 连续，系统实体同步改名） ----------------

    def _set_id_counter(self, value: int) -> bool:
        """把 id 高水位线写回 value，让下一次 add 接着 N+1，不会又跳号。"""
        counter_file = self.config_dir / "id_counter"
        try:
            tmp = counter_file.with_suffix(".tmp")
            tmp.write_text(str(value), encoding="utf-8")
            os.replace(tmp, counter_file)
            return True
        except OSError as e:
            SafePrint.warn(f"无法写入 id 高水位线({e})")
            return False

    def _rename_windows_task(self, old_id: int, new_id: int) -> bool:
        """把系统实体的序号 old_id 改成 new_id，保留触发器/设置/运行历史。

        为什么不"删除+重建"：重建会把 StartBoundary 重置到今天的网格，还会
        丢掉 Repetition 之类的开关（"登录自启"那条正挂着每 10 分钟的重复触发）。
        这里用 Register-ScheduledTask -InputObject 原样搬运实体，只把动作里的
        `internal-run <旧id>` 改写成新 id。

        实体按 `TaskScheduler-<id>-*` 前缀匹配，不把中文任务名拼进 PowerShell
        脚本：-Command 传中文有编码坑，脚本保持纯 ASCII 最稳。
        """
        ps = (
            "$ErrorActionPreference='Stop';"
            f"$old = (Get-ScheduledTask -TaskName 'TaskScheduler-{old_id}-*' "
            "-ErrorAction SilentlyContinue | Select-Object -First 1).TaskName;"
            "if (-not $old) { 'NOMATCH'; exit 0 };"
            f"$new = 'TaskScheduler-{new_id}-' + ($old -replace '^TaskScheduler-{old_id}-','');"
            "$t = Get-ScheduledTask -TaskName $old;"
            "if (-not $t.Actions -or $t.Actions.Count -lt 1) { 'NOACTION'; exit 0 };"
            f"$t.Actions[0].Arguments = ($t.Actions[0].Arguments -replace "
            f"'internal-run\\s+{old_id}\\s*$','internal-run {new_id}');"
            "Register-ScheduledTask -TaskName $new -InputObject $t -Force | Out-Null;"
            "Unregister-ScheduledTask -TaskName $old -Confirm:$false;"
            "'OK'"
        )
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
        except FileNotFoundError:
            SafePrint.warn("未找到 powershell，无法改名系统任务")
            return False
        out = (r.stdout or "").strip()
        last = out.splitlines()[-1].strip() if out else ""
        if last == "OK":
            return True
        detail = last or (r.stderr or "").strip()[:200] or f"rc={r.returncode}"
        SafePrint.warn(f"系统任务改名失败(TaskScheduler-{old_id}-* -> {new_id}): {detail}")
        return False

    @staticmethod
    def _print_renumber_table(rows: List[Tuple[str, str, str]], header: str = ""):
        SafePrint.print("\n" + "=" * 64)
        SafePrint.print(f"{header:<10}{'任务':<28}{'序号':<16}")
        SafePrint.print("-" * 64)
        for name, change, note in rows:
            SafePrint.print(f"{'':<10}{name:<28}{change:<16}{note}")
        SafePrint.print("=" * 64)

    def renumber_tasks(self, dry_run: bool = False, quiet: bool = False) -> bool:
        """把序号压实成 1..N（保持现有相对顺序，从 1 开始连续）。

        三件事必须一起做，否则 id 与系统实体就会错位：
        1) 系统侧把 `TaskScheduler-<旧id>-<名称>` 改成新号，并改写动作里的
           `internal-run <id>`；
        2) 库里改写 tasks.id 与 execution_logs.task_id（历史日志跟着走）；
        3) 重置 id 高水位线到 N，下一次 add 从 N+1 继续。

        顺序是"系统先、库后"：改名失败或重建失败时整体回滚，库保持原样，
        绝不会出现"库和系统实体各说各话"。Windows 侧优先原地改名（保相位），
        改名失败才退回按库重建。
        """
        ordered = sorted(self.tasks, key=lambda t: t.id)
        mapping = {t.id: idx for idx, t in enumerate(ordered, start=1)}
        moves = [(t, mapping[t.id]) for t in ordered if t.id != mapping[t.id]]

        if not moves:
            # 没有要挪的行，但高水位可能还停在更大的旧号上（删掉尾号就会这样）：
            # 一并对齐到 N，否则下一次 add 从 N+2 起跳，立刻又出现空号。
            self._set_id_counter(len(ordered))
            if not quiet:
                SafePrint.info(f"序号已经连续（1..{len(ordered)}），id 高水位已对齐")
            return True

        rows = [(t.name, f"{t.id} -> {nid}", "") for t, nid in moves]
        if dry_run:
            self._print_renumber_table(rows, "将调整")
            return True

        done: List[Tuple[int, int]] = []
        failure: Optional[str] = None

        if self.platform == Platform.WINDOWS:
            for task, nid in moves:
                if self._rename_windows_task(task.id, nid):
                    done.append((task.id, nid))
                    continue
                SafePrint.warn(f"{task.name}: 原地改名失败，改为按库重建实体")
                tmp = copy.copy(task)
                tmp.id = nid
                if self._setup_windows_task(tmp, anchor=self._repair_anchor(task), sync_db=False):
                    subprocess.run(['schtasks', '/Delete', '/TN', self._windows_task_name(task), '/F'],
                                   capture_output=True, encoding='utf-8', errors='replace')
                    done.append((task.id, nid))
                else:
                    failure = f"{task.name}: 改名与重建均失败"
                    break

            if failure:
                for old_id, nid in reversed(done):
                    if not self._rename_windows_task(nid, old_id):
                        SafePrint.error(
                            f"回滚失败: TaskScheduler-{nid}-* 仍指向新号，"
                            f"请运行 `schedule repair` 按库校准")
                SafePrint.error(f"序号调整已中止（数据库未改）: {failure}")
                return False
        else:
            for task, nid in moves:
                tmp = copy.copy(task)
                tmp.id = nid
                self._remove_system_scheduler(task)
                self._setup_system_scheduler(tmp)
                done.append((task.id, nid))

        if not self.db.renumber_ids(mapping):
            SafePrint.error("数据库改写失败，系统侧已改名的实体需修复：`schedule repair`")
            return False
        self._set_id_counter(len(ordered))
        self._load_tasks()

        if not quiet:
            self._print_renumber_table(rows, "已调整")
            SafePrint.success(f"序号已压实为 1..{len(ordered)}")

        if self.platform == Platform.WINDOWS:
            bad = []
            for task in self.tasks:
                info = self._query_windows_task(self._windows_task_name(task))
                if info is None:
                    bad.append(f"{task.name}(实体缺失)")
                elif not re.search(rf"internal-run\s+{task.id}\s*$",
                                   info.get("arguments", "") or ""):
                    bad.append(f"{task.name}(动作仍指向别的 id)")
            if bad:
                SafePrint.warn("重编号后系统侧校验未通过: " + "；".join(bad)
                               + "（运行 `schedule repair` 可修正）")
        return True

    def _setup_macos_task(self, task: Task) -> bool:
        """设置 macOS launchd（修复时间硬编码）"""
        try:
            plist_name = f"com.taskscheduler.{task.id}.plist"
            plist_path = Path.home() / "Library/LaunchAgents" / plist_name
            
            # 解析时间
            hour, minute = 2, 0  # 默认值
            if task.schedule_type == 'daily':
                hour, minute = map(int, task.schedule_value.split(':'))
            elif task.schedule_type == 'every_days':
                days_part, time_part = task.schedule_value.split('|')
                hour, minute = map(int, time_part.split(':'))
            elif task.schedule_type == 'weekly' or task.schedule_type == 'monthly':
                parts = task.schedule_value.split()
                hour, minute = map(int, parts[1].split(':'))
            
            # 构建 plist 内容
            python_exe = sys.executable
            script_path = Path(__file__).resolve()
            
            # 根据调度类型构建不同的 StartCalendarInterval
            if task.schedule_type == 'weekly':
                parts = task.schedule_value.split()
                day_map = {'mon': 2, 'tue': 3, 'wed': 4, 'thu': 5, 'fri': 6, 'sat': 7, 'sun': 1}
                weekday = day_map.get(parts[0].lower(), 1)
                interval_xml = f'''
    <key>Weekday</key>
    <integer>{weekday}</integer>
    <key>Hour</key>
    <integer>{hour}</integer>
    <key>Minute</key>
    <integer>{minute}</integer>'''
            elif task.schedule_type == 'monthly':
                parts = task.schedule_value.split()
                day = int(parts[0])
                interval_xml = f'''
    <key>Day</key>
    <integer>{day}</integer>
    <key>Hour</key>
    <integer>{hour}</integer>
    <key>Minute</key>
    <integer>{minute}</integer>'''
            else:
                interval_xml = f'''
    <key>Hour</key>
    <integer>{hour}</integer>
    <key>Minute</key>
    <integer>{minute}</integer>'''

            # 开机/登录触发没有日历时间，改用 RunAtLoad（登录即运行）
            if task.schedule_type == 'startup':
                schedule_block = '''    <key>RunAtLoad</key>
    <true/>'''
            else:
                schedule_block = f'''    <key>StartCalendarInterval</key>
    <dict>{interval_xml}
    </dict>'''
            
            plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.taskscheduler.{task.id}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>{script_path}</string>
        <string>internal-run</string>
        <string>{task.id}</string>
    </array>
    {schedule_block}
    <key>StandardOutPath</key>
    <string>{self.log_dir}/task_{task.id}.out.log</string>
    <key>StandardErrorPath</key>
    <string>{self.log_dir}/task_{task.id}.err.log</string>
</dict>
</plist>'''
            
            with open(plist_path, 'w') as f:
                f.write(plist_content)
            
            # 加载任务
            result = subprocess.run(['launchctl', 'load', str(plist_path)],
                                  capture_output=True, text=True, encoding='utf-8', errors='replace')
            
            if result.returncode == 0:
                SafePrint.success(f"macOS launchd 任务已创建: {plist_path}")
                return True
            else:
                SafePrint.error(f"macOS 任务加载失败: {result.stderr}")
                return False
            
        except Exception as e:
            SafePrint.error(f"macOS 任务设置失败: {e}")
            return False
    
    def _setup_linux_task(self, task: Task) -> bool:
        """设置 Linux cron"""
        try:
            cron_expr = self._build_cron_expression(task)
            
            python_exe = sys.executable
            script_path = Path(__file__).resolve()
            command = f'{cron_expr} {python_exe} {script_path} internal-run {task.id}'
            
            # 读取现有 crontab
            result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, encoding='utf-8', errors='replace')
            existing_cron = result.stdout if result.returncode == 0 else ""
            
            # 移除旧的同 ID 任务
            lines = existing_cron.split('\n')
            new_lines = []
            skip_next = False
            for line in lines:
                if skip_next:
                    skip_next = False
                    continue
                if f"internal-run {task.id}" in line:
                    continue
                if f"Task Scheduler: {task.name}" in line and f"(ID: {task.id})" in line:
                    skip_next = True
                    continue
                new_lines.append(line)
            
            # 添加新任务
            new_cron = '\n'.join(new_lines)
            new_cron += f"\n# Task Scheduler: {task.name} (ID: {task.id})\n{command}\n"
            
            # 写入新 crontab
            result = subprocess.run(['crontab', '-'], input=new_cron, text=True,
                                  capture_output=True, encoding='utf-8', errors='replace')
            
            if result.returncode == 0:
                SafePrint.success("Linux cron 任务已创建")
                return True
            else:
                SafePrint.error(f"Linux cron 设置失败: {result.stderr}")
                return False
            
        except Exception as e:
            SafePrint.error(f"Linux 任务设置失败: {e}")
            return False
    
    def _build_cron_expression(self, task: Task) -> str:
        """构建 cron 表达式"""
        if task.schedule_type == 'startup':
            return '@reboot'
        if task.schedule_type == 'hourly':
            return '0 * * * *'
        elif task.schedule_type == 'daily':
            time_parts = task.schedule_value.split(':')
            return f"{time_parts[1]} {time_parts[0]} * * *"
        elif task.schedule_type == 'every_days':
            days_part, time_part = task.schedule_value.split('|')
            time_parts = time_part.split(':')
            return f"{time_parts[1]} {time_parts[0]} */{days_part} * *"
        elif task.schedule_type == 'weekly':
            parts = task.schedule_value.split()
            day_map = {'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6, 'sun': 0}
            day = day_map.get(parts[0].lower(), 1)
            time_parts = parts[1].split(':')
            return f"{time_parts[1]} {time_parts[0]} * * {day}"
        elif task.schedule_type == 'monthly':
            parts = task.schedule_value.split()
            day = parts[0]
            time_parts = parts[1].split(':')
            return f"{time_parts[1]} {time_parts[0]} {day} * *"
        return '0 2 * * *'
    
    def remove_task(self, name: str, auto_renumber: bool = True) -> bool:
        """删除任务

        auto_renumber：删掉中间一条会留下空号，默认顺手压实成 1..N，
        让 `schedule list` 的序号始终连续（`--no-renumber` 可关闭）。
        """
        task = next((t for t in self.tasks if t.name == name), None)
        if not task:
            SafePrint.error(f"任务 '{name}' 不存在")
            return False
        
        # 从数据库删除
        if not self.db.delete_task(task.id):
            SafePrint.error("从数据库删除任务失败")
            return False
        
        # 移除系统调度器
        self._remove_system_scheduler(task)
        
        # 从内存列表移除
        self.tasks.remove(task)

        if auto_renumber:
            if self.renumber_tasks(quiet=True):
                SafePrint.info("序号已自动压实为 1..N")
            else:
                SafePrint.warn("序号仍有空号，可稍后运行 `schedule renumber`")
        
        return True
    
    def _remove_system_scheduler(self, task: Task):
        """移除系统级定时任务"""
        try:
            if self.platform == Platform.WINDOWS:
                task_name = f"TaskScheduler-{task.id}-{task.name.replace(' ', '_')}"
                subprocess.run(['schtasks', '/Delete', '/TN', task_name, '/F'],
                             capture_output=True, encoding='utf-8', errors='replace')
            elif self.platform == Platform.MACOS:
                plist_name = f"com.taskscheduler.{task.id}.plist"
                plist_path = Path.home() / "Library/LaunchAgents" / plist_name
                if plist_path.exists():
                    subprocess.run(['launchctl', 'unload', str(plist_path)],
                                 capture_output=True, encoding='utf-8', errors='replace')
                    plist_path.unlink()
            else:
                result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, encoding='utf-8', errors='replace')
                if result.returncode == 0:
                    lines = result.stdout.split('\n')
                    new_lines = []
                    skip_next = False
                    for line in lines:
                        if skip_next:
                            skip_next = False
                            continue
                        if f"Task Scheduler: {task.name}" in line and f"(ID: {task.id})" in line:
                            skip_next = True
                            continue
                        if f"internal-run {task.id}" in line:
                            continue
                        new_lines.append(line)
                    new_cron = '\n'.join(new_lines)
                    subprocess.run(['crontab', '-'], input=new_cron, text=True,
                                 capture_output=True, encoding='utf-8', errors='replace')
        except Exception as e:
            SafePrint.warn(f"系统任务移除警告: {e}")
    
    def pause_task(self, name: str) -> bool:
        """暂停任务"""
        task = next((t for t in self.tasks if t.name == name), None)
        if not task:
            SafePrint.error(f"任务 '{name}' 不存在")
            return False
        
        task.status = "paused"
        # 暂停即失去触发计划：同时清掉 next_run，避免库里残留一个"暂停中却仍有
        # 下次执行时间"的陈旧值（显示层已屏蔽该值，但直接读 DB 的工具/导出会被误导）。
        # 失败回滚时恢复原值，保证 DB 与系统任务不漂移。
        cleared_next_run = task.next_run
        task.next_run = None
        if self.db.save_task(task):
            # 暂停系统调度器
            if self.platform == Platform.WINDOWS:
                task_name = f"TaskScheduler-{task.id}-{task.name.replace(' ', '_')}"
                result = subprocess.run(['schtasks', '/Change', '/TN', task_name, '/DISABLE'],
                             capture_output=True, encoding='utf-8', errors='replace')
                if result.returncode != 0:
                    # 回滚状态，避免 DB 与系统任务漂移
                    task.status = "active"
                    task.next_run = cleared_next_run
                    self.db.save_task(task)
                    SafePrint.error(f"暂停系统任务失败: {result.stderr.strip()}")
                    return False
            return True
        return False
    
    def enable_task(self, name: str) -> bool:
        """启用任务"""
        task = next((t for t in self.tasks if t.name == name), None)
        if not task:
            SafePrint.error(f"任务 '{name}' 不存在")
            return False
        
        task.status = "active"
        task.next_run = self._calculate_next_run(task)
        if self.db.save_task(task):
            # 启用系统调度器
            if self.platform == Platform.WINDOWS:
                task_name = f"TaskScheduler-{task.id}-{task.name.replace(' ', '_')}"
                result = subprocess.run(['schtasks', '/Change', '/TN', task_name, '/ENABLE'],
                             capture_output=True, encoding='utf-8', errors='replace')
                if result.returncode != 0:
                    # 回滚状态，避免 DB 与系统任务漂移
                    task.status = "paused"
                    self.db.save_task(task)
                    SafePrint.error(f"启用系统任务失败: {result.stderr.strip()}")
                    return False
                # 旧任务可能仍是"用电池不启动/错过不补跑"的默认设置，一并修正
                self._apply_windows_task_settings(task, enabled=True)
                # 以 Windows 网格为权威回填 next_run：_calculate_next_run 用的是
                # created_at 锚点，重建过 StartBoundary 的任务会与系统相位不一致
                self._sync_next_run_with_windows(task)
            return True
        return False
    
    def run_task_now(self, name: str) -> bool:
        """立即执行任务（带依赖检查）"""
        task = next((t for t in self.tasks if t.name == name), None)
        if not task:
            SafePrint.error(f"任务 '{name}' 不存在")
            return False
        
        return self._execute_task_with_deps(task, manual=True)
    
    def _execute_task_with_deps(self, task: Task, manual: bool = False,
                               retry_count: int = 0) -> bool:
        """执行任务（包含依赖处理）"""
        # 暂停的任务只允许手动 run-now 触发，避免 DB 状态与系统任务漂移时被偷偷跑起来。
        # 只拦 paused/disabled：running 可能是上次进程被强杀留下的陈旧状态
        # （Windows 侧 MultipleInstances=IgnoreNew，能再次进来就说明上一轮已经结束），
        # 否则这种任务会被永久跳过。
        if not manual and task.status in ("paused", "disabled"):
            SafePrint.warn(f"任务 '{task.name}' 当前状态为 {task.status}，跳过执行")
            return False
        if not manual and task.status == "running":
            SafePrint.warn(f"任务 '{task.name}' 上次执行未正常收尾（状态仍为 running），按陈旧状态继续执行")

        # 检查依赖
        if task.depends_on:
            dep_task = next((t for t in self.tasks if t.name == task.depends_on), None)
            if dep_task:
                SafePrint.info(f"任务 '{task.name}' 依赖 '{dep_task.name}'，先执行依赖...")
                dep_result = self._execute_task_with_deps(dep_task, manual, 0)
                if not dep_result:
                    SafePrint.error(f"依赖任务 '{dep_task.name}' 执行失败，跳过 '{task.name}'")
                    return False
            else:
                SafePrint.warn(f"依赖任务 '{task.depends_on}' 不存在")
        
        # 执行当前任务
        return self._execute_task(task, manual, retry_count)
    
    def _execute_task(self, task: Task, manual: bool = False,
                     retry_count: int = 0) -> bool:
        """执行单个任务（带重试机制）"""
        SafePrint.info(f"{'='*60}")
        SafePrint.info(f"执行任务: {task.name}")
        SafePrint.info(f"命令: {task.command}")
        SafePrint.info(f"{'='*60}")
        
        # 更新任务状态
        task.status = "running"
        if not manual:
            self.db.save_task(task)
        
        start_time = datetime.now()
        output = ""
        error = ""
        
        try:
            # 准备环境
            env = os.environ.copy()
            env.update(task.env_vars)
            
            # Windows 编码修复 - 系统级设置
            if sys.platform == 'win32':
                env['PYTHONIOENCODING'] = 'utf-8'
                env['PYTHONUTF8MODE'] = '1'
                env['LC_ALL'] = 'C.UTF-8'
                env['LANG'] = 'C.UTF-8'
                env['PYTHONLEGACYWINDOWSSTDIO'] = 'utf-8'
            
            # 准备执行命令
            # 将命令包装，确保 UTF-8 环境
            cmd_to_run = None
            if sys.platform == 'win32':
                # GUI 启动器（wscript/cscript/mshta）：直接执行，不再经
                # python.exe + cmd.exe 中转，链路上没有任何控制台进程，
                # 从根上排除"每次触发闪一下窗口"。
                cmd_to_run = gui_launcher_argv(task.command)
            if sys.platform == 'win32' and cmd_to_run is None:
                # 创建临时 wrapper 脚本
                launcher_script = os.path.join(os.path.dirname(__file__), 'task_utf8_wrapper.py')

                # 显式使用有控制台的 python.exe 运行子进程。
                # 不能用 sys.executable：当本脚本由 pythonw.exe 启动（计划任务隐藏窗口）
                # 时，sys.executable 就是 pythonw.exe，会让被执行的命令也变成无窗口进程，
                # 其写往管道 stdout 的输出随之丢失（实测日志 output 由 5769 字节变 0）。
                # 窗口隐藏由父进程的 pythonw 负责，子进程通过管道回传输出不受影响。
                _console_py = os.path.join(os.path.dirname(sys.executable), "python.exe")
                if sys.executable.lower().endswith("pythonw.exe") and os.path.exists(_console_py):
                    _child_py = _console_py
                else:
                    _child_py = sys.executable

                # 调用 wrapper 脚本执行命令
                cmd_to_run = [_child_py, launcher_script, task.command]
            elif sys.platform != 'win32':
                cmd_to_run = task.command
            
            # 执行命令
            # CREATE_NO_WINDOW：显式禁止为子控制台程序创建窗口。手动 run-now 时
            # 父进程（终端）带控制台，不加此标志会让被执行的 .bat/.cmd 弹出 cmd 窗口；
            # 计划任务自动触发时父进程无控制台，此项无副作用。
            _nw = {}
            if sys.platform == 'win32' and hasattr(subprocess, 'CREATE_NO_WINDOW'):
                _nw['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                cmd_to_run,
                shell=False if sys.platform == 'win32' else True,
                cwd=task.working_dir,
                env=env,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=14400,
                **_nw
            )
            
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            output = result.stdout
            error = result.stderr
            
            # 如果错误为空，显示返回码
            if not error and result.returncode != 0:
                error = f"命令返回非零退出码: {result.returncode}"
            
            # 如果输出为空但有错误，显示错误
            if not output and error:
                output = f"[无标准输出]\n[错误输出]: {error[:1000]}"
            
            # 更新任务状态
            task.last_run = self._fmt_ts(start_time)
            task.next_run = self._calculate_next_run(task)
            
            if result.returncode == 0:
                SafePrint.success(f"任务执行成功 (耗时: {duration:.1f}秒)")
                task.status = "active"
                task.fail_count = 0  # 重置失败计数
                task.run_count += 1  # 仅成功才计数
                
                # 记录日志
                self.db.log_execution(task.id, task.name, "SUCCESS",
                                    duration, output[:10000], error[:2000], retry_count)
                
                # 发送通知
                if not manual:
                    self._send_notification(task, success=True)
                    if not self._notify_suppressed(task, success=True):
                        WindowsNotifier.notify(
                            f"任务成功: {task.name}",
                            f"执行完成，耗时 {duration:.1f} 秒",
                            "info"
                        )
                
                self.db.save_task(task)
                return True
            else:
                SafePrint.error(f"任务执行失败 (耗时: {duration:.1f}秒)")
                SafePrint.error(f"退出码: {result.returncode}")
                if error:
                    SafePrint.error(f"错误信息: {error[:500]}")
                if output:
                    SafePrint.info(f"输出: {output[:200]}")
                task.fail_count += 1
                task.status = "failed"
                
                # 记录日志
                self.db.log_execution(task.id, task.name, "FAILED",
                                    duration, output[:10000], error[:2000], retry_count)
                
                # 重试逻辑
                if retry_count < task.retry_count:
                    SafePrint.info(f"将在 {task.retry_delay} 秒后重试...")
                    time.sleep(task.retry_delay)
                    return self._execute_task(task, manual, retry_count + 1)
                
                # 发送失败通知
                if not manual:
                    self._send_notification(task, success=False, error=error)
                    if not self._notify_suppressed(task, success=False):
                        WindowsNotifier.notify(
                            f"任务失败: {task.name}",
                            f"失败 {task.fail_count} 次，请检查日志",
                            "error"
                        )
                
                self.db.save_task(task)
                return False
                
        except subprocess.TimeoutExpired:
            SafePrint.error("任务执行超时 (超过4小时)")
            task.fail_count += 1
            task.status = "failed"
            error_msg = "任务执行超时 (超过4小时)"
            self.db.log_execution(task.id, task.name, "TIMEOUT", 14400, "", error_msg, retry_count)
            
            self.db.save_task(task)
            return False
        except Exception as e:
            import traceback
            SafePrint.error(f"任务执行出错: {e}")
            SafePrint.warn(f"详细错误: {traceback.format_exc()[:200]}")
            task.fail_count += 1
            task.status = "failed"
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            self.db.log_execution(task.id, task.name, "ERROR", 0, "", error_msg[:2000], retry_count)
            
            self.db.save_task(task)
            return False
    
    def _notify_suppressed(self, task: Task, success: bool) -> bool:
        """按任务开关抑制桌面气泡弹窗（守护类高频任务用）。

        取值来自任务 env_vars 的 TASK_SCHEDULER_SILENT（也可用同名环境变量全局设置）：
          1 / true / yes / on / all  -> 成功与失败都不弹
          success / ok               -> 只静默成功，失败仍弹（推荐给守护任务：
                                        正常运行零打扰，真挂了还能看到提示）
        """
        raw = (task.env_vars or {}).get('TASK_SCHEDULER_SILENT')
        if raw is None:
            raw = os.environ.get('TASK_SCHEDULER_SILENT')
        flag = str(raw or '').strip().lower()
        if flag in ('1', 'true', 'yes', 'on', 'all'):
            return True
        if flag in ('success', 'ok') and success:
            return True
        return False

    def _send_notification(self, task: Task, success: bool, error: str = ""):
        """发送通知"""
        if success and task.notify_on_success:
            if task.notify_on_success == "slack" and task.slack_webhook:
                self._send_slack_notification(task, success, error)
            elif task.notify_on_success == "email" and task.email:
                self._send_email_notification(task, success, error)
        
        elif not success and task.notify_on_fail:
            if task.notify_on_fail == "slack" and task.slack_webhook:
                self._send_slack_notification(task, success, error)
            elif task.notify_on_fail == "email" and task.email:
                self._send_email_notification(task, success, error)
    
    def _send_slack_notification(self, task: Task, success: bool, error: str):
        """发送 Slack 通知"""
        try:
            import urllib.request
            import json
            
            color = "good" if success else "danger"
            status_text = "成功" if success else "失败"
            
            payload = {
                "attachments": [{
                    "color": color,
                    "title": f"定时任务通知: {task.name}",
                    "text": f"执行状态: {status_text}\n时间: {datetime.now().isoformat()}",
                    "fields": [
                        {"title": "任务ID", "value": str(task.id), "short": True},
                        {"title": "运行次数", "value": str(task.run_count), "short": True}
                    ]
                }]
            }
            
            if not success and error:
                payload["attachments"][0]["fields"].append({
                    "title": "错误信息",
                    "value": error[:1000],
                    "short": False
                })
            
            if task.slack_webhook:
                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    task.slack_webhook,
                    data=data,
                    headers={'Content-Type': 'application/json'}
                )
                urllib.request.urlopen(req)
        except Exception as e:
            SafePrint.warn(f"Slack通知发送失败: {e}")
    
    def _send_email_notification(self, task: Task, success: bool, error: str):
        """发送邮件通知"""
        SafePrint.info(f"[邮件通知] 任务 '{task.name}' {'成功' if success else '失败'}")
        SafePrint.info(f"收件人: {task.email}")
    
    def list_tasks(self, group: Optional[str] = None) -> List[Task]:
        """列出所有任务"""
        tasks = self.tasks
        if group:
            tasks = [t for t in tasks if t.group == group]
        return sorted(tasks, key=lambda x: x.id)
    
    def get_logs(self, task_name: Optional[str] = None, last: int = 50,
                 today: bool = False, failed: bool = False) -> List[Dict]:
        """获取执行日志（从数据库）"""
        status = "FAILED" if failed else None
        return self.db.get_logs(task_name, last, status, today)
    
    def export_config(self, output_file: str):
        """导出配置"""
        data = {
            "settings": {
                "version": "2.0",
                "exported_at": datetime.now().isoformat()
            },
            "tasks": [asdict(task) for task in self.tasks]
        }
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        SafePrint.success(f"配置已导出到: {output_file}")
    
    _IMPORT_ALIASES = {'group_name': 'group'}

    def _task_kwargs_from_import(self, raw: Any) -> Dict[str, Any]:
        """把导出文件里的一条任务记录规整成 Task 的构造参数（不含 id）。

        导出文件是 `asdict(Task)` 的结果，字段本应与 Task 一一对应，但导入方必须
        容忍三种现实情况 —— 直接 `Task(**task_data)` 会在这三种情况下全线崩掉：
        1. `id` 是 **必填** 字段（无默认值）。旧实现先 `pop('id')` 再构造，必然抛
           `Task.__init__() missing 1 required positional argument: 'id'`；
           而导出的 id 也不能照搬：id 已经编进系统任务名 `TaskScheduler-<id>-<name>`，
           且必须单调递增发号，所以这里丢掉 id，由调用方用 `_get_next_id()` 重新分配。
        2. 不同版本 / 手改过的文件可能多出 `group_name`、`comment` 之类的键，
           多一个未知键就是 TypeError；这里按 Task 的字段白名单过滤，未知键忽略并提示。
        3. 脏数据（缺 name、调度值写错、env_vars 是 JSON 字符串）应该逐条跳过并报错，
           而不是让整份配置一条都进不来。

        返回值可直接拆成 `Task(id=..., **kwargs)`。
        """
        if not isinstance(raw, dict):
            raise ValueError("任务记录不是 JSON 对象")

        known = {f.name for f in fields(Task)}
        kwargs: Dict[str, Any] = {}
        ignored: List[str] = []
        for key, value in raw.items():
            name = self._IMPORT_ALIASES.get(key, key)
            if name == 'id':
                continue  # 重新发号，见上方说明
            if name not in known:
                ignored.append(key)
                continue
            kwargs[name] = value
        if ignored:
            SafePrint.warn(f"忽略未知字段: {', '.join(ignored)}")

        # 必填字段：缺失或不是非空字符串就整条跳过（不让脏数据打断其它任务）
        for required in ('name', 'command', 'schedule_type', 'schedule_value'):
            value = kwargs.get(required)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"缺少必填字段 '{required}'")
            kwargs[required] = value.strip()

        schedule_type = kwargs['schedule_type'].lower()
        kwargs['schedule_type'] = schedule_type
        if schedule_type not in ('hourly', 'daily', 'every_days', 'weekly',
                                 'monthly', 'interval', 'startup'):
            raise ValueError(f"未知调度类型 '{schedule_type}'")
        err = _validate_schedule_value(schedule_type, kwargs['schedule_value'])
        if err:
            raise ValueError(err)

        # 状态归位：running/failed 是执行态残留，disabled 等同于暂停
        status = str(kwargs.get('status') or 'active').strip().lower()
        if status in ('paused', 'disabled'):
            kwargs['status'] = 'paused'
        elif status in ('active', 'running', 'failed'):
            kwargs['status'] = 'active'
        else:
            SafePrint.warn(f"任务 '{kwargs['name']}' 的状态 '{status}' 无法识别，按 active 导入")
            kwargs['status'] = 'active'

        # env_vars：库里存的是 JSON 文本，导出后是 dict；手工文件里也可能是 JSON 字符串
        env = kwargs.get('env_vars')
        if env in (None, ''):
            kwargs['env_vars'] = {}
        elif isinstance(env, dict):
            kwargs['env_vars'] = {str(k): str(v) for k, v in env.items()}
        elif isinstance(env, str):
            try:
                parsed = json.loads(env) if env.strip() else {}
            except ValueError:
                raise ValueError(f"env_vars 不是合法 JSON: {env!r}")
            if not isinstance(parsed, dict):
                raise ValueError("env_vars 应为对象")
            kwargs['env_vars'] = {str(k): str(v) for k, v in parsed.items()}
        else:
            raise ValueError("env_vars 应为对象或 JSON 字符串")

        # 计数字段：脏值退回默认值，负数归零
        for key, default in (('run_count', 0), ('fail_count', 0),
                             ('retry_count', 3), ('retry_delay', 60)):
            try:
                value = int(kwargs.get(key, default))
            except (TypeError, ValueError):
                value = default
            kwargs[key] = max(0, value)

        # 可选字符串字段：非字符串一律当空，避免写库时类型噪声
        for key in ('created_at', 'last_run', 'next_run', 'group', 'depends_on',
                    'working_dir', 'notify_on_success', 'notify_on_fail',
                    'slack_webhook', 'email'):
            if kwargs.get(key) is not None and not isinstance(kwargs[key], str):
                kwargs[key] = None

        # next_run 不照搬：暂停的任务没有触发计划，启用的任务按本机时钟重算
        kwargs.pop('next_run', None)
        return kwargs

    def import_config(self, input_file: str, overwrite: bool = False,
                      dry_run: bool = False) -> bool:
        """导入配置

        overwrite：同名任务已存在时默认跳过（避免误把现有任务的统计和历史抹掉），
                   加 --overwrite 才就地覆盖（保留原 id，系统任务名不会漂移）。
        dry_run：只解析并打印将导入什么，不写库、不建系统任务。
        """
        try:
            with open(input_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except OSError as e:
            SafePrint.error(f"读取配置文件失败: {e}")
            return False
        except ValueError as e:
            SafePrint.error(f"配置文件不是合法 JSON: {e}")
            return False

        if not isinstance(data, dict):
            SafePrint.error("配置格式错误：根节点应为 JSON 对象")
            return False
        raw_tasks = data.get('tasks')
        if not isinstance(raw_tasks, list):
            SafePrint.error("配置格式错误：缺少 'tasks' 数组")
            return False

        settings = data.get('settings') if isinstance(data.get('settings'), dict) else {}
        version = str(settings.get('version') or '')
        if version and version != '2.0':
            SafePrint.warn(f"配置版本为 {version}，将按 2.0 字段尽力导入")

        imported = skipped = failed = 0
        for index, raw in enumerate(raw_tasks, 1):
            try:
                kwargs = self._task_kwargs_from_import(raw)
            except ValueError as e:
                failed += 1
                SafePrint.error(f"第 {index} 条任务导入失败: {e}")
                continue

            name = kwargs['name']
            existing = next((t for t in self.tasks if t.name == name), None)
            if existing is not None and not overwrite:
                skipped += 1
                SafePrint.warn(f"任务 '{name}' 已存在，已跳过（加 --overwrite 可覆盖）")
                continue

            if dry_run:
                SafePrint.info(f"[DRY-RUN] {name} -> "
                               f"{format_schedule_str(Task(id=0, **kwargs))}")
                imported += 1
                continue

            paused = kwargs['status'] == 'paused'

            # 先由建/改两种分支得到最终 task 对象
            if existing is not None:
                # 就地覆盖并保留原 id：INSERT OR REPLACE 在 name 唯一键上会把旧行
                # 整行删掉再插新 id，等于静默丢失原有执行历史与 id
                task = existing
                new_id = task.id
                for key, value in kwargs.items():
                    setattr(task, key, value)
                task.id = new_id
            else:
                task = Task(id=self._get_next_id(), **kwargs)

            # 暂停的任务库里不该留 next_run（与 pause_task 的语义保持一致）
            task.next_run = None if paused else self._calculate_next_run(task)

            if not self.db.save_task(task):
                failed += 1
                continue
            if existing is None:
                self.tasks.append(task)

            # 系统调度器：与 add 保持一致；暂停任务照样建实体再禁用（_setup_windows_task
            # 内部已按 task.status 决定启用/禁用），这样 `enable` 里的
            # schtasks /Change /ENABLE 才有对象可用。
            if not self._setup_system_scheduler(task):
                SafePrint.warn(f"任务 '{name}' 的系统计划任务创建失败，可稍后执行 `schedule repair`")
            if paused:
                # 建成功后 _setup_windows_task 会用 Windows 网格回填 next_run，
                # 这里再按"暂停无触发计划"的语义钉回 None，保证 DB 与系统任务不漂移
                task.next_run = None
                self.db.save_task(task)

            imported += 1

        if dry_run:
            SafePrint.info(f"[DRY-RUN] 可导入 {imported} 个任务，跳过 {skipped} 个，失败 {failed} 个")
        else:
            SafePrint.success(f"成功导入 {imported} 个任务"
                              + (f"，跳过 {skipped} 个" if skipped else "")
                              + (f"，失败 {failed} 个" if failed else ""))
        return failed == 0


def format_schedule_str(task: Task) -> str:
    """生成人类可读的调度描述（中文）"""
    st = task.schedule_type
    sv = task.schedule_value
    weekdays = {
        'mon': '周一', 'tue': '周二', 'wed': '周三',
        'thu': '周四', 'fri': '周五', 'sat': '周六', 'sun': '周日'
    }
    if st == 'startup':
        return "登录时启动"
    if st == 'daily':
        return f"每天 {sv}"
    if st == 'hourly':
        return "每小时"
    if st == 'weekly':
        parts = sv.split()
        if len(parts) == 2:
            return f"每周{weekdays.get(parts[0].lower()[:3], parts[0])} {parts[1]}"
        return f"每周 {sv}"
    if st == 'monthly':
        parts = sv.split()
        if len(parts) == 2:
            return f"每月{parts[0]}日 {parts[1]}"
        return f"每月 {sv}"
    if st == 'every_days':
        days, time_part = sv.split('|')
        return f"每{days}天 {time_part}"
    if st == 'interval':
        if sv.endswith('m'):
            return f"每{sv[:-1]}分钟"
        if sv.endswith('h'):
            return f"每{sv[:-1]}小时"
        if sv.endswith('d'):
            return f"每{sv[:-1]}天"
        return f"间隔 {sv}"
    return f"{st} {sv}"


def print_tasks_table(tasks: List[Task]):
    """打印任务表格（安全编码版本）"""
    if not tasks:
        SafePrint.print("\n[暂无任务]\n")
        return
    
    SafePrint.print("\n" + "="*80)
    SafePrint.print(f"{'ID':<4} {'任务名称':<20} {'调度':<15} {'状态':<8} {'上次执行':<16} {'下次执行':<16}")
    SafePrint.print("-"*80)
    
    for task in tasks:
        status_map = {
            'active': '[启用]',
            'paused': '[暂停]',
            'running': '[运行中]',
            'failed': '[失败]',
            'disabled': '[已禁用]',
        }
        status_icon = status_map.get(task.status, task.status)
        last_run = task.last_run[:16] if task.last_run else "-"
        # 暂停中的任务没有真实的系统触发计划，显示陈旧的 next_run 只会误导排查
        next_run = task.next_run[:16] if (task.next_run and task.status == 'active') else "-"
        
        schedule_str = format_schedule_str(task)
        if len(schedule_str) > 14:
            schedule_str = schedule_str[:11] + "..."
        
        SafePrint.print(f"{task.id:<4} {task.name:<20} {schedule_str:<15} "
                       f"{status_icon:<8} {last_run:<16} {next_run:<16}")
    
    SafePrint.print("="*80)
    SafePrint.print(f"\n总计: {len(tasks)} 个任务\n")


def _validate_schedule_value(schedule_type: str, schedule_value: str) -> Optional[str]:
    """校验调度时间格式，返回错误信息（无错误返回 None）"""
    try:
        if schedule_type == "daily":
            hour, minute = map(int, schedule_value.split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return f"无效时间 '{schedule_value}'，小时需 0-23，分钟需 0-59"
        elif schedule_type == "every_days":
            days_part, time_part = schedule_value.split('|')
            days = int(days_part)
            if days < 1:
                return f"无效天数 '{days_part}'，需 ≥ 1"
            hour, minute = map(int, time_part.split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return f"无效时间 '{time_part}'，小时需 0-23，分钟需 0-59"
        elif schedule_type == "weekly":
            parts = schedule_value.split()
            if len(parts) != 2:
                return f"每周调度需 '星期 时间' 格式，如 'mon 09:30'"
            day_map = {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'}
            if parts[0].lower()[:3] not in day_map:
                return f"无效星期 '{parts[0]}'"
            hour, minute = map(int, parts[1].split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return f"无效时间 '{parts[1]}'，小时需 0-23，分钟需 0-59"
        elif schedule_type == "monthly":
            parts = schedule_value.split()
            if len(parts) != 2:
                return f"每月调度需 '日 时间' 格式，如 '15 09:30'"
            day = int(parts[0])
            if not (1 <= day <= 31):
                return f"无效日期 '{parts[0]}'，需 1-31"
            hour, minute = map(int, parts[1].split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return f"无效时间 '{parts[1]}'，小时需 0-23，分钟需 0-59"
        elif schedule_type == "interval":
            value = schedule_value
            if value.endswith('m'):
                n = int(value[:-1])
                if not (1 <= n <= 1439):
                    return f"无效分钟间隔 '{value}'，需 1-1439"
            elif value.endswith('h'):
                n = int(value[:-1])
                if not (1 <= n <= 23):
                    return f"无效小时间隔 '{value}'，需 1-23"
            elif value.endswith('d'):
                n = int(value[:-1])
                if not (1 <= n <= 31):
                    return f"无效天数间隔 '{value}'，需 1-31"
            else:
                return f"无效间隔 '{value}'，需以 m/h/d 结尾"
    except ValueError:
        return f"无效调度格式 '{schedule_value}'"
    except Exception:
        return f"无法解析调度 '{schedule_value}'"
    return None


def main():
    # prog 固定为 `schedule`：让 --help / usage / 示例里的命令名与全局命令一致。
    # 不写死的话，经 schedule.ps1 → python schedule.py 调用时 %(prog)s 会展开成
    # "schedule.py"，文档与帮助里就会出现 `schedule.py add ...` 这种不该出现的写法。
    parser = argparse.ArgumentParser(prog='schedule',
        description='跨平台定时任务管理器 v2.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 添加每日任务
  %(prog)s add "整理下载" --command "python organize.py ~/Downloads" --daily 02:00
  
  # 添加带依赖的任务
  %(prog)s add "数据同步" --command "sync.sh" --daily 01:00 --depends-on "数据备份"
  
  # 添加带重试的任务
  %(prog)s add "重要任务" --command "job.sh" --daily 12:00 --retry-count 5 --retry-delay 300
  
  # 修改触发时间（其余字段保持不变）
  %(prog)s update "整理下载" --daily 03:30
  
  # 修改任意字段：命令/分组/依赖/重试/通知/环境变量（未传的字段保持原值）
  %(prog)s update "数据同步" --command "bash sync_v2.sh" --every-days 3 01:00 \\
      --group "维护任务" --depends-on "数据备份" --retry-count 5 --retry-delay 300 \\
      --env "API_KEY=xxx" --notify-on-fail slack --slack-webhook "https://hooks.slack.com/..."
  
  # 重命名 / 改名后依赖引用会自动同步
  %(prog)s update "数据同步" --name "数据同步(新版)"
  
  # 先看将改什么，不落盘
  %(prog)s update "数据同步" --daily 04:00 --dry-run
  
  # 列出所有任务
  %(prog)s list
  
  # 查看日志
  %(prog)s logs --last 20
  
  # 导出配置
  %(prog)s export config.json
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='可用命令')
    
    # add 命令
    add_parser = subparsers.add_parser('add', help='添加新任务')
    add_parser.add_argument('name', help='任务名称')
    add_parser.add_argument('--cmd', '--command', required=True, dest='task_command', help='要执行的命令')
    add_parser.add_argument('--daily', metavar='HH:MM', help='每天执行的时间')
    add_parser.add_argument('--weekly', nargs=2, metavar=('DAY', 'HH:MM'),
                           help='每周执行（如: mon 09:00）')
    add_parser.add_argument('--monthly', nargs=2, metavar=('DAY', 'HH:MM'),
                           help='每月执行（如: 1 03:00）')
    add_parser.add_argument('--hourly', action='store_true', help='每小时执行')
    add_parser.add_argument('--interval', metavar='VALUE',
                           help='间隔执行（如: 30m, 4h, 7d）')
    add_parser.add_argument('--every-days', nargs=2, metavar=('DAYS', 'HH:MM'),
                           help='每隔N天执行（如: 2 12:06）')
    add_parser.add_argument('--at-startup', action='store_true',
                           help='开机/登录时执行（Windows: ONLOGON；macOS: RunAtLoad；Linux: @reboot）')
    add_parser.add_argument('--group', help='任务分组')
    add_parser.add_argument('--depends-on', help='依赖的其他任务')
    add_parser.add_argument('--working-dir', help='工作目录')
    add_parser.add_argument('--env', action='append', help='环境变量（KEY=VALUE）')
    add_parser.add_argument('--notify-on-success', choices=['slack', 'email'],
                           help='成功时通知方式')
    add_parser.add_argument('--notify-on-fail', choices=['slack', 'email'],
                           help='失败时通知方式')
    add_parser.add_argument('--slack-webhook', help='Slack webhook URL')
    add_parser.add_argument('--email', help='通知邮箱')
    add_parser.add_argument('--retry-count', type=int, default=3,
                           help='失败重试次数（默认3；同时写入任务计划程序的失败重启策略）')
    add_parser.add_argument('--retry-delay', type=int, default=60,
                           help='重试间隔秒数（默认60；Windows 侧最小 1 分钟）')
    
    # list 命令
    list_parser = subparsers.add_parser('list', help='列出所有任务')
    list_parser.add_argument('--group', help='按分组筛选')
    
    # remove 命令
    remove_parser = subparsers.add_parser('remove', help='删除任务')
    remove_parser.add_argument('name', help='任务名称')
    remove_parser.add_argument('--no-renumber', action='store_true',
                               help='删除后不自动压实序号（保留原 id，留下空号）')
    
    # pause 命令
    pause_parser = subparsers.add_parser('pause', help='暂停任务')
    pause_parser.add_argument('name', nargs='?', help='任务名称')
    pause_parser.add_argument('--group', help='暂停整个分组')
    
    # enable 命令
    enable_parser = subparsers.add_parser('enable', help='启用任务')
    enable_parser.add_argument('name', help='任务名称')

    # update 命令（就地修改既有任务的任一字段）
    update_parser = subparsers.add_parser(
        'update',
        help='修改既有任务（触发时间/命令/分组/依赖/重试/通知等）',
        description='就地修改既有任务的任一字段：只改传入的字段，未传的保持原值；'
                    '保留原 id 与执行历史，改完自动重算 next_run 并重建系统计划任务。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
字段说明:
  任务名     --name NEW             重命名；其它任务的 --depends-on 引用会自动同步
  命令       --cmd/--command CMD   要执行的命令行（原样存库，经 shell 执行）
  调度       以下七选一，给哪个就整体换成哪种调度：
               --daily HH:MM                 每天
               --weekly DAY HH:MM            每周（如 mon 09:00）
               --monthly DAY HH:MM           每月（如 1 03:00）
               --hourly                      每小时
               --interval VALUE              间隔（如 30m / 4h / 7d）
               --every-days N HH:MM          每 N 天（沿用原日期网格，保住相位）
               --at-startup                  登录时启动（Windows ONLOGON）
  状态       --status active|paused  等价于 enable / pause
  分组       --group NAME            传空字符串表示清空
  依赖       --depends-on NAME       前序任务名；传空字符串表示清空
  工作目录   --working-dir DIR       传空字符串表示清空
  环境变量   --env KEY=VALUE         可重复；给了就整体替换原环境变量
             --clear-env             清空该任务的环境变量
  重试       --retry-count N         失败重试次数（0 表示不重试）
             --retry-delay SEC       重试间隔秒数（同时写入任务计划程序的失败重启策略）
  通知       --notify-on-success {slack,email} / --notify-on-fail {slack,email}
             --slack-webhook URL     Slack webhook
             --email ADDR            通知邮箱
  其它       --dry-run               只显示将修改什么，不写库、不动系统任务

不可修改: id / created_at / last_run / run_count / fail_count / next_run
          （id 与统计字段由系统维护，next_run 在修改后自动重算）

示例:
  schedule update "整理下载" --daily 03:30
  schedule update "数据同步" --every-days 3 01:00 --retry-count 5 --retry-delay 300
  schedule update "任务名" --cmd "python new_job.py" --group "维护任务" --env "ENV=prod"
  schedule update "任务名" --name "新任务名"
  schedule update "任务名" --daily 04:00 --dry-run
        """)

    update_parser.add_argument('name', help='要修改的任务名称（原有任务）')

    sched_group = update_parser.add_argument_group('调度字段（七选一，给哪个就整体换成哪种）')
    sched_group.add_argument('--daily', metavar='HH:MM', help='改为每天执行，如 18:30')
    sched_group.add_argument('--weekly', nargs=2, metavar=('DAY', 'HH:MM'),
                             help='改为每周执行，如 mon 09:00')
    sched_group.add_argument('--monthly', nargs=2, metavar=('DAY', 'HH:MM'),
                             help='改为每月执行，如 1 03:00')
    sched_group.add_argument('--hourly', action='store_true', help='改为每小时执行')
    sched_group.add_argument('--interval', metavar='VALUE',
                             help='改为间隔执行，如 30m / 4h / 7d')
    sched_group.add_argument('--every-days', nargs=2, metavar=('DAYS', 'HH:MM'),
                             help='改为每隔N天执行，如 3 21:40（沿用原日期网格，保住相位）')
    sched_group.add_argument('--at-startup', action='store_true',
                             help='改为开机/登录时执行（Windows ONLOGON）')

    task_group = update_parser.add_argument_group('任务字段')
    task_group.add_argument('--name', dest='new_name', default=_UNSET,
                            help='重命名任务（其它任务的依赖引用会自动同步）')
    task_group.add_argument('--cmd', '--command', dest='task_command', default=_UNSET,
                            help='要执行的命令行')
    task_group.add_argument('--status', choices=['active', 'paused'],
                            help='启用/暂停（等价于 enable / pause）')
    task_group.add_argument('--group', default=_UNSET,
                            help='任务分组（传空字符串表示清空）')
    task_group.add_argument('--depends-on', dest='depends_on', default=_UNSET,
                            help='依赖的前序任务名（传空字符串表示清空）')
    task_group.add_argument('--working-dir', dest='working_dir', default=_UNSET,
                            help='工作目录（传空字符串表示清空）')
    task_group.add_argument('--env', action='append', metavar='KEY=VALUE',
                            help='环境变量，可重复；给定时整体替换原环境变量')
    task_group.add_argument('--clear-env', action='store_true',
                            help='清空该任务的环境变量')

    notify_group = update_parser.add_argument_group('重试与通知')
    notify_group.add_argument('--retry-count', dest='retry_count', type=int,
                              default=_UNSET, help='失败重试次数（0 表示不重试；同时写入任务计划程序的失败重启策略）')
    notify_group.add_argument('--retry-delay', dest='retry_delay', type=int,
                              default=_UNSET, help='重试间隔秒数（Windows 侧最小 1 分钟）')
    notify_group.add_argument('--notify-on-success', dest='notify_on_success',
                              choices=['slack', 'email'], help='成功时的通知方式')
    notify_group.add_argument('--notify-on-fail', dest='notify_on_fail',
                              choices=['slack', 'email'], help='失败时的通知方式')
    notify_group.add_argument('--slack-webhook', dest='slack_webhook', default=_UNSET,
                              help='Slack webhook URL')
    notify_group.add_argument('--email', default=_UNSET, help='通知邮箱')

    update_parser.add_argument('--dry-run', action='store_true',
                               help='只显示将修改什么，不写库、不动系统任务')
    
    # run-now 命令
    run_parser = subparsers.add_parser('run-now', help='立即执行任务')
    run_parser.add_argument('name', help='任务名称')
    
    # logs 命令
    logs_parser = subparsers.add_parser('logs', help='查看日志')
    logs_parser.add_argument('--task', help='特定任务名称')
    logs_parser.add_argument('--last', type=int, default=50, help='显示最近N条')
    logs_parser.add_argument('--today', action='store_true', help='仅显示今天')
    logs_parser.add_argument('--failed', action='store_true', help='仅显示失败')
    
    # export 命令
    export_parser = subparsers.add_parser('export', help='导出配置')
    export_parser.add_argument('file', help='输出文件路径')
    
    # import 命令
    import_parser = subparsers.add_parser('import', help='导入配置')
    import_parser.add_argument('file', help='输入文件路径')
    import_parser.add_argument('--overwrite', action='store_true',
                               help='同名任务已存在时覆盖（默认跳过，保留现有统计）')
    import_parser.add_argument('--dry-run', action='store_true',
                               help='只解析并显示将导入什么，不写库、不建系统任务')
    
    # repair 命令
    repair_parser = subparsers.add_parser(
        'repair', help='按数据库重建/校正系统计划任务（被杀毒误删后一键恢复）')
    repair_parser.add_argument('--dry-run', action='store_true', help='只检查不修改')
    repair_parser.add_argument('--prune', action='store_true',
                               help='删除系统里没有数据库记录的孤儿任务')
    repair_parser.add_argument('--task', help='只修复指定任务名称')

    # renumber 命令
    renumber_parser = subparsers.add_parser(
        'renumber', help='把任务序号压实为 1..N 连续（系统任务名同步改名）')
    renumber_parser.add_argument('--dry-run', action='store_true',
                                 help='只显示将如何调整，不落盘')

    # internal-run 命令（内部使用）
    internal_parser = subparsers.add_parser('internal-run', help=argparse.SUPPRESS)
    internal_parser.add_argument('task_id', type=int, help='任务ID')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    scheduler = TaskScheduler()
    
    
    
    # 处理 internal-run 命令
    if args.command == 'internal-run':
        task = next((t for t in scheduler.tasks if t.id == args.task_id), None)
        if task:
            scheduler._execute_task_with_deps(task)
        else:
            SafePrint.error(f"任务 ID {args.task_id} 不存在")
        return
    
    # 处理其他命令
    if args.command == 'add':
        # 确定调度类型和值
        schedule_type = None
        schedule_value = None
        
        if args.at_startup:
            schedule_type = "startup"
            schedule_value = "onlogon"
        elif args.daily:
            schedule_type = "daily"
            schedule_value = args.daily
        elif args.weekly:
            schedule_type = "weekly"
            schedule_value = f"{args.weekly[0]} {args.weekly[1]}"
        elif args.monthly:
            schedule_type = "monthly"
            schedule_value = f"{args.monthly[0]} {args.monthly[1]}"
        elif args.hourly:
            schedule_type = "hourly"
            schedule_value = "0"
        elif args.interval:
            schedule_type = "interval"
            schedule_value = args.interval
        elif args.every_days:
            schedule_type = "every_days"
            schedule_value = f"{args.every_days[0]}|{args.every_days[1]}"
        else:
            SafePrint.error("请指定调度时间（--at-startup, --daily, --weekly, --monthly, --hourly, --interval, --every-days）")
            return
        
        # 校验调度时间格式
        err = _validate_schedule_value(schedule_type, schedule_value)
        if err:
            SafePrint.error(f"调度时间无效: {err}")
            sys.exit(1)
        
        # 解析环境变量
        env_vars = {}
        if args.env:
            for env in args.env:
                if '=' in env:
                    key, value = env.split('=', 1)
                    env_vars[key] = value
        
        task = scheduler.add_task(
            name=args.name,
            command=args.task_command,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            group=args.group,
            depends_on=args.depends_on,
            working_dir=args.working_dir,
            env_vars=env_vars,
            notify_on_success=args.notify_on_success,
            notify_on_fail=args.notify_on_fail,
            slack_webhook=args.slack_webhook,
            email=args.email,
            retry_count=args.retry_count,
            retry_delay=args.retry_delay
        )
        
        if task:
            SafePrint.print(f"\n[OK] 任务添加成功!")
            SafePrint.print(f"   名称: {task.name}")
            SafePrint.print(f"   ID: {task.id}")
            SafePrint.print(f"   调度: {format_schedule_str(task)}")
            if task.next_run:
                SafePrint.print(f"   下次执行: {task.next_run[:16]}")
            if task.depends_on:
                SafePrint.print(f"   依赖: {task.depends_on}")
            if task.retry_count > 0:
                SafePrint.print(f"   重试: {task.retry_count}次，间隔{task.retry_delay}秒")
    
    elif args.command == 'list':
        tasks = scheduler.list_tasks(group=args.group)
        print_tasks_table(tasks)
    
    elif args.command == 'remove':
        if scheduler.remove_task(args.name, auto_renumber=not args.no_renumber):
            SafePrint.success(f"任务 '{args.name}' 已删除")
    
    elif args.command == 'pause':
        if args.group:
            tasks = [t for t in scheduler.tasks if t.group == args.group]
            if not tasks:
                SafePrint.error(f"分组 '{args.group}' 下没有任务")
                return
            for task in tasks:
                scheduler.pause_task(task.name)
            SafePrint.success(f"分组 '{args.group}' 的所有任务已暂停")
        elif args.name:
            if scheduler.pause_task(args.name):
                SafePrint.success(f"任务 '{args.name}' 已暂停")
        else:
            SafePrint.error("请指定任务名称或 --group 分组")
    
    elif args.command == 'enable':
        if scheduler.enable_task(args.name):
            SafePrint.success(f"任务 '{args.name}' 已启用")

    elif args.command == 'update':
        # 调度参数：最多给一种，给了就整体换调度（类型 + 值必须成对）
        picked: List[tuple] = []
        if args.at_startup:
            picked.append(('startup', 'onlogon'))
        if args.daily:
            picked.append(('daily', args.daily))
        if args.weekly:
            picked.append(('weekly', f"{args.weekly[0]} {args.weekly[1]}"))
        if args.monthly:
            picked.append(('monthly', f"{args.monthly[0]} {args.monthly[1]}"))
        if args.hourly:
            picked.append(('hourly', '0'))
        if args.interval:
            picked.append(('interval', args.interval))
        if args.every_days:
            picked.append(('every_days', f"{args.every_days[0]}|{args.every_days[1]}"))
        if len(picked) > 1:
            SafePrint.error("一次只能指定一种调度：--at-startup / --daily / --weekly / "
                            "--monthly / --hourly / --interval / --every-days")
            sys.exit(1)

        kwargs: Dict[str, Any] = {}
        if picked:
            kwargs['schedule_type'], kwargs['schedule_value'] = picked[0]

        if args.new_name is not _UNSET:
            kwargs['new_name'] = args.new_name
        if args.task_command is not _UNSET:
            kwargs['command'] = args.task_command
        if args.status:
            kwargs['status'] = args.status
        for arg_name, field_name in (
                ('group', 'group'), ('depends_on', 'depends_on'),
                ('working_dir', 'working_dir'),
                ('notify_on_success', 'notify_on_success'),
                ('notify_on_fail', 'notify_on_fail'),
                ('slack_webhook', 'slack_webhook'), ('email', 'email')):
            value = getattr(args, arg_name)
            if value is not _UNSET:
                kwargs[field_name] = value

        if args.env:
            env_vars = {}
            for item in args.env:
                if '=' not in item:
                    SafePrint.error(f"环境变量格式应为 KEY=VALUE: {item}")
                    sys.exit(1)
                key, value = item.split('=', 1)
                env_vars[key] = value
            kwargs['env_vars'] = env_vars
        elif args.clear_env:
            kwargs['env_vars'] = {}

        if args.retry_count is not _UNSET:
            kwargs['retry_count'] = args.retry_count
        if args.retry_delay is not _UNSET:
            kwargs['retry_delay'] = args.retry_delay

        if not scheduler.update_task(args.name, dry_run=args.dry_run, **kwargs):
            sys.exit(1)
    
    elif args.command == 'run-now':
        scheduler.run_task_now(args.name)
    
    elif args.command == 'logs':
        logs = scheduler.get_logs(
            task_name=args.task,
            last=args.last,
            today=args.today,
            failed=args.failed
        )
        
        if not logs:
            SafePrint.print("\n[暂无日志]\n")
            return
        
        SafePrint.print(f"\n显示最近 {len(logs)} 条日志:\n")
        for log in logs:
            status_icon = "[OK]" if log['status'] == 'SUCCESS' else "[FAIL]"
            SafePrint.print(f"{status_icon} [{log['executed_at']}] {log['task_name']}")
            if args.failed or len(logs) <= 10:
                if log['output']:
                    SafePrint.print(f"输出: {log['output'][:500]}")
                if log['error']:
                    SafePrint.print(f"错误: {log['error'][:500]}")
                SafePrint.print("")
    
    elif args.command == 'export':
        scheduler.export_config(args.file)
    
    elif args.command == 'import':
        if not scheduler.import_config(args.file, overwrite=args.overwrite,
                                       dry_run=args.dry_run):
            sys.exit(1)

    elif args.command == 'repair':
        ok = scheduler.repair_tasks(dry_run=args.dry_run, prune=args.prune, only=args.task)
        if ok:
            SafePrint.success("系统计划任务自检通过")
        elif args.dry_run:
            SafePrint.warn("检测到与数据库不一致的项（--dry-run 未做修改）")
            sys.exit(1)
        else:
            SafePrint.error("存在未能修复的项，见上表")
            sys.exit(1)

    elif args.command == 'renumber':
        if not scheduler.renumber_tasks(dry_run=args.dry_run):
            sys.exit(1)


if __name__ == '__main__':
    main()
