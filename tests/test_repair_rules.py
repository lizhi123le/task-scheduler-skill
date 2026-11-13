"""`schedule repair` 判定规则的回归测试。

背景：`DatabaseManager.load_tasks` 曾用 `row[19] or 3` 读重试次数，0 是
`--retry-count 0`（关闭重试）的合法取值，却被当成"没设过"抬回默认 3。
后果是 `schedule repair` 认为任务计划程序里"未设置"对不上库里的 3，
擅自把「如果任务失败，尝试重新启动」写成 3 次 —— 用户明确关掉的策略
被系统侧重新打开。

同一条命令里还有第二个"擅自修改"：把 `status=failed`（上次执行失败）
当成用户暂停，于是 repair 会去禁用系统实体、清掉 next_run。

跑法：python -m unittest discover -s tests -v
"""
import importlib.util
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "schedule.py"
_spec = importlib.util.spec_from_file_location("schedule_under_test", SCRIPT)
schedule = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = schedule
_spec.loader.exec_module(schedule)


def _make_task(**overrides):
    kwargs = dict(
        id=7,
        name="C盘安全清理",
        command="python clean.py",
        schedule_type="every_days",
        schedule_value="3|21:06",
        status="active",
        next_run=(datetime.now() + timedelta(hours=6)).isoformat(),
        retry_count=0,
    )
    kwargs.update(overrides)
    return schedule.Task(**kwargs)


def _windows_info(task, sched, restart_count="", restart_interval=""):
    """与 `_query_windows_task` 返回结构一致、且与库完全对得上的系统侧快照。"""
    days, time_part = task.schedule_value.split("|")
    # 入口命令按被测代码自己算（`"pythonw.exe" "schedule.py" internal-run <id>`），
    # 再拆成 <Command> / <Arguments>，免得测试写死路径后误报"入口命令不符"。
    entry = sched._windows_run_cmd(task)
    m = re.match(r'^"([^"]+)"\s+(.*)$', entry)
    # 触发日期取被测代码认可的网格点，这样"相位一致"的用例是确定的
    want = sched._every_days_next_run(task)
    return {
        "command": m.group(1),
        "arguments": m.group(2),
        "start_boundary": want or f"{datetime.now():%Y-%m-%d}T{time_part}:00",
        "days_interval": days,
        "dib": "false",
        "sib": "false",
        "swa": "true",
        "mip": "IgnoreNew",
        "restart_interval": restart_interval,
        "restart_count": restart_count,
        "repeat_interval": "",
        "repeat_duration": "",
    }


def _plain_info(task, sched, repeat="", start_boundary="2026-11-14T04:00:00"):
    """任意调度类型的系统侧快照（every_days 以外不看 DaysInterval）。"""
    entry = sched._windows_run_cmd(task)
    m = re.match(r'^"([^"]+)"\s+(.*)$', entry)
    return {
        "command": m.group(1),
        "arguments": m.group(2),
        "start_boundary": start_boundary,
        "days_interval": "",
        "dib": "false",
        "sib": "false",
        "swa": "true",
        "mip": "IgnoreNew",
        "restart_interval": "",
        "restart_count": "",
        "repeat_interval": repeat,
        "repeat_duration": "",
    }


def _scheduler():
    """只借用比较逻辑，不碰数据库与系统：构造一个不落盘的 Scheduler。"""
    scheduler = object.__new__(schedule.TaskScheduler)
    scheduler.platform = schedule.Platform.WINDOWS
    return scheduler


