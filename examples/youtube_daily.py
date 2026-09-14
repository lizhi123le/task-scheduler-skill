#!/usr/bin/env python3
"""
YouTube 每日热门视频下载器 v5.0 - 高成功率优化版本
核心改进：
1. 多客户端轮换策略（Android/TV/iOS/Web）
2. 智能请求间隔避免被封
3. Cookie支持绕过限制
4. 指数退避重试
5. 视频完整性验证
6. 更丰富的频道备选
7. 支持并行下载
8. 支持指定类别

使用: 
  正常模式: python youtube_daily.py
  快速测试: python youtube_daily.py --test
  并行下载: python youtube_daily.py --workers 3
  指定类别: python youtube_daily.py --categories 科技 新闻 美食
  组合使用: python youtube_daily.py --workers 2 --categories 科技 教育 --test
"""

# [重要] 在任何其他导入之前设置编码环境
import sys
import os

if sys.platform == 'win32':
    # 1. 设置 Windows 控制台代码页为 UTF-8
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
    
    # 2. 设置环境变量（必须在这时候设置）
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8MODE'] = '1'
    os.environ['PYTHONLEGACYWINDOWSSTDIO'] = 'utf-8'
    os.environ['LC_ALL'] = 'C.UTF-8'
    os.environ['LANG'] = 'C.UTF-8'

import subprocess
import json
import random
import time
import sqlite3
import re
import argparse
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# ============ 配置 ============

class DownloadStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"

@dataclass
class DownloadResult:
    status: DownloadStatus
    file_path: Optional[str] = None
    error_msg: Optional[str] = None
    strategy_used: Optional[str] = None

# 扩展的热门频道配置（更多备选，确保成功率）
YOUTUBE_SOURCES: dict[str, list[str]] = {
    "科技": [
        "https://www.youtube.com/@LinusTechTips/videos",
        "https://www.youtube.com/@MKBHD/videos",
        "https://www.youtube.com/@UnboxTherapy/videos",
        "https://www.youtube.com/@Dave2D/videos",
        "https://www.youtube.com/@MarquesBrownlee/videos",
        "https://www.youtube.com/@TechLinked/videos",
        "https://www.youtube.com/@HardwareCanucks/videos",
        "https://www.youtube.com/@AustinEvans/videos",
    ],
    "新闻": [
        "https://www.youtube.com/@BBCNews/videos",
        "https://www.youtube.com/@CNN/videos",
        "https://www.youtube.com/@Reuters/videos",
        "https://www.youtube.com/@AssociatedPress/videos",
        "https://www.youtube.com/@ABCNews/videos",
        "https://www.youtube.com/@NBCNews/videos",
        "https://www.youtube.com/@CBSNews/videos",
        "https://www.youtube.com/@AlJazeeraEnglish/videos",
    ],
    "教育": [
        "https://www.youtube.com/@TED/videos",
        "https://www.youtube.com/@freecodecamp/videos",
        "https://www.youtube.com/@KhanAcademy/videos",
        "https://www.youtube.com/@MITOpenCourseWare/videos",
        "https://www.youtube.com/@CrashCourse/videos",
        "https://www.youtube.com/@3Blue1Brown/videos",
        "https://www.youtube.com/@Veritasium/videos",
        "https://www.youtube.com/@Vsauce/videos",
    ],
    "娱乐": [
        "https://www.youtube.com/@MrBeast/videos",
        "https://www.youtube.com/@PewDiePie/videos",
        "https://www.youtube.com/@DudePerfect/videos",
        "https://www.youtube.com/@5-MinuteCrafts/videos",
        "https://www.youtube.com/@JennaMarbles/videos",
        "https://www.youtube.com/@GoodMythicalMorning/videos",
        "https://www.youtube.com/@TheEllenShow/videos",
        "https://www.youtube.com/@JimmyFallon/videos",
    ],
    "音乐": [
        "https://www.youtube.com/@VEVO/videos",
        "https://www.youtube.com/@Billboard/videos",
        "https://www.youtube.com/@Spotify/videos",
        "https://www.youtube.com/@TrapNation/videos",
        "https://www.youtube.com/@NoCopyrightSounds/videos",
        "https://www.youtube.com/@Monstercat/videos",
        "https://www.youtube.com/@UltraRecords/videos",
        "https://www.youtube.com/@ArmadaMusic/videos",
    ],
    "游戏": [
        "https://www.youtube.com/@Markiplier/videos",
        "https://www.youtube.com/@Jacksepticeye/videos",
        "https://www.youtube.com/@DanTDM/videos",
        "https://www.youtube.com/@TheGameTheorists/videos",
        "https://www.youtube.com/@PewDiePie/videos",
        "https://www.youtube.com/@Ninja/videos",
        "https://www.youtube.com/@VanossGaming/videos",
        "https://www.youtube.com/@SSSniperWolf/videos",
    ],
    "美食": [
        "https://www.youtube.com/@BuzzFeedTasty/videos",
        "https://www.youtube.com/@BonAppetit/videos",
        "https://www.youtube.com/@GordonRamsay/videos",
        "https://www.youtube.com/@JoshuaWeissman/videos",
        "https://www.youtube.com/@babishculinaryuniverse/videos",
        "https://www.youtube.com/@NickDiGiovanni/videos",
        "https://www.youtube.com/@Maangchi/videos",
        "https://www.youtube.com/@SortedFood/videos",
        "https://www.youtube.com/@FoodWishes/videos",
        "https://www.youtube.com/@AdamRagusatv/videos",
    ],
    "旅游": [
        "https://www.youtube.com/@YesTheory/videos",
        "https://www.youtube.com/@KaraandNate/videos",
        "https://www.youtube.com/@LostLeBlanc/videos",
        "https://www.youtube.com/@drewbinsky/videos",
        "https://www.youtube.com/@EvaZuBeck/videos",
        "https://www.youtube.com/@MrBenBrown/videos",
        "https://www.youtube.com/@TheBucketListFamily/videos",
        "https://www.youtube.com/@IndigoTraveller/videos",
        "https://www.youtube.com/@DangerousBiz/videos",
        "https://www.youtube.com/@TheEndlessAdventure/videos",
    ],
    "萌宠": [
        "https://www.youtube.com/@ColeandMarmalade/videos",
        "https://www.youtube.com/@TheDodo/videos",
        "https://www.youtube.com/@KittenLady/videos",
        "https://www.youtube.com/@GoneToTheSnowDogs/videos",
        "https://www.youtube.com/@TuckerBudzyn/videos",
        "https://www.youtube.com/@Maymo/videos",
        "https://www.youtube.com/@RumbleVirals/videos",
        "https://www.youtube.com/@ViralHog/videos",
    ],
    "健身": [
        "https://www.youtube.com/@AthleanX/videos",
        "https://www.youtube.com/@ChloeTing/videos",
        "https://www.youtube.com/@THENX/videos",
        "https://www.youtube.com/@JeffNippard/videos",
        "https://www.youtube.com/@Blogilates/videos",
        "https://www.youtube.com/@MadFit/videos",
        "https://www.youtube.com/@PamelaRf1/videos",
        "https://www.youtube.com/@ChrisBumstead/videos",
    ],
    "投掷": [
        "https://www.youtube.com/@BrodieSmith/videos",
        "https://www.youtube.com/@HowRidiculous/videos",
        "https://www.youtube.com/@DudePerfect/videos",
        "https://www.youtube.com/@RickSmithJr/videos",
        "https://www.youtube.com/@Trickshotdavideo/videos",
        "https://www.youtube.com/@ThatsAmazing/videos",
        "https://www.youtube.com/@babanzhang",
        "https://www.youtube.com/@AdamCeladin/videos",
        "https://www.youtube.com/@ChrisGreenYT/videos",
        "https://www.youtube.com/@WorldKnifeThrowing/videos",
        "https://www.youtube.com/@JackDagger/videos",
        "https://www.youtube.com/@ScottSlinger/videos",
        "https://www.youtube.com/@DavidAndresThrowing/videos",
        "https://www.youtube.com/@DeadlyKitten/videos",
        "https://www.youtube.com/@stevenjameskoe/videos",
        "https://www.youtube.com/@timothysb/videos",
        "https://www.youtube.com/@MingDongSong/videos",
        "https://www.youtube.com/@POV throwing/videos",
        "https://www.youtube.com/@throwingshow/videos",
        "https://www.youtube.com/@KnifeThrowingGuy/videos",
    ],
    "搞笑": [
        "https://www.youtube.com/@Smosh/videos",
        "https://www.youtube.com/@FailArmy/videos",
        "https://www.youtube.com/@MrBeast/videos",
        "https://www.youtube.com/@ViralHog/videos",
        "https://www.youtube.com/@ComedyCentral/videos",
        "https://www.youtube.com/@SaturdayNightLive/videos",
        "https://www.youtube.com/@JustForLaughs/videos",
        "https://www.youtube.com/@TryGuys/videos",
    ],
    "测评": [
        "https://www.youtube.com/@JerryRigEverything/videos",
        "https://www.youtube.com/@ShortCircuit/videos",
        "https://www.youtube.com/@UrAvgConsumer/videos",
        "https://www.youtube.com/@Mrwhosetheboss/videos",
        "https://www.youtube.com/@randomfrankp/videos",
        "https://www.youtube.com/@AndroidAuthority/videos",
    ],
}

