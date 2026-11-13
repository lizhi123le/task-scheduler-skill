# task-scheduler（本机维护版）

跨平台定时任务管理器 skill（SQLite 持久化 + 任务依赖 + 失败重试 + Windows 原生通知）。

本仓为**个人维护分支**，基于上游 skill 并修复了若干缺陷，便于版本化与备份。

## 相对上游的修复

### 1. 暂停不清 `next_run`（数据层残留）

`pause_task` 原先只置 `status='paused'` 而不清 `next_run`，库里会留下"暂停中却仍有
下次执行时间"的陈旧值。显示层已用 `status == 'active'` 守卫遮住，因此 `list` 看起来正常，
但**任何直接读 DB 的工具/导出都会被误导**。

修法：置 paused 前保存原值并清空，`schtasks /DISABLE` 失败回滚时一并恢复。

### 2. `_get_next_id` 复用 ID

原 `max(id)+1` 在删除任务后会**_复用 ID**。而 Windows 侧任务实体名把 id 编进了名字
（`TaskScheduler-<id>-<name>`），一旦某次删除部分失败（DB 行已删但 `schtasks /Delete`
没成功），旧实体就会与新任务产生同 id 残留，排查时极易误判。

修法：改为**单调高水位线** —— `<config_dir>/id_counter` 记录已发号，取
`max(文件值, 现存最大 id)+1` 并原子写回（`tmp` + `os.replace`）。

### 3. 入库时间戳格式不统一

各写入点直接用 `datetime.isoformat()`，落库带微秒（`2026-09-14T22:07:01.966007`），
而 Windows 任务计划程序是秒精度本机时间，两边口径对不上。

修法：新增模块级 `_fmt_ts()` 统一为 `%Y-%m-%dT%H:%M:%S`，接入 `task.last_run` 与
`execution_logs.executed_at`。`created_at` **刻意不动**（它是 `_calculate_next_run`
的 `every_days` 网格锚点，改它会移动下次执行时间）。

> 注：`_fmt_ts` 定义在**模块级**而非类内 —— 它被 `DatabaseManager.log_execution`
> 与 `TaskScheduler._execute_task` 两处使用，放在任一类内部都会让另一处拿不到。

### 4. Windows 电源设置静默失效

`_apply_windows_task_settings` 原先 `if result.returncode == 0: return True`，
而 `returncode` 是**子进程 powershell.exe 的退出码**，不是 `Set-ScheduledTask`
cmdlet 是否成功。失败时会静默留下"任务已建但电池设置没应用"的坏任务 ——
这正是「任务已启用但到点不跑」（`0x800710E0`）的成因。

修法：PS 段末尾回读（`Get-ScheduledTask` 取 State + `Export-ScheduledTask` 取 XML），
输出 ASCII 锚点行 `VERIFY state=… DIB=… SIB=… SWA=… MIP=…`；Python 侧逐项校验
`DIB=false / SIB=false / SWA=true / MIP=IgnoreNew`，任一不符即返回 False 并告警。

### 5. 执行任务时弹出命令窗口

Windows 上 `subprocess` 为控制台子程序创建进程时，若不传
`creationflags=CREATE_NO_WINDOW`，**每次调用都会弹出一个新控制台窗口**。
技能此前只用过 `capture_output=True`，从未设过 `creationflags`。

修法：两处子进程创建加该标志 —— `schedule.py::_execute_task`、
`task_utf8_wrapper.py`。实测不影响 stdout 捕获。

### 6. 新版任务用 `pythonw.exe` 启动（无窗口）

任务动作原先指向 `python.exe`。Task Scheduler 启动时无控制台，`python.exe` 会创建并
持有一个控制台窗口。改用同目录 `pythonw.exe`（GUI 子系统）即不创建窗口。
配套两处必要加固：

- `SafePrint.print` 容忍 `sys.stdout is None`（pythonw 下 stdout 为 None，
  `print()` 会抛 `AttributeError` → 整个任务执行中途崩溃）；
- `_execute_task` 的子进程**强制用 `python.exe`** —— 因 pythonw 下 `sys.executable`
  也是 pythonw，会让子进程一并变成无窗口进程，其写往管道 stdout 的输出全部丢失
  （实测 `execution_logs.output` 由 5769 字节掉到 0，而任务仍报 SUCCESS）。

### 7. 系统计划任务“假成功”