class RetryCountRoundTripTest(unittest.TestCase):
    """库里的 0 必须原样读回，不能被默认值顶掉。"""

    def setUp(self):
        # sqlite3 的 with 只提交不关连接（schedule.py 依赖 GC 回收），
        # Windows 上回收前删不掉 db 文件，清理失败不计入测试结论。
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db_path = Path(self._tmp.name) / "scheduler.db"
        self.db = schedule.DatabaseManager(db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_zero_retry_survives_round_trip(self):
        task = _make_task(retry_count=0, retry_delay=0)
        self.assertTrue(self.db.save_task(task))

        loaded = self.db.load_tasks()[0]
        self.assertEqual(loaded.retry_count, 0)
        self.assertEqual(loaded.retry_delay, 0)

    def test_null_retry_falls_back_to_default(self):
        task = _make_task()
        self.assertTrue(self.db.save_task(task))
        with sqlite3.connect(self.db.db_path) as conn:
            conn.execute("UPDATE tasks SET retry_count = NULL, retry_delay = NULL")

        loaded = self.db.load_tasks()[0]
        self.assertEqual(loaded.retry_count, 3)
        self.assertEqual(loaded.retry_delay, 60)

    def test_grid_anchor_survives_round_trip(self):
        anchor = "2026-11-13T21:06:00"
        self.assertTrue(self.db.save_task(_make_task(grid_anchor=anchor)))
        self.assertEqual(self.db.load_tasks()[0].grid_anchor, anchor)


class GridAnchorTest(unittest.TestCase):
    """「每 N 天」的相位锚点：显式存储，改间隔时重定。"""

    def test_pick_prefers_last_run(self):
        task = _make_task(last_run="2026-11-13T21:06:00",
                          next_run="2026-11-16T21:06:00")
        self.assertEqual(schedule.TaskScheduler._pick_grid_anchor(task),
                         "2026-11-13T21:06:00")

    def test_ensure_fills_missing_anchor_for_legacy_rows(self):
        task = _make_task(last_run="2026-11-13T21:06:00", grid_anchor=None)
        self.assertTrue(_scheduler()._ensure_grid_anchor(task))
        self.assertEqual(task.grid_anchor, "2026-11-13T21:06:00")

    def test_ensure_keeps_existing_anchor(self):
        task = _make_task(last_run="2026-11-13T21:06:00",
                          grid_anchor="2026-11-10T21:06:00")
        self.assertFalse(_scheduler()._ensure_grid_anchor(task))
        self.assertEqual(task.grid_anchor, "2026-11-10T21:06:00")


class RepairRestartPolicyTest(unittest.TestCase):
    """repair 对失败重启策略的判定必须只看库里的实际值。"""

    def test_no_restart_added_when_retry_disabled(self):
        task = _make_task(retry_count=0)
        sched = _scheduler()
        rebuild, fix, _ = sched._windows_task_problems(
            task, _windows_info(task, sched), "Ready")
        self.assertEqual(rebuild, [])
        self.assertEqual(fix, [], "库 retry_count=0 时不应要求系统侧新增失败重启")

    def test_restart_diff_reported_when_db_asks_for_it(self):
        task = _make_task(retry_count=3, retry_delay=60)
        sched = _scheduler()
        _, fix, _ = sched._windows_task_problems(
            task, _windows_info(task, sched), "Ready")
        self.assertIn("失败重启次数 未设置 != 3", fix)

    def test_leftover_restart_cleared_when_retry_disabled(self):
        task = _make_task(retry_count=0)
        sched = _scheduler()
        info = _windows_info(task, sched, restart_count="3", restart_interval="PT1M")
        _, fix, _ = sched._windows_task_problems(task, info, "Ready")
        self.assertTrue(any("失败重启" in item and "关闭" in item for item in fix))


class SchedulableStatusTest(unittest.TestCase):
    """`failed` 是执行结果，不是用户暂停：repair 不能因此停用任务。"""

    def test_failed_task_keeps_running(self):
        task = _make_task(status="failed", retry_count=0)
        sched = _scheduler()
        rebuild, fix, realign = sched._windows_task_problems(
            task, _windows_info(task, sched), "Ready")
        self.assertEqual((rebuild, fix, realign), ([], [], []),
                         "上次执行失败的任务不该被 repair 当成暂停处理")

    def test_failed_task_enabled_when_system_disabled(self):
        task = _make_task(status="failed", retry_count=0)
        sched = _scheduler()
        _, fix, _ = sched._windows_task_problems(
            task, _windows_info(task, sched), "Disabled")
        self.assertTrue(any("Disabled" in item for item in fix),
                        "库里仍应执行、系统侧却 Disabled 时，repair 应把它启用回来")

    def test_paused_task_reported_as_paused(self):
        task = _make_task(status="paused", retry_count=0)
        sched = _scheduler()
        _, fix, realign = sched._windows_task_problems(
            task, _windows_info(task, sched), "Ready")
        self.assertTrue(any("应保持停用" in item for item in fix))
        self.assertEqual(realign, [], "暂停任务不参与相位校准")


class EveryDaysPhaseTest(unittest.TestCase):
    """「每 N 天」的相位必须跟着上次按点执行走，不能被算成隔天。"""

    def test_phase_follows_last_scheduled_run(self):
        now = datetime.now()
        last = (now - timedelta(days=1)).replace(hour=21, minute=6,
                                                 second=0, microsecond=0)
        task = _make_task(last_run=last.isoformat(), next_run=None)

        wanted = _scheduler()._every_days_next_run(task)
        self.assertEqual(wanted, (last + timedelta(days=3)).isoformat())
        self.assertNotEqual(
            wanted, (last + timedelta(days=1)).isoformat(),
            "上次执行后一天不是这个网格的点（这正是 11-13 → 11-14 的错）")

    def test_manual_run_does_not_shift_phase(self):
        now = datetime.now()
        manual = (now - timedelta(days=1)).replace(hour=3, minute=7,
                                                   second=0, microsecond=0)
        grid = (now + timedelta(days=1)).replace(hour=21, minute=6,
                                                 second=0, microsecond=0)
        task = _make_task(last_run=manual.isoformat(), next_run=None,
                          grid_anchor=grid.isoformat())

        wanted = _scheduler()._every_days_next_run(task)
        self.assertEqual(wanted, grid.isoformat(),
                         "手动 run-now 不该改动 grid_anchor 定下的相位")

    def test_interval_change_reanchors_from_last_run(self):
        now = datetime.now()
        last = (now - timedelta(days=1)).replace(hour=21, minute=6,
                                                 second=0, microsecond=0)
        task = _make_task(schedule_value="5|12:03",
                          last_run=last.isoformat(), next_run=None,
                          grid_anchor=last.isoformat())

        wanted = _scheduler()._every_days_next_run(task)
        expected = last.replace(hour=12, minute=3) + timedelta(days=5)
        self.assertEqual(wanted, expected.isoformat(),
                         "3 天改 5 天后，日期要从上次执行那天重新起算")

    def test_repair_realigns_when_boundary_drifted(self):
        now = datetime.now()
        last = (now - timedelta(days=1)).replace(hour=21, minute=6,
                                                 second=0, microsecond=0)
        task = _make_task(last_run=last.isoformat(), next_run=None)
        sched = _scheduler()
        info = _windows_info(task, sched)
        # 系统被旧版钉到了"上次执行 + 1 天"
        info["start_boundary"] = (last + timedelta(days=1)).isoformat()

        rebuild, fix, realign = sched._windows_task_problems(task, info, "Ready")
        self.assertEqual((rebuild, fix), ([], []))
        self.assertTrue(any("触发日期" in item for item in realign))

    def test_repair_accepts_aligned_boundary(self):
        now = datetime.now()
        last = (now - timedelta(days=1)).replace(hour=21, minute=6,
                                                 second=0, microsecond=0)
        task = _make_task(last_run=last.isoformat(), next_run=None)
        sched = _scheduler()
        _, _, realign = sched._windows_task_problems(
            task, _windows_info(task, sched), "Ready")
        self.assertEqual(realign, [])


class BaseScheduleRepeatTest(unittest.TestCase):
    """hourly / interval 的「重复」就是调度本身，不能被当成多余的 --repeat。"""

    def test_interval_repeat_missing_is_reported(self):
        task = _make_task(schedule_type="interval", schedule_value="30m")
        sched = _scheduler()
        _, fix, _ = sched._windows_task_problems(task, _plain_info(task, sched), "Ready")
        self.assertIn("重复间隔 未设置 != PT30M", fix)

    def test_interval_repeat_present_is_clean(self):
        task = _make_task(schedule_type="interval", schedule_value="30m")
        sched = _scheduler()
        problems = sched._windows_task_problems(
            task, _plain_info(task, sched, repeat="PT30M"), "Ready")
        self.assertEqual(problems, ([], [], []))

    def test_hourly_repeat_is_clean(self):
        task = _make_task(schedule_type="hourly", schedule_value="0")
        sched = _scheduler()
        problems = sched._windows_task_problems(
            task, _plain_info(task, sched, repeat="PT1H"), "Ready")
        self.assertEqual(problems, ([], [], []))

    def test_leftover_repeat_on_daily_is_cleared(self):
        task = _make_task(schedule_type="daily", schedule_value="04:00")
        sched = _scheduler()
        _, fix, _ = sched._windows_task_problems(
            task, _plain_info(task, sched, repeat="PT30M"), "Ready")
        self.assertTrue(any("重复间隔" in item and "关闭" in item for item in fix))


class WeeklySameDayTest(unittest.TestCase):
    def test_weekly_today_later_is_scheduled_today(self):
        now = datetime.now()
        later = now + timedelta(hours=1)
        if later.date() != now.date():
            self.skipTest("临近午夜，跳过同日用例")
        weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
        task = _make_task(schedule_type="weekly",
                          schedule_value=f"{weekday} {later:%H:%M}")
        got = _scheduler()._calculate_next_run(task)
        self.assertTrue(got.startswith(f"{now:%Y-%m-%d}"),
                        f"今天就是目标星期且时间未到，应安排在今天，实际 {got}")


if __name__ == "__main__":
    unittest.main()