# 优化的下载配置
OUTPUT_DIR: str = "D:/Users/lizhi/Downloads/YouTube_Daily"
MAX_RETRIES_PER_VIDEO: int = 4
TIMEOUT_SECONDS: float = 900.0  # 15分钟
MAX_VIDEOS_TO_CHECK: int = 8
GLOBAL_TIMEOUT: float = 2400.0  # 40分钟
MIN_REQUEST_INTERVAL: float = 3.0
MAX_REQUEST_INTERVAL: float = 8.0
COOKIE_FILE: Optional[str] = None
USE_PROXY: bool = False
PROXY_URL: Optional[str] = None

# 成功率阈值配置（关键修复：不要求100%成功率）
MIN_SUCCESS_RATE: float = 0.3  # 最低30%类别成功率即可视为任务成功
MIN_SUCCESS_CATEGORIES: int = 1  # 至少成功下载1个类别

# 命令行参数解析
def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='YouTube 每日热门视频下载器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                          # 下载所有类别（单线程）
  %(prog)s --workers 3              # 并行下载3个类别
  %(prog)s --categories 科技 新闻    # 只下载科技、新闻类别
  %(prog)s --categories all            # 下载所有类别（等于不指定）
  %(prog)s -w 2 -c 科技 美食 --test  # 并行下载科技、美食类别（测试模式）
        """
    )
    
    parser.add_argument(
        '-w', '--workers',
        type=int,
        default=1,
        help='并行下载的任务数（默认: 1，建议: 2-4）'
    )
    
    parser.add_argument(
        '-c', '--categories',
        nargs='+',
        metavar='CATEGORY',
        help=f'指定要下载的类别，可选: {", ".join(YOUTUBE_SOURCES.keys())} 或 "all" 表示全部'
    )
    
    parser.add_argument(
        '--test',
        action='store_true',
        help='启用测试模式（减少延迟和重试次数）'
    )
    
    parser.add_argument(
        '--min-success-rate',
        type=float,
        default=0.3,
        metavar='RATE',
        help='最低成功率阈值（0.0-1.0，默认: 0.3）'
    )
    
    parser.add_argument(
        '--min-success-categories',
        type=int,
        default=1,
        metavar='N',
        help='最少成功类别数（默认: 1）'
    )
    
    return parser.parse_args()

# 解析命令行参数
ARGS = parse_args()
TEST_MODE = ARGS.test
SELECTED_CATEGORIES = ARGS.categories
MAX_WORKERS = ARGS.workers

# 应用命令行参数到配置
MIN_SUCCESS_RATE = ARGS.min_success_rate
MIN_SUCCESS_CATEGORIES = ARGS.min_success_categories

# 处理 "all" 参数
if SELECTED_CATEGORIES and 'all' in SELECTED_CATEGORIES:
    SELECTED_CATEGORIES = None  # None 表示使用所有类别
elif SELECTED_CATEGORIES:
    SELECTED_CATEGORIES = SELECTED_CATEGORIES
else:
    SELECTED_CATEGORIES = None

if TEST_MODE:
    print("[TEST MODE] 启用快速测试模式 - 减少延迟和重试次数")
    MIN_REQUEST_INTERVAL = 0.5
    MAX_REQUEST_INTERVAL = 1.0
    MAX_RETRIES_PER_VIDEO = 1
    TIMEOUT_SECONDS = 60.0
    GLOBAL_TIMEOUT = 300.0

# 多客户端策略配置（按成功率排序）
DOWNLOAD_STRATEGIES: list[dict[str, Any]] = [
    {
        "name": "TV Client (高成功率)",
        "format": "best[height<=1080]/best[height<=720]/best",
        "extractor_args": "youtube:player_client=tv",
        "extra_args": ["--merge-output-format", "mp4", "--no-check-certificates"],
        "priority": 1,
    },
    {
        "name": "Android Client VRR",
        "format": "best[height<=1080]/best[height<=720]/best",
        "extractor_args": "youtube:player_client=android_vr",
        "extra_args": ["--merge-output-format", "mp4", "--no-check-certificates"],
        "priority": 2,
    },
    {
        "name": "iOS Client",
        "format": "best[height<=1080]/best[height<=720]/best",
        "extractor_args": "youtube:player_client=ios",
        "extra_args": ["--merge-output-format", "mp4"],
        "priority": 3,
    },
    {
        "name": "Web Client (带模拟)",
        "format": "best[height<=720]/best[height<=480]/best",
        "extractor_args": "youtube:player_client=web",
        "extra_args": ["--merge-output-format", "mp4", "--add-header", "Referer:https://www.youtube.com/"],
        "priority": 4,
    },
    {
        "name": "Medium Quality (稳定)",
        "format": "best[height<=480]/best[height<=360]/best",
        "extractor_args": None,
        "extra_args": ["--merge-output-format", "mp4"],
        "priority": 5,
    },
    {
        "name": "Simple MP4 (保底)",
        "format": "18",  # 360p mp4
        "extractor_args": None,
        "extra_args": [],
        "priority": 6,
    },
    {
        "name": "Audio Only (最后手段)",
        "format": "bestaudio/best",
        "extractor_args": None,
        "extra_args": ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"],
        "priority": 7,
    },
]


# ============ 下载历史管理器 ============

class DownloadHistory:
    """
    下载历史管理器 - 使用SQLite跟踪已下载视频，避免重复下载
    """
    
    def __init__(self):
        self.db_dir = Path.home() / ".config" / "task-scheduler" / "data"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "youtube_downloads.db"
        self._init_db()
    
    def _init_db(self) -> None:
        """初始化数据库表"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # 创建下载历史表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloaded_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT UNIQUE NOT NULL,
                video_url TEXT NOT NULL,
                category TEXT NOT NULL,
                channel TEXT NOT NULL,
                title TEXT,
                file_path TEXT,
                file_size_mb REAL,
                download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'success'  -- success, failed, skipped
            )
        ''')
        
        # 创建索引加快查询
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_video_id ON downloaded_videos(video_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_category ON downloaded_videos(category)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_download_date ON downloaded_videos(download_date)
        ''')
        
        conn.commit()
        conn.close()
        
        # 初始化时执行一次清理
        self.cleanup_old_records(keep_days=2)
    
    def extract_video_id(self, url: str) -> Optional[str]:
        """
        从YouTube URL中提取视频ID
        支持格式:
        - https://www.youtube.com/watch?v=VIDEO_ID
        - https://youtu.be/VIDEO_ID
        - https://www.youtube.com/embed/VIDEO_ID
        """
        patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})',
            r'youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None
    
    def is_downloaded(self, video_id: str) -> bool:
        """检查视频是否已下载过"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT COUNT(*) FROM downloaded_videos WHERE video_id = ? AND status = 'success'",
            (video_id,)
        )
        count = cursor.fetchone()[0]
        
        conn.close()
        return count > 0
    
    def add_record(self, video_id: str, video_url: str, category: str, channel: str,
                   title: str = "", file_path: str = "", file_size_mb: float = 0.0,
                   status: str = "success") -> None:
        """添加下载记录，并自动清理旧记录"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        try:
            cursor.execute('''
                INSERT OR REPLACE INTO downloaded_videos
                (video_id, video_url, category, channel, title, file_path, file_size_mb, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (video_id, video_url, category, channel, title, file_path, file_size_mb, status))
            conn.commit()

            # 添加记录后执行清理，只保留最近两个日期的记录
            self.cleanup_old_records(keep_days=2)

        except Exception as e:
            print(f"[WARN] 保存下载记录失败: {e}")
        finally:
            conn.close()

    def cleanup_old_records(self, keep_days: int = 2) -> int:
        """
        清理旧的下载记录，只保留最近 N 个日期的记录

        例如：
        - 如果记录日期为：2025-02-06, 2025-02-05, 2025-02-04, 2025-02-03
        - keep_days=2: 保留 2025-02-06, 2025-02-05，删除 2025-02-04, 2025-02-03

        返回: 删除的记录数
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        deleted_count = 0

        try:
            # 获取所有不同的日期（按日期降序）
            cursor.execute('''
                SELECT DISTINCT date(download_date) as download_day
                FROM downloaded_videos
                WHERE status = 'success'
                ORDER BY download_day DESC
            ''')

            dates = [row[0] for row in cursor.fetchall()]

            if len(dates) <= keep_days:
                # 不需要清理
                return 0

            # 保留最近 keep_days 个日期，删除更旧的
            dates_to_delete = dates[keep_days:]

            for old_date in dates_to_delete:
                cursor.execute('''
                    DELETE FROM downloaded_videos
                    WHERE date(download_date) = ? AND status = 'success'
                ''', (old_date,))

                count = cursor.rowcount
                deleted_count += count
                print(f"  [清理] 旧记录 [{old_date}]: 删除 {count} 条")

            conn.commit()

            if deleted_count > 0:
                print(f"[OK] 历史记录清理完成: 保留最近 {keep_days} 个日期，删除 {deleted_count} 条旧记录")

        except Exception as e:
            print(f"[WARN] 清理旧记录失败: {e}")
        finally:
            conn.close()

        return deleted_count
    
    def get_download_stats(self) -> dict[str, Any]:
        """获取下载统计信息"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # 总下载数
        cursor.execute("SELECT COUNT(*) FROM downloaded_videos WHERE status = 'success'")
        total_downloads = cursor.fetchone()[0]
        
        # 各类别下载数
        cursor.execute('''
            SELECT category, COUNT(*) as count 
            FROM downloaded_videos 
            WHERE status = 'success'
            GROUP BY category
        ''')
        category_stats = {row[0]: row[1] for row in cursor.fetchall()}
        
        # 最近7天下载数
        cursor.execute('''
            SELECT COUNT(*) FROM downloaded_videos 
            WHERE status = 'success' 
            AND download_date >= datetime('now', '-7 days')
        ''')
        recent_downloads = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "total_downloads": total_downloads,
            "category_stats": category_stats,
            "recent_downloads": recent_downloads
        }
    
    def deduplicate_video_list(self, video_urls: list[str], category: str) -> tuple[list[str], list[str]]:
        """
        过滤已下载的视频，返回未下载的视频列表和跳过的视频列表
        
        返回: (未下载的视频列表, 已下载的视频ID列表)
        """
        new_urls = []
        skipped_ids = []
        
        for url in video_urls:
            video_id = self.extract_video_id(url)
            if not video_id:
                # 无法提取ID，保留该URL
                new_urls.append(url)
                continue
            
            if self.is_downloaded(video_id):
                skipped_ids.append(video_id)
                logger.info(f"  [SKIP] 视频已下载过: {video_id}")
            else:
                new_urls.append(url)
        
        return new_urls, skipped_ids


# 全局下载历史管理器实例
download_history = DownloadHistory()

# 线程锁（用于并行下载时的日志同步）
log_lock = Lock()

# ============ 日志工具 ============

@dataclass
class Logger:
    """线程安全的日志记录器"""
    log_dir: Path = field(init=False)
    log_file: Path = field(init=False)
    _lock: Lock = field(default_factory=Lock, repr=False)
    
    def __post_init__(self):
        self.log_dir = Path.home() / ".config" / "task-scheduler" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"youtube_daily_v4_{datetime.now().strftime('%Y%m%d')}.log"
    
    def log(self, msg: str, level: str = "INFO") -> None:
        """记录日志（线程安全）"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        
        # 使用全局锁确保多线程下日志不混乱
        with log_lock:
            print(line)
            
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                print(f"[ERROR] Failed to write log: {e}")
    
    def info(self, msg: str) -> None:
        self.log(msg, "INFO")
    
    def success(self, msg: str) -> None:
        self.log(msg, "SUCCESS")
    
    def warning(self, msg: str) -> None:
        self.log(msg, "WARN")
    
    def error(self, msg: str) -> None:
        self.log(msg, "ERROR")

logger = Logger()

# ============ 核心功能 ============

def random_delay(min_seconds: Optional[float] = None, max_seconds: Optional[float] = None) -> float:
    """随机延迟，避免请求过于规律"""
    min_s = min_seconds if min_seconds is not None else MIN_REQUEST_INTERVAL
    max_s = max_seconds if max_seconds is not None else MAX_REQUEST_INTERVAL
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)
    return delay

def exponential_backoff(attempt: int, base_delay: float = 5.0, max_delay: float = 60.0) -> float:
    """指数退避延迟"""
    delay = min(base_delay * (2 ** attempt) + random.uniform(0, 2), max_delay)
    logger.info(f"退避等待: {delay:.1f}秒 (尝试 {attempt + 1})")
    time.sleep(delay)
    return delay

class CompletedProcessWrapper:
    """包装 CompletedProcess 以提供兼容的接口"""
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

def run_subprocess(cmd: list[str], timeout: float = 30, **kwargs) -> Any:
    """
    运行子进程并处理编码问题
    关键修复：在Windows上使用不同方法避免GBK编码问题
    """
    import io
    
    # 确保环境变量使用UTF-8编码
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8MODE'] = '1'
    env['LC_ALL'] = 'C.UTF-8'  # 使用C locale避免本地化编码
    
    # 移除可能干扰的参数
    kwargs.pop('encoding', None)
    kwargs.pop('errors', None)
    kwargs.pop('universal_newlines', None)
    kwargs.pop('text', None)
    kwargs.pop('capture_output', None)
    
    # 关键修复：使用subprocess.run替代Popen，并指定编码
    try:
        result = subprocess.run(
            cmd,
            env=env,
            timeout=timeout,
            encoding='utf-8',  # 强制UTF-8编码
            errors='replace',  # 替换无法解码的字符
            capture_output=True,
            **kwargs
        )
        return CompletedProcessWrapper(result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        raise
    except Exception as e:
        # 最后的兜底方案
        return CompletedProcessWrapper(-1, '', str(e)[:500])

def check_yt_dlp() -> bool:
    """检查 yt-dlp 并显示版本"""
    try:
        result = run_subprocess(["yt-dlp", "--version"], timeout=10)
        if result.returncode == 0:
            version = result.stdout.strip()
            logger.success(f"yt-dlp 版本: {version}")
            
            # 检查是否是最新版
            try:
                check_result = run_subprocess(["yt-dlp", "--update", "--dry-run"], timeout=15)
                if "Updated" in check_result.stdout or "Update" in check_result.stderr:
                    logger.warning("检测到 yt-dlp 有新版本，建议更新: yt-dlp --update")
            except Exception:
                pass
            
            return True
    except FileNotFoundError:
        logger.error("yt-dlp 未安装")
        logger.info("安装命令: pip install -U yt-dlp")
    except Exception as e:
        logger.error(f"检查 yt-dlp 失败: {e}")
    
    return False

def test_youtube_connection() -> bool:
    """测试YouTube连接是否正常"""
    logger.info("测试 YouTube 连接...")
    try:
        test_url = "https://www.youtube.com/@YouTube/videos"
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--playlist-items", "1:1",
            "--print", "%(webpage_url)s",
            "--no-warnings",
            "--extractor-args", "youtube:player_client=web",
            test_url
        ]
        
        result = run_subprocess(cmd, timeout=30)
        
        if result.returncode == 0 and result.stdout.strip():
            logger.success("[OK] YouTube 连接正常")
            return True
        else:
            error = result.stderr[:200] if result.stderr else "未知错误"
            logger.error(f"[FAIL] YouTube 连接测试失败: {error}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("[FAIL] YouTube 连接测试超时")
        return False
    except Exception as e:
        logger.error(f"[FAIL] YouTube 连接测试异常: {e}")
        return False

def cleanup_old_files(output_dir: Path, keep_days: int = 2) -> tuple[int, int]:
    """
    清理指定目录下所有类别文件夹中旧文件
    根据文件名中的日期判断，保留最近 N 个不同日期的所有文件，删除其他日期的文件
    
    文件名格式: YYYYMMDD_HHMMSS_标题.ext
    
    例如：
    - 20260115、20260106、20260103、20260101
    - keep_days=2: 保留 20260115、20260106 的所有文件，删除 20260103、20260101
    
    返回: (删除的文件数, 释放的空间MB)
    """
    from datetime import datetime
    
    deleted_count = 0
    freed_space_mb = 0.0
    
    logger.info(f"清理旧文件（每个类别保留最近 {keep_days} 个日期的文件）...")
    
    if not output_dir.exists():
        logger.info("输出目录不存在，无需清理")
        return 0, 0
    
    # 遍历所有类别文件夹
    for category in YOUTUBE_SOURCES.keys():
        category_dir = output_dir / category
        if not category_dir.exists():
            continue
        
        # 收集该类别下所有文件，按日期分组
        date_files: dict[datetime, list[Path]] = {}  # 日期 -> 文件列表
        
        try:
            for file_path in category_dir.iterdir():
                if file_path.is_file():
                    try:
                        # 从文件名解析日期 (格式: YYYYMMDD_HHMMSS_标题.ext)
                        file_name = file_path.stem  # 不含扩展名
                        date_part = file_name.split('_')[0]  # 取第一部分 YYYYMMDD
                        
                        if len(date_part) == 8 and date_part.isdigit():
                            file_date = datetime.strptime(date_part, '%Y%m%d')
                            if file_date not in date_files:
                                date_files[file_date] = []
                            date_files[file_date].append(file_path)
                        else:
                            # 无法解析日期的文件，保留
                            pass
                    except Exception as e:
                        logger.warning(f"  处理文件失败 [{category}] {file_path.name}: {e}")
        except Exception as e:
            logger.warning(f"  扫描类别文件夹失败 [{category}]: {e}")
            continue
        
        # 按日期降序排序（最新的在前）
        sorted_dates = sorted(date_files.keys(), reverse=True)
        
        # 保留前 N 个日期的所有文件
        dates_to_keep = sorted_dates[:keep_days]
        dates_to_delete = sorted_dates[keep_days:]
        
        # 删除文件
        for file_date in dates_to_delete:
            for file_path in date_files[file_date]:
                try:
                    file_size = file_path.stat().st_size
                    file_path.unlink()
                    deleted_count += 1
                    freed_space_mb += file_size / (1024 * 1024)
                    logger.info(f"  删除旧文件 [{category}]: {file_path.name} ({file_date.strftime('%Y-%m-%d')})")
                except Exception as e:
                    logger.warning(f"  删除文件失败 [{category}] {file_path.name}: {e}")
        
        if dates_to_delete:
            kept_count = sum(len(date_files[d]) for d in dates_to_keep)
            deleted_file_count = sum(len(date_files[d]) for d in dates_to_delete)
            logger.info(f"  [{category}] 保留 {kept_count} 个文件（{len(dates_to_keep)} 个日期），删除 {deleted_file_count} 个旧文件（{len(dates_to_delete)} 个日期）")
    
    if deleted_count > 0:
        logger.success(f"清理完成: 删除 {deleted_count} 个旧文件, 释放 {freed_space_mb:.1f} MB")
    else:
        logger.info("没有需要清理的旧文件")
    
    return deleted_count, int(freed_space_mb)

def get_video_info(url: str) -> Optional[Dict[str, Any]]:
    """获取视频信息，用于验证视频是否可访问"""
    try:
        cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-download",
            "--no-warnings",
            "--extractor-args", "youtube:player_client=web",
            url
        ]
        
        result = run_subprocess(cmd, timeout=30)
        
        if result.returncode == 0 and result.stdout:
            try:
                # 解析第一行JSON
                lines = result.stdout.strip().split('\n')
                if lines:
                    return json.loads(lines[0])
            except json.JSONDecodeError:
                pass
        
        return None
    except Exception as e:
        logger.warning(f"获取视频信息失败: {e}")
        return None

def get_multiple_videos(channel_url: str, max_videos: int = 8) -> list[str]:
    """获取频道的多个最新视频URL，带重试逻辑"""
    for attempt in range(3):
        try:
            # 随机延迟
            random_delay(0.5, 2)
            
            # 使用更激进的超时设置
            cmd = [
                "yt-dlp",
                "--flat-playlist",
                "--playlist-items", f"1:{max_videos}",
                "--print", "%(webpage_url)s",
                "--no-warnings",
                "--socket-timeout", "15",  # 增加socket超时
                "--extractor-args", "youtube:player_client=web",
                channel_url
            ]
            
            logger.info(f"  正在获取频道视频列表...")
            
            result = run_subprocess(cmd, timeout=30)
            
            if result.returncode == 0:
                urls = [line.strip() for line in result.stdout.split('\n') if line.strip()]
                # 过滤掉Shorts和社区帖子
                video_urls = [url for url in urls if '/shorts/' not in url and '/community?' not in url]
                
                if video_urls:
                    logger.info(f"  从频道获取到 {len(video_urls)} 个视频")
                    return video_urls
                else:
                    logger.warning(f"  频道没有常规视频（可能是全部Shorts）")
            else:
                error_msg = result.stderr[:200] if result.stderr else 'Unknown'
                # 简化错误信息
                if "404" in error_msg:
                    error_msg = "频道不存在或已更名"
                elif "Unable to download API page" in error_msg:
                    error_msg = "YouTube API限制"
                logger.warning(f"  获取视频列表失败 (尝试 {attempt + 1}/3): {error_msg}")
                
        except subprocess.TimeoutExpired:
            logger.warning(f"  获取视频列表超时 (尝试 {attempt + 1}/3)")
        except Exception as e:
            logger.warning(f"  获取视频列表异常 (尝试 {attempt + 1}/3): {str(e)[:100]}")
        
        if attempt < 2:
            exponential_backoff(attempt, base_delay=3)
    
    return []

def verify_download(file_path: Path) -> bool:
    """验证下载的文件是否完整有效"""
    try:
        if not file_path.exists():
            return False
        
        file_size = file_path.stat().st_size
        
        # 文件必须大于 100KB（太小可能是错误文件）
        if file_size < 100 * 1024:
            logger.warning(f"文件太小 ({file_size} bytes)，可能不完整")
            return False
        
        # 检查文件扩展名
        ext = file_path.suffix.lower()
        if ext not in ['.mp4', '.webm', '.mkv', '.mp3', '.m4a']:
            logger.warning(f"未知的文件格式: {ext}")
            return False
        
        # 对于视频文件，尝试用 ffprobe 验证（如果可用）
        if ext in ['.mp4', '.webm', '.mkv']:
            try:
                # 关键修复：使用临时文件避免编码问题
                result = run_subprocess(
                    ["ffprobe", "-v", "error", "-show_entries", 
                     "format=duration", "-of", 
                     "default=noprint_wrappers=1:nokey=1", str(file_path)],
                    timeout=10
                )
                if result.returncode == 0:
                    duration = float(result.stdout.strip())
                    if duration < 5:  # 视频时长必须大于5秒
                        logger.warning(f"视频时长过短 ({duration:.1f}秒)")
                        return False
                    logger.info(f"视频验证通过，时长: {duration:.1f}秒")
                    return True
            except FileNotFoundError:
                # ffprobe 不可用，跳过视频验证
                logger.info(f"文件大小验证通过: {file_size / 1024 / 1024:.1f} MB")
                return True
            except Exception as e:
                logger.warning(f"视频验证失败: {e}")
                return False
        
        logger.info(f"文件验证通过: {file_size / 1024 / 1024:.1f} MB")
        return True
        
    except Exception as e:
        logger.error(f"验证文件时出错: {e}")
        return False

def download_video_strategy(
    url: str, 
    category: str, 
    output_dir: Path, 
    strategy: dict[str, Any],
    channel: str = ""
) -> DownloadResult:
    """使用指定策略下载视频"""
    
    # 提取视频ID
    video_id = download_history.extract_video_id(url)
    if video_id and download_history.is_downloaded(video_id):
        logger.info(f"  [SKIP] 视频已下载过: {video_id}")
        return DownloadResult(
            status=DownloadStatus.SKIPPED,
            error_msg=f"视频 {video_id} 已下载过",
            strategy_used=None
        )
    
    cat_dir = output_dir / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_template = str(cat_dir / f"{timestamp}_%(title).60s.%(ext)s")
    
    try:
        logger.info(f"使用策略: {strategy['name']} [{category}]")
        
        cmd: list[str] = [
            "yt-dlp",
            "-f", str(strategy["format"]),
            "--output", output_template,
            "--restrict-filenames",
            "--no-warnings",
            "--retries", "5",
            "--fragment-retries", "5",
            "--skip-unavailable-fragments",
            "--file-access-retries", "3",
            "--continue",  # 支持断点续传
            "--no-overwrites",
        ]
        
        # 添加提取器参数
        extractor_args = strategy.get("extractor_args")
        if extractor_args is not None:
            cmd.extend(["--extractor-args", str(extractor_args)])
        
        # 添加额外参数
        extra_args = strategy.get("extra_args", [])
        if isinstance(extra_args, list):
            cmd.extend([str(arg) for arg in extra_args])
        
        # 添加Cookie（如果配置）
        if COOKIE_FILE and Path(COOKIE_FILE).exists():
            cmd.extend(["--cookies", COOKIE_FILE])
        
        # 添加代理（如果配置）
        if USE_PROXY and PROXY_URL:
            cmd.extend(["--proxy", PROXY_URL])
        
        # 添加用户代理
        cmd.extend(["--user-agent", 
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"])
        
        # 添加URL
        cmd.append(url)
        
        # 执行下载
        result = run_subprocess(cmd, timeout=TIMEOUT_SECONDS)
        
        if result.returncode == 0:
            # 查找下载的文件
            downloaded_files = list(cat_dir.glob(f"{timestamp}_*"))
            if downloaded_files:
                file_path = downloaded_files[0]
                if verify_download(file_path):
                    # 计算文件大小
                    file_size_mb = file_path.stat().st_size / (1024 * 1024)
                    # 提取视频ID用于记录
                    video_id = download_history.extract_video_id(url) or ""
                    video_title = file_path.stem[:100]  # 从文件名提取标题
                    
                    # 保存下载记录
                    download_history.add_record(
                        video_id=video_id,
                        video_url=url,
                        category=category,
                        channel=channel,
                        title=video_title,
                        file_path=str(file_path),
                        file_size_mb=file_size_mb,
                        status="success"
                    )
                    
                    logger.success(f"[OK] 下载成功: {file_path.name}")
                    return DownloadResult(
                        status=DownloadStatus.SUCCESS,
                        file_path=str(file_path),
                        strategy_used=str(strategy['name'])
                    )
                else:
                    # 删除无效文件
                    file_path.unlink(missing_ok=True)
                    return DownloadResult(
                        status=DownloadStatus.FAILED,
                        error_msg="文件验证失败",
                        strategy_used=str(strategy['name'])
                    )
            else:
                return DownloadResult(
                    status=DownloadStatus.FAILED,
                    error_msg="无法找到下载的文件",
                    strategy_used=str(strategy['name'])
                )
        else:
            error_msg = result.stderr[:250] if result.stderr else '未知错误'
            # 过滤掉常见但无害的警告
            if "Sign in to confirm" in error_msg or "unavailable" in error_msg.lower():
                error_msg = "视频需要登录或不可用"
            return DownloadResult(
                status=DownloadStatus.FAILED,
                error_msg=error_msg,
                strategy_used=str(strategy['name'])
            )
            
    except subprocess.TimeoutExpired:
        return DownloadResult(
            status=DownloadStatus.TIMEOUT,
            error_msg=f"下载超时 ({TIMEOUT_SECONDS}秒)",
            strategy_used=str(strategy['name'])
        )
    except Exception as e:
        return DownloadResult(
            status=DownloadStatus.FAILED,
            error_msg=str(e)[:200],
            strategy_used=str(strategy['name'])
        )

def download_with_all_strategies(url: str, category: str, output_dir: Path) -> DownloadResult:
    """尝试所有策略下载视频"""
    
    # 按优先级排序策略
    sorted_strategies = sorted(DOWNLOAD_STRATEGIES, key=lambda x: int(x.get('priority', 0)))
    
    for i, strategy in enumerate(sorted_strategies):
        # 随机延迟，避免请求过快
        if i > 0:
            delay = random_delay(3, 7)
            logger.info(f"策略切换间隔: {delay:.1f}秒")
        
        result = download_video_strategy(url, category, output_dir, strategy)
        
        if result.status == DownloadStatus.SUCCESS:
            return result
        
        logger.warning(f"策略失败: {strategy['name']} - {result.error_msg}")
        
        # 指数退避
        if i < len(sorted_strategies) - 1:
            exponential_backoff(i, base_delay=5)
    
    return DownloadResult(
        status=DownloadStatus.FAILED,
        error_msg="所有策略均已失败"
    )

def download_category_with_guarantee(
    category: str, 
    channels: List[str], 
    output_dir: Path
) -> Tuple[bool, int, Optional[str]]:
    """
    下载某个类别，确保至少成功一个
    返回: (是否成功, 尝试视频数, 成功文件路径)
    """
    start_time = time.time()
    videos_tried = 0
    
    # 每个类别的最大处理时间（防止单个类别卡住）
    CATEGORY_TIMEOUT = min(GLOBAL_TIMEOUT / len(YOUTUBE_SOURCES), 300.0)  # 最多5分钟/类别
    
    # 随机打乱频道顺序，避免总是从同一个频道开始
    shuffled_channels = channels.copy()
    random.shuffle(shuffled_channels)
    
    for channel_idx, channel in enumerate(shuffled_channels):
        channel_start = time.time()
        
        # 检查全局超时
        elapsed = time.time() - start_time
        if elapsed > CATEGORY_TIMEOUT:
            logger.error(f"[TIMEOUT] 类别 {category} 超出类别超时限制 ({CATEGORY_TIMEOUT:.0f}秒)")
            return False, videos_tried, None
        
        # 提取频道名称用于日志
        channel_name = channel.split('/@')[-1].split('/')[0] if '/@' in channel else f"Channel_{channel_idx}"
        logger.info(f"[{category}] 频道 {channel_idx + 1}/{len(shuffled_channels)}: @{channel_name}")
        
        # 获取多个视频（带单个频道超时）
        try:
            import threading
            video_urls: list[str] = []
            
            def fetch_videos():
                nonlocal video_urls
                video_urls = get_multiple_videos(channel, MAX_VIDEOS_TO_CHECK)
            
            # 使用线程来限制获取视频列表的时间
            thread = threading.Thread(target=fetch_videos)
            thread.start()
            thread.join(timeout=60)  # 最多60秒获取视频列表
            
            if thread.is_alive():
                logger.warning(f"  获取视频列表超时，跳过此频道")
                continue
            
        except Exception as e:
            logger.warning(f"  获取视频列表出错: {e}")
            continue
        
        if not video_urls:
            logger.warning(f"  频道无可用视频，跳过")
            channel_elapsed = time.time() - channel_start
            logger.info(f"  频道处理耗时: {channel_elapsed:.1f}秒")
            continue
        
        # 去重检查：过滤已下载的视频
        original_count = len(video_urls)
        video_urls, skipped_ids = download_history.deduplicate_video_list(video_urls, category)
        
        if skipped_ids:
            logger.info(f"  跳过 {len(skipped_ids)} 个已下载视频")
        
        if not video_urls:
            logger.warning(f"  该频道所有视频都已下载过，切换到下一个频道")
            continue
        
        # 尝试下载每个视频
        for url_idx, url in enumerate(video_urls):
            videos_tried += 1
            
            # 再次检查超时
            elapsed = time.time() - start_time
            if elapsed > CATEGORY_TIMEOUT:
                logger.error(f"[TIMEOUT] 类别 {category} 超出类别超时限制")
                return False, videos_tried, None
            
            logger.info(f"[{category}] 视频 {url_idx + 1}/{len(video_urls)}")
            
            # 尝试下载（带超时保护）
            try:
                result = download_with_all_strategies(url, category, output_dir)
                
                if result.status == DownloadStatus.SUCCESS:
                    logger.success(f"[SUCCESS] 类别 {category} 下载成功!")
                    return True, videos_tried, result.file_path
                
                logger.warning(f"  下载失败: {result.error_msg}")
                
            except Exception as e:
                logger.error(f"  下载过程异常: {e}")
            
            # 失败后等待
            random_delay(2, 5)
        
        channel_elapsed = time.time() - channel_start
        logger.info(f"  频道处理耗时: {channel_elapsed:.1f}秒，切换到下一个...")
    
    logger.error(f"[FAIL] 类别 {category} 所有频道均失败")
    return False, videos_tried, None

# ============ 并行下载支持 ============

def download_single_category(category: str, channels: List[str], output_dir: Path) -> Tuple[bool, int, Optional[str]]:
    """
    下载单个类别（用于并行下载）
    返回: (是否成功, 尝试视频数, 成功文件路径)
    """
    logger.info("")
    logger.info(f"[START] 开始处理类别: {category}")
    logger.info("-" * 70)
    
    # 类别间随机延迟（避免同时请求）
    delay = random.uniform(1, 3)
    logger.info(f"类别启动延迟: {delay:.1f}秒")
    time.sleep(delay)
    
    category_success, videos_tried, file_path = download_category_with_guarantee(
        category, channels, output_dir
    )
    
    if category_success and file_path:
        logger.success(f"[OK] 类别 {category} 完成")
    else:
        logger.error(f"[FAIL] 类别 {category} 失败 (尝试了 {videos_tried} 个视频)")
    
    return category_success, videos_tried, file_path


# ============ 主程序 ============

def main() -> int:
    """主程序"""
    logger.info("=" * 70)
    logger.success("YouTube 每日下载器 v5.0 - 高成功率版本")
    logger.info("核心改进: 多客户端轮换 | 智能重试 | 视频验证 | 并行下载")
    logger.info("=" * 70)
    
    # 显示运行参数
    if SELECTED_CATEGORIES:
        logger.info(f"指定类别: {', '.join(SELECTED_CATEGORIES)}")
    else:
        logger.info(f"下载所有类别 ({len(YOUTUBE_SOURCES)} 个)")
    
    if MAX_WORKERS > 1:
        logger.info(f"并行下载: 启用 {MAX_WORKERS} 个线程")
    else:
        logger.info("并行下载: 禁用（单线程顺序下载）")
    
    if TEST_MODE:
        logger.info("测试模式: 启用")
    
    logger.info("-" * 70)
    
    # 检查 yt-dlp
    if not check_yt_dlp():
        return 1
    
    # 测试 YouTube 连接
    if not test_youtube_connection():
        logger.error("无法连接到 YouTube，请检查网络或代理设置")
        return 1
    
    # 创建输出目录
    output_dir = Path(OUTPUT_DIR).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"输出目录: {output_dir}")
    
    # 清理旧文件（每个类别只保留最新的 2 个文件）
    cleanup_old_files(output_dir)
    
    # 准备要处理的类别列表
    if SELECTED_CATEGORIES:
        # 使用指定的类别
        categories = [(cat, YOUTUBE_SOURCES[cat]) for cat in SELECTED_CATEGORIES if cat in YOUTUBE_SOURCES]
        if not categories:
            logger.error("指定的类别无效")
            return 1
    else:
        # 使用所有类别
        categories = list(YOUTUBE_SOURCES.items())
        random.shuffle(categories)  # 随机顺序
    
    total_categories = len(categories)
    logger.info(f"开始下载 {total_categories} 个类别...")
    
    # 统计
    success_count = 0
    failed_count = 0
    total_videos_tried = 0
    successful_files: list[tuple[str, str]] = []
    
    # 记录总开始时间
    total_start_time = time.time()
    
    # 根据工作线程数选择串行或并行下载
    if MAX_WORKERS > 1 and total_categories > 1:
        # 并行下载模式
        logger.info(f"使用并行下载模式 ({MAX_WORKERS} 线程)...")
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任务
            future_to_category = {
                executor.submit(download_single_category, cat, chans, output_dir): cat
                for cat, chans in categories
            }
            
            # 收集结果
            for future in as_completed(future_to_category):
                category = future_to_category[future]
                try:
                    category_success, videos_tried, file_path = future.result()
                    total_videos_tried += videos_tried
                    
                    if category_success and file_path:
                        success_count += 1
                        successful_files.append((category, file_path))
                    else:
                        failed_count += 1
                        
                except Exception as e:
                    logger.error(f"[ERROR] 类别 {category} 处理异常: {e}")
                    failed_count += 1
    else:
        # 串行下载模式
        logger.info("使用串行下载模式...")
        
        for idx, (category, channels) in enumerate(categories, 1):
            logger.info("")
            logger.info(f"[{idx}/{total_categories}] 处理类别: {category}")
            logger.info("-" * 70)
            
            # 类别间延迟（除了第一个）
            if idx > 1:
                delay = random_delay(8, 15)
                logger.info(f"类别间冷却: {delay:.1f}秒")
            
            category_success, videos_tried, file_path = download_category_with_guarantee(
                category, channels, output_dir
            )
            
            total_videos_tried += videos_tried
            
            if category_success and file_path:
                success_count += 1
                successful_files.append((category, file_path))
                logger.success(f"[OK] 类别 {category} 完成")
            else:
                failed_count += 1
                logger.error(f"[FAIL] 类别 {category} 失败 (尝试了 {videos_tried} 个视频)")
    
    # 计算总耗时
    total_time = time.time() - total_start_time
    
    # 总结
    logger.info("")
    logger.info("=" * 70)
    logger.success("下载完成 - 统计报告")
    logger.info("=" * 70)
    logger.info(f"总耗时: {total_time/60:.1f} 分钟")
    logger.info(f"成功类别: {success_count}/{total_categories}")
    logger.info(f"失败类别: {failed_count}")
    logger.info(f"尝试视频数: {total_videos_tried}")
    if total_categories > 0:
        logger.info(f"成功率: {success_count/total_categories*100:.1f}%")
    
    # 显示下载历史统计
    try:
        stats = download_history.get_download_stats()
        logger.info("")
        logger.info("下载历史统计:")
        logger.info(f"  历史总下载: {stats['total_downloads']} 个视频")
        logger.info(f"  最近7天: {stats['recent_downloads']} 个视频")
        if stats['category_stats']:
            logger.info("  各类别统计:")
            for cat, count in sorted(stats['category_stats'].items(), key=lambda x: x[1], reverse=True)[:5]:
                logger.info(f"    {cat}: {count} 个")
    except Exception as e:
        logger.warning(f"获取下载统计失败: {e}")
    
    if successful_files:
        logger.info("")
        logger.success("本次成功下载的文件:")
        for cat, path in successful_files:
            logger.info(f"  [{cat}] {Path(path).name}")
    
    logger.info("=" * 70)
    
    # 退出码逻辑（修复：允许部分类别失败）
    success_rate = success_count / total_categories if total_categories > 0 else 0
    
    if failed_count == 0:
        logger.success("[PERFECT] 完美！所有类别均成功下载")
        return 0
    elif success_count >= MIN_SUCCESS_CATEGORIES and success_rate >= MIN_SUCCESS_RATE:
        logger.success(f"[SUCCESS] 任务成功！成功率 {success_rate*100:.1f}% ({success_count}/{total_categories})")
        logger.info(f"        满足最低要求: 至少 {MIN_SUCCESS_CATEGORIES} 个类别，成功率 >= {MIN_SUCCESS_RATE*100:.0f}%")
        return 0
    else:
        logger.error(f"[FAIL] 任务失败！成功率 {success_rate*100:.1f}% ({success_count}/{total_categories})")
        logger.error(f"       未达到最低要求: 至少 {MIN_SUCCESS_CATEGORIES} 个类别，成功率 >= {MIN_SUCCESS_RATE*100:.0f}%")
        return 1

if __name__ == "__main__":
    exit(main())