`_setup_windows_task` 原先只看 `schtasks /Create` 的返回码就打印“任务已创建”。可返回码 0
只代表命令没报错：权限、杀软拦截、`/TN` 超长、`Set-ScheduledTask` 失败都可能让实体**没真正
落盘**，于是出现「`update` 改了调度，任务计划程序里还是旧的」这类现象。

修法：`add` / `update` 建完回读系统实体，逐项核对触发时间、间隔、入口命令与启用状态；
不一致自动重建一次。`update` 两次仍不一致就以**非 0 退出码**报错并提示 `schedule repair`，
不再静默成功。顺带修掉：暂停任务不再被回填 `next_run`（`_sync_next_run_with_windows` 只对
active 生效，重建路径也会清空）。

### 8. 重试设置没有落到任务计划程序

`retry_count` / `retry_delay` 原先只存在数据库里，`internal-run` 在进程内自己重试；任务计划程序里
「设置 → 如果任务失败，尝试重新启动」始终是空的。用户改 `--retry-delay 600`，`schedule list`
看不到、任务计划程序里更看不到，等于改了没反馈。

修法：`_apply_windows_task_settings` 把同一组参数写进系统任务的 `<RestartOnFailure>`
（`Count` = retry_count，`Interval` = retry_delay，Windows 侧最小 1 分钟）并回读校验
（日志里是 `RST=PT10M/3`）；`_windows_task_problems` 也会把“系统侧没有重启策略/数值不符”
报出来，`repair` 能原地修正。实际重试仍由技能内部执行（`internal-run` 正常失败以 0 退出），
系统级重启只作进程崩溃兜底，**不会双重重试**；`--retry-count 0` 会清掉系统侧重启策略。

注意：如果任务实体由**更高权限**上下文创建，普通进程改不动也删不掉（`Access is denied`），
需在管理员 PowerShell 里跑 `schedule repair` 重写这组设置。

### 9. 触发器重复间隔没地方设

`schtasks /Create` 没有「重复间隔」参数，技能原先只有基准调度（每天 09:00 之类），
想在时间窗里反复跑只能自己起循环。

修法：给 Task 加 `repeat_interval` / `repeat_duration`（`30m`/`2h`/`1d`），建完任务后改触发器：
用 `New-ScheduledTaskTrigger` 生成 `Repetition` 模板塞进现有触发器，一次
`Set-ScheduledTask -Trigger ... -Settings ...` 同时落重复与设置，并回读校验
（VERIFY 里是 `REP=PT30M/PT4H`，无限期是 `REP=PT2H/`）。`--repeat` 最小 1 分钟、
上限 31 天，持续时间不得小于间隔；`--repeat` 不带值（或 `--clear-repeat`）关闭；
旧库自动 `ALTER TABLE` 补两列。

### 10. `repair` 擅自写失败重启策略、把"失败"当"暂停"

`load_tasks` 曾用 `row[19] or 3` 读 `retry_count`。`0` 是 `--retry-count 0`（关闭重试）的
合法取值，却被当成"没设过"抬回默认 3，于是 `schedule repair` 认为任务计划程序里
"未设置 != 3"，把「设置 → 如果任务失败，尝试重新启动」写成 3 次（VERIFY 里 `RST=PT1M/3`）——
用户明确关掉的策略被重新打开。同一条命令还有第二处：`status = failed` 只被当成"非 active"，
`repair` 会把它的系统实体禁用、清掉 `next_run`（日志里还说成"库里是 paused"）。

修法：读库时区分 `0` 与 `NULL`（只有 NULL 才回落默认 3 / 60）；新增 `is_schedulable()`，
把 `active` / `running` / `failed` 都视为"应保持启用"，`repair` / `_setup_windows_task` /
`_sync_next_run_with_windows` 统一按它判断，只有 `paused` / `disabled` 才停用。
回归测试见 `tests/test_repair_rules.py`。

### 11. 「每 N 天」的相位错开一天

`_calculate_next_run` 原先固定按 `created_at` 的日期网格重算 `next_run`。`created_at` 带时分秒，
而 Windows 的网格锚点是 `StartBoundary`；只要 `update` / `repair` 重钉过 StartBoundary，两边
网格就错开，算出来的"下一个网格点"会紧贴上次执行 —— 实测「11-13 21:02 刚跑过，下次被算成
11-14 21:02」，`update` 又把这个错值当锚点钉回系统，越改越偏（每 3 天变成隔天跑）。

修法：给 Task 加 `grid_anchor`（每个「每 N 天」任务显式存网格起点，老库 `ALTER TABLE` 补列、
按上次执行补值）：

