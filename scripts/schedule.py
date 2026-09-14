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
from dataclasses import dataclass, asdict, field
from enum import Enum
import threading
import traceback
import calendar

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


class WindowsNotifier:
    """Windows 原生通知"""
    @staticmethod
    def notify(title: str, message: str, icon_type: str = "info"):
        """发送 Windows 通知"""
        if sys.platform != 'win32':
            return
        
        try:
            # 使用 PowerShell 发送通知
            icon_map = {
                "info": "Information",
                "warning": "Warning",
                "error": "Error"
            }
            icon = icon_map.get(icon_type, "Information")
            
            ps_script = f'''
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::{icon}
$notify.Visible = $true
$notify.ShowBalloonTip(5000, "{title}", "{message}", [System.Windows.Forms.ToolTipIcon]::{icon})
Start-Sleep -Seconds 6
$notify.Dispose()
'''
            subprocess.run(
                ["powershell", "-Command", ps_script],
                capture_output=True,
                timeout=10,
                encoding='utf-8',
                errors='replace'
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
        """把 DB 的 next_run 对齐到 Windows 权威值；有变化则写库。返回是否已更新。"""
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
        
        # 设置系统级调度器
        scheduler_result = self._setup_system_scheduler(task)
        if not scheduler_result:
            SafePrint.warn("系统调度器设置失败，但任务已保存到数据库")
        
        return task
    
    def _setup_system_scheduler(self, task: Task) -> bool:
        """设置系统级定时任务"""
        try:
            if self.platform == Platform.WINDOWS:
                return self._setup_windows_task(task)
            elif self.platform == Platform.MACOS:
                return self._setup_macos_task(task)
            else:
                return self._setup_linux_task(task)
        except Exception as e:
            SafePrint.error(f"系统调度器设置失败: {e}")
            return False
    
    def _setup_windows_task(self, task: Task) -> bool:
        """设置 Windows Task Scheduler"""
        try:
            task_name = f"TaskScheduler-{task.id}-{task.name.replace(' ', '_')}"
            
            # 删除已存在的任务
            subprocess.run(['schtasks', '/Delete', '/TN', task_name, '/F'],
                         capture_output=True, encoding='utf-8', errors='replace')
            
            # 构建 schtasks 命令
            schedule_map = {
                'hourly': ['/SC', 'HOURLY'],
                'daily': ['/SC', 'DAILY'],
                'every_days': ['/SC', 'DAILY'],
                'weekly': ['/SC', 'WEEKLY'],
                'monthly': ['/SC', 'MONTHLY']
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
            
            # 构建执行命令
            # 用 pythonw.exe（无控制台）而非 python.exe：计划任务由 Task Scheduler
            # 启动时没有控制台，python.exe 会弹出一个命令窗口并在任务执行期间一直
            # 亮着。pythonw 是无窗口解释器，配合 SafePrint 对 stdout=None 的兜底，
            # 执行期不再出现窗口。
            # 【注意】pythonw 下 sys.executable 也是 pythonw.exe，会让子进程一并变成
            # 无窗口进程而丢失管道输出 —— 故 _execute_task 里另有"子进程强制用
            # python.exe"的处理，见该处注释。
            # 找不到 pythonw 时回退 python.exe（仅美观受影响）。
            python_exe = sys.executable
            pythonw_exe = Path(python_exe).with_name("pythonw.exe")
            launcher_exe = str(pythonw_exe) if pythonw_exe.exists() else python_exe
            script_path = Path(__file__).resolve()
            run_cmd = f'"{launcher_exe}" "{script_path}" internal-run {task.id}'
            
            cmd_parts.extend(['/TR', run_cmd, '/F'])
            
            # 执行命令
            result = subprocess.run(cmd_parts, capture_output=True, text=True, encoding='utf-8', errors='replace')
            
            if result.returncode == 0:
                SafePrint.success(f"Windows Task Scheduler 任务已创建: {task_name}")
                # schtasks 会继承 Windows 默认设置（用电池不启动、错过不补跑），
                # 必须显式改写，否则任务会"已启用但到点不执行"
                self._apply_windows_task_settings(task_name, enabled=(task.status == "active"))
                # 让 DB 的 next_run 对齐 Windows 权威网格（StartBoundary + DaysInterval）。
                # 用 created_at 推算会与系统差一天（整点锚点 vs 带时分秒的创建时刻）。
                if self._sync_next_run_with_windows(task):
                    SafePrint.info(f"next_run 已按 Windows 网格校准为 {task.next_run}")
                return True
            else:
                SafePrint.error(f"Windows 任务创建失败: {result.stderr}")
                return False
                
        except Exception as e:
            SafePrint.error(f"Windows 任务设置失败: {e}")
            return False

    def _apply_windows_task_settings(self, task_name: str, enabled: bool = True) -> bool:
        """修正 Windows 任务的电源与补偿设置

        `schtasks /Create` 生成的任务会带上 Windows 的默认设置：
        - DisallowStartIfOnBatteries=True   用电池时直接不启动
        - StopIfGoingOnBatteries=True       切到电池就把任务杀掉
        - StartWhenAvailable=False          到点时机器关机/睡眠就永久丢弃该次执行

        三者叠加的结果就是"任务状态是已启用，但到点不跑"，任务计划程序里
        显示最后一次结果为 0x800710E0（操作员或管理员拒绝了请求）。
        这里统一改成：允许电池启动、切电池不终止、错过的执行尽快补跑。

        注意 `Set-ScheduledTask -Settings` 会连 Settings.Enabled 一起覆盖成默认
        True，所以必须按调用方给的状态显式 Enable/Disable，否则暂停的任务会被
        静默启用，与数据库中的 paused 状态漂移。
        """
        if self.platform != Platform.WINDOWS:
            return True

        safe_name = task_name.replace("'", "''")
        state_cmd = 'Enable-ScheduledTask' if enabled else 'Disable-ScheduledTask'
        ps_script = (
            "$ErrorActionPreference='Stop';"
            "$name = '" + safe_name + "';"
            "$s = New-ScheduledTaskSettingsSet "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable "
            "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 72);"
            "Set-ScheduledTask -TaskName $name -Settings $s | Out-Null;"
            f"{state_cmd} -TaskName $name | Out-Null;"
            # 回读校验：命令返回 0 只说明调用没报错，不等于设置真的落盘。
            # 这里把实际生效值读回来，失败即判为未生效，避免"任务已建但设置没应用"。
            "$t = Get-ScheduledTask -TaskName $name;"
            "$x = [xml](Export-ScheduledTask -TaskName $name);"
            "$want = 'DIB=' + [string]$x.Task.Settings.DisallowStartIfOnBatteries + "
            "' SIB=' + [string]$x.Task.Settings.StopIfGoingOnBatteries + "
            "' SWA=' + [string]$x.Task.Settings.StartWhenAvailable + "
            "' MIP=' + [string]$x.Task.Settings.MultipleInstancesPolicy;"
            "'VERIFY state=' + [string]$t.State + ' ' + $want"
        )
        try:
            result = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_script],
                capture_output=True, text=True, encoding='utf-8', errors='replace')
        except FileNotFoundError:
            SafePrint.warn("未找到 powershell，未能修正 Windows 任务电源设置")
            return False

        # 校验回读结果：只有四项设置都符合预期才算成功
        verify_line = ""
        for line in (result.stdout or "").splitlines():
            if line.strip().startswith("VERIFY "):
                verify_line = line.strip()
                break

        want_state = "Running" if enabled else "Disabled"
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
            if parts.get("state") != want_state:
                bad.append(f"state={parts.get('state')}（期望 {want_state}）")

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
        return False
    
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
    <key>StartCalendarInterval</key>
    <dict>{interval_xml}
    </dict>
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
    
    def remove_task(self, name: str) -> bool:
        """删除任务"""
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
                self._apply_windows_task_settings(task_name, enabled=True)
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
            if sys.platform == 'win32':
                # 创建临时 wrapper 脚本
                import tempfile
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
            else:
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
    
    def import_config(self, input_file: str):
        """导入配置"""
        try:
            with open(input_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            imported = 0
            for task_data in data.get('tasks', []):
                # 移除 id，让系统自动分配
                task_data.pop('id', None)
                task = Task(**task_data)
                task.id = self._get_next_id()
                
                if self.db.save_task(task):
                    self.tasks.append(task)
                    self._setup_system_scheduler(task)
                    imported += 1
            
            SafePrint.success(f"成功导入 {imported} 个任务")
        except Exception as e:
            SafePrint.error(f"导入失败: {e}")
            traceback.print_exc()


def format_schedule_str(task: Task) -> str:
    """生成人类可读的调度描述（中文）"""
    st = task.schedule_type
    sv = task.schedule_value
    weekdays = {
        'mon': '周一', 'tue': '周二', 'wed': '周三',
        'thu': '周四', 'fri': '周五', 'sat': '周六', 'sun': '周日'
    }
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
    parser = argparse.ArgumentParser(        description='跨平台定时任务管理器 v2.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 添加每日任务
  %(prog)s add "整理下载" --command "python organize.py ~/Downloads" --daily 02:00
  
  # 添加带依赖的任务
  %(prog)s add "数据同步" --command "sync.sh" --daily 01:00 --depends-on "数据备份"
  
  # 添加带重试的任务
  %(prog)s add "重要任务" --command "job.sh" --daily 12:00 --retry-count 5 --retry-delay 300
  
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
                           help='失败重试次数（默认3）')
    add_parser.add_argument('--retry-delay', type=int, default=60,
                           help='重试间隔秒数（默认60）')
    
    # list 命令
    list_parser = subparsers.add_parser('list', help='列出所有任务')
    list_parser.add_argument('--group', help='按分组筛选')
    
    # remove 命令
    remove_parser = subparsers.add_parser('remove', help='删除任务')
    remove_parser.add_argument('name', help='任务名称')
    
    # pause 命令
    pause_parser = subparsers.add_parser('pause', help='暂停任务')
    pause_parser.add_argument('name', nargs='?', help='任务名称')
    pause_parser.add_argument('--group', help='暂停整个分组')
    
    # enable 命令
    enable_parser = subparsers.add_parser('enable', help='启用任务')
    enable_parser.add_argument('name', help='任务名称')
    
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
        
        if args.daily:
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
            SafePrint.error("请指定调度时间（--daily, --weekly, --monthly, --hourly, --interval, --every-days）")
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
        if scheduler.remove_task(args.name):
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
        scheduler.import_config(args.file)


if __name__ == '__main__':
    main()