- `_every_days_next_run` 以 `grid_anchor` 为唯一相位来源（依次退回 last_run / next_run /
  created_at），`_calculate_next_run`、`update` 改调度都走它；
- `update` 改间隔或时刻时把锚点重置到**上次真正执行那天**，所以「每 3 天」改「每 5 天」，
  日期从上次执行往后加 5 天（不再只换时分）；只改时分则保持原日期；
- 手动 `run-now` 不动锚点，测试任务不会把排期挪走；
- `_execute_task` 执行完按 `StartBoundary + DaysInterval` 回填 `next_run`；
- `_windows_task_problems` 新增第三类差异 `realign`：日期与网格不符 → `repair` 用
  `_realign_every_days` 原地重钉 StartBoundary 并回填 `next_run`，不重建实体、保留历史。

### 12. 其它调度类型的时间换算

对齐「每周 / 每月 / 每小时 / 每 N 分钟」时发现三处：

1. **hourly / interval 只跑一次**：`schtasks /SC HOURLY|MINUTE` 用 `TimeTrigger + <Repetition>`
   实现间隔，而 `_apply_windows_task_settings` 在没有 `--repeat` 时无条件
   `$tr.Repetition = $null`，把调度本身清掉 —— 任务建好后 `NextRunTime` 为空，跑过一次就停。
   修法：把 `hourly → 1h`、`interval → schedule_value` 当作"基础重复"，始终写入并回读校验；
   `_windows_task_problems` 也按同一口径比对，`repair` 能原地补回。
2. **next_run 被算成"今天的触发时分"**：`_windows_next_run` 只认 `<DaysInterval>`，其余类型
   退化为"今天/明天在触发时分"，且该值会覆盖库里的正确值（hourly 于是显示成明天）。
   修法：直接读任务计划程序的 `NextRunTime`，各类型统一。
3. **weekly 同日被跳过**：`days_ahead <= 0` 一律 +7 天，今天就是目标星期且时间未到时
   被排到下周。修法：先按当天算，时间已过才 +7。
4. **每月任务设置落不下**：`Set-ScheduledTask -Trigger $tr -Settings $s` 对月度触发器报
   `0x80070057 参数错误`，连带电源设置失败（电池/睡眠下到点不跑）。
   修法：设置与触发器分两次调用，只有真要改重复时才回传 `-Trigger`。

探针脚本：`tests/probe_schedule_types.py`（隔离库逐类型比对 `next_run` 与系统 `NextRunTime`）。

## 目录

```
SKILL.md              技能说明与触发词
scripts/schedule.py          主实现（本仓改动集中于此）
scripts/task_launcher.py     UTF-8 启动器
scripts/task_utf8_wrapper.py 子进程 UTF-8 包装（含 CREATE_NO_WINDOW）
schedule.ps1 / .bat          全局命令包装
setup_global.ps1 / .bat      安装到全局 PATH
examples/                    示例任务与脚本
tests/test_repair_rules.py   repair 判定规则回归测试（unittest）
```

## 使用

```powershell
schedule list
schedule add "任务名" --cmd "命令" --daily 21:00
schedule update "任务名" --daily 18:30   # 改触发时间/命令/依赖…（保留 id 与历史）
schedule update "任务名" --repeat 30m --repeat-duration 4h   # 触发器重复间隔
schedule update "*" --retry-count 0                          # 通配符：批量改所有任务
schedule update "任务名" --daily 04:00 --dry-run   # 只看改什么
schedule pause|enable|remove "任务名"
schedule run-now "任务名"     # 注意：run-now 在本进程内直接执行，不经 Task Scheduler
schedule logs [任务名]
```

## 本仓已知事项

- `schedule.ps1` 的脚本路径已改为**由 `$PSScriptRoot` 推导**（原为写死的绝对路径）。
- `examples/sample_config.json` 保留上游的示例路径（`~/.config/opencode/skills/...`），
  仅作格式示例，不参与运行。
- 上游目录里有个名为 `]` 的 47 字节垃圾文件（内容 `[ Adding to User PATH ...`），
  疑为某次 `setup_global` 的 echo 重定向写偏所致。**本仓未收录**。
- **测试注意**：调用 `add_task` 会**真的注册 Windows 计划任务实体**。集成测试必须
  桩掉 `_setup_system_scheduler` / `_remove_system_scheduler` / `_apply_windows_task_settings`，
  并在模块内拦截所有 `schtasks` 调用（`pause_task` 内部直接调它，桩方法覆盖不到），
  测试后复查无 `TaskScheduler-*` 残留。
