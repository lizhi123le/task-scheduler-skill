---
name: task-scheduler
description: |
  跨平台定时任务管理工具 v2.0（改进版），支持 Windows/macOS/Linux。轻松创建、管理、监控定时任务，无需记忆复杂的 cron 语法或 Task Scheduler 配置。支持 SQLite 持久化、任务依赖、失败重试、Windows 原生通知、结构化日志等高级功能。

  中文关键词: 定时任务、任务调度、cron替代、自动化任务、计划任务、任务管理、定时执行、任务监控、crontab、Task Scheduler、任务依赖、失败重试、Windows通知
  English Keywords: Scheduled task, Task scheduling, Cron job, Automation, Planned task
disable-model-invocation: false
---

# Task Scheduler v2.0 - 跨平台定时任务管理器

轻松管理定时任务，无需复杂的 cron 语法或 Windows Task Scheduler。

## 改进亮点 v2.0

✅ **修复 Windows 编码问题** - 完美支持中文输出  
✅ **SQLite 持久化** - 比 JSON 更可靠的数据存储  
✅ **任务依赖执行** - 自动按依赖顺序执行任务链  
✅ **失败重试机制** - 自动重试失败任务，支持自定义次数和间隔  
✅ **Windows 原生通知** - 任务完成/失败时显示系统通知  
✅ **改进的日志系统** - 结构化日志存储在 SQLite 中  
✅ **修复系统调度器** - 正确创建 Windows/macOS/Linux 系统任务  

## 快速开始

### 基础用法

```bash
# 添加每日任务
schedule add "整理下载" \
  --command "python organize.py ~/Downloads" \
  --daily 02:00

# 添加带依赖的任务
schedule add "数据同步" \
  --command "python sync.py" \
  --daily 01:00 \
  --depends-on "数据备份"

# 添加带重试的任务
schedule add "重要任务" \
  --command "python critical.py" \
  --daily 12:00 \
  --retry-count 5 \
  --retry-delay 300

# 查看所有任务
schedule list

# 查看执行日志
schedule logs
```

## 核心功能

### 1. 添加任务

**每日任务**
```bash
schedule add "任务名称" \
  --command "要执行的命令" \
  --daily HH:MM
```

**每周任务**
```bash
schedule add "任务名称" \
  --command "要执行的命令" \
  --weekly {mon|tue|wed|thu|fri|sat|sun} HH:MM
```

**每月任务**
```bash
schedule add "任务名称" \
  --command "要执行的命令" \
  --monthly DD HH:MM
```

**自定义间隔**
```bash
# 每N分钟
schedule add "高频检查" \
  --command "check.sh" \
  --interval 30m

# 每N小时
schedule add "定时同步" \
  --command "sync.sh" \
  --interval 4h

# 每N天
schedule add "数据清理" \
  --command "cleanup.sh" \
  --interval 7d
```

### 2. 任务依赖（新功能）

```bash
# 任务B在任务A成功后执行
schedule add "数据备份" \
  --command "backup.sh" \
  --daily 00:00

schedule add "数据同步" \
  --command "sync.sh" \
  --daily 01:00 \
  --depends-on "数据备份"

# 执行数据同步时会先自动执行数据备份
```

### 3. 失败重试（新功能）

```bash
# 失败时重试3次，每次间隔60秒
schedule add "网络任务" \
  --command "python download.py" \
  --daily 10:00 \
  --retry-count 3 \
  --retry-delay 60
```

重试分两层，数值始终一致：

- **实际重试**由技能自身在 `internal-run` 里执行：失败后等 `retry_delay` 秒再跑，最多 `retry_count` 次，
  每次尝试都写 `execution_logs`（`retry_attempt` 列可看第几次）。
- **同一组参数会写入 Windows 任务计划程序**的「设置 → 如果任务失败，尝试重新启动」：
  次数 = `retry_count`、间隔 = `retry_delay`（Windows 侧最小 1 分钟，不足 60 秒按 60 秒写），
  所以在任务计划程序里能看到这组配置。正常失败由技能内部重试（`internal-run` 以 0 退出），
  只有进程异常崩溃（非 0 退出）才会触发系统重启，**不会双重重试**。
- `--retry-count 0` 等于关闭重试，系统侧也会清掉失败重启策略。

> 权限提示：如果任务实体是由**更高权限**的上下文创建的，普通用户既改不动也删不掉
> （`Access is denied`），此时要在管理员 PowerShell 里执行 `schedule repair` 或
> `schedule update <任务名> --retry-count N --retry-delay S` 才能重写这组设置。

### 4. 任务管理

```bash
# 列出所有任务
schedule list

# 暂停任务
schedule pause "任务名称"

# 启用任务
schedule enable "任务名称"

# 删除任务
schedule remove "任务名称"

# 立即运行一次（测试）
schedule run-now "任务名称"
```

### 5. 修改任务（update）

已有任务不必删了重建 —— `update` 直接改任一字段，保留原 id 与执行历史，改完自动重算
`next_run` 并重建系统计划任务。

```bash
# 只改触发时间（其余字段一律不动）
schedule update "任务名称" --daily 18:30

# 换调度类型：每周 / 每月 / 每N天 / 间隔 / 登录启动
schedule update "任务名称" --weekly mon 09:00
schedule update "任务名称" --every-days 3 21:40
schedule update "任务名称" --interval 30m
schedule update "任务名称" --at-startup

# 改命令、分组、依赖、工作目录
schedule update "任务名称" \
  --command "python new_job.py" \
  --group "维护任务" \
  --depends-on "数据备份" \
  --working-dir "D:/work"

# 改重试与通知
schedule update "任务名称" \
  --retry-count 5 \
  --retry-delay 300 \
  --notify-on-fail slack \
  --slack-webhook "https://hooks.slack.com/..."

# 改环境变量（给了 --env 就整体替换；--clear-env 清空）
schedule update "任务名称" --env "API_KEY=xxx" --env "ENV=prod"
schedule update "任务名称" --clear-env

# 启用 / 暂停（等价于 enable / pause）
schedule update "任务名称" --status paused

# 重命名（其它任务的 --depends-on 引用会自动同步）
schedule update "任务名称" --name "新任务名称"

# 先看将改什么，不落盘、不动系统任务
schedule update "任务名称" --daily 04:00 --dry-run
```

字段一览：

| 字段 | 参数 | 说明 |
|------|------|------|
| 任务名 | `--name` | 重命名；其它任务的依赖引用自动同步 |
| 命令 | `--cmd` / `--command` | 要执行的命令行（原样存库，经 shell 执行） |
| 调度 | `--daily` `--weekly` `--monthly` `--hourly` `--interval` `--every-days` `--at-startup` | 七选一，给哪个就整体换成哪种调度 |
| 状态 | `--status active\|paused` | 等价于 `enable` / `pause` |
| 分组 | `--group` | 传空字符串表示清空 |
| 依赖 | `--depends-on` | 传空字符串表示清空 |
| 工作目录 | `--working-dir` | 传空字符串表示清空 |
| 环境变量 | `--env KEY=VALUE` / `--clear-env` | 给了 `--env` 就整体替换 |
| 重试 | `--retry-count` / `--retry-delay` | 次数为 0 表示不重试；同时写入任务计划程序的失败重启策略 |
| 通知 | `--notify-on-success` / `--notify-on-fail` / `--slack-webhook` / `--email` | slack 或 email |
| 其它 | `--dry-run` | 只看将改什么，不写库、不动系统任务 |

不可修改：`id` / `created_at` / `last_run` / `run_count` / `fail_count` / `next_run`
（由系统维护，`next_run` 在修改后自动重算）。

规则：

- **只改传入的字段**，未传的保持原值；字符串字段传空串表示清空（如 `--group ""`）。
- 一次只能给一种调度参数（`--daily` / `--weekly` / `--monthly` / `--hourly` / `--interval` /
  `--every-days` / `--at-startup`）。
- 任务不存在、改名撞名、命令为空、调度时间非法 → 直接报错，不落库、不动系统任务。
- `every_days` 改时间时沿用旧 `next_run` 的日期网格，**不会**把「每 3 天」的任务整体挪到今天。
- 改完会**回读系统计划任务**核对（触发时间 / 间隔 / 入口命令 / 启用状态）：对不上就自动重建一次；
  两次仍对不上则报错并以非 0 退出（数据库已更新），用管理员 PowerShell `schedule repair` 兜底。
- 暂停的任务改完仍保持暂停：系统实体是 Disabled，库里不留 `next_run`。
- 统计字段（`run_count` / `fail_count` / `last_run` / `created_at`）不开放修改；`next_run` 自动重算。

### 6. 监控与日志（改进）

```bash
# 查看所有日志（从 SQLite 数据库）
schedule logs

# 查看特定任务日志
schedule logs --task "任务名称"

# 查看最近N条日志
schedule logs --last 20

# 查看失败的任务
schedule logs --failed
```

### 7. 通知设置（增强）

```bash
# 任务失败时发送Slack通知
schedule add "重要任务" \
  --command "critical_job.sh" \
  --daily 12:00 \
  --notify-on-fail slack \
  --slack-webhook "https://hooks.slack.com/..."

# 任务完成时发送邮件
schedule add "日报生成" \
  --command "daily_report.sh" \
  --daily 18:00 \
  --notify-on-success email \
  --email "admin@example.com"

# Windows 系统还会显示原生通知气泡
```

### 8. 任务组

```bash
# 将任务分组管理
schedule add "清理日志" \
  --command "clean_logs.sh" \
  --weekly sun 00:00 \
  --group "维护任务"

# 查看组内任务
schedule list --group "维护任务"

# 暂停整个组
schedule pause --group "维护任务"
```

### 9. 环境变量

```bash
# 为任务设置环境变量
schedule add "Python脚本" \
  --command "python script.py" \
  --daily 10:00 \
  --env "API_KEY=secret123" \
  --env "ENV=production" \
  --working-dir "/path/to/project"
```

## 完整工作流示例

### 场景1: YouTube 每日下载自动化（带依赖和重试）

```bash
# 1. 创建 YouTube 下载任务（失败时重试3次）
schedule add "YouTube每日下载" \
  --command "python youtube_daily_v2.py" \
  --daily 09:00 \
  --group "视频下载" \
  --retry-count 3 \
  --retry-delay 120 \
  --notify-on-fail email \
  --email "admin@example.com"

# 2. 创建整理任务（依赖下载完成）
schedule add "整理下载视频" \
  --command "python organize.py D:/Users/lizhi/Downloads/YouTube_Daily" \
  --daily 10:00 \
  --group "视频下载" \
  --depends-on "YouTube每日下载"

# 3. 查看所有视频相关任务
schedule list --group "视频下载"
```

### 场景2: 开发工作流自动化（带依赖链）

```bash
# 1. 代码备份
schedule add "代码备份" \
  --command "bash ~/scripts/backup_code.sh" \
  --daily 02:00

# 2. 运行测试（依赖备份成功）
schedule add "运行测试" \
  --command "npm test" \
  --daily 02:30 \
  --depends-on "代码备份" \
  --working-dir "~/projects/myapp"

# 3. 生成报告（依赖测试通过）
schedule add "生成测试报告" \
  --command "python generate_report.py" \
  --daily 03:00 \
  --depends-on "运行测试" \
  --notify-on-success email \
  --email "team@company.com"
```

### 场景3: 系统维护自动化（带通知）

```bash
# 1. 每天清理系统日志
schedule add "清理系统日志" \
  --command "sudo find /var/log -name '*.log' -mtime +7 -delete" \
  --daily 03:00 \
  --group "系统维护"

# 2. 每周检查磁盘空间
schedule add "磁盘检查" \
  --command "df -h | python check_disk.py" \
  --weekly sun 08:00 \
  --notify-on-fail slack \
  --group "系统维护"

# 3. 每月系统更新
schedule add "系统更新" \
  --command "sudo apt update && sudo apt upgrade -y" \
  --monthly 1 04:00 \
  --group "系统维护" \
  --retry-count 2
```

## 数据库结构

任务数据存储在 SQLite 数据库，路径按平台取值（`TaskScheduler._get_config_dir()`）：

| 平台 | 数据库路径 |
|------|-----------|
| Windows | `%APPDATA%\task-scheduler\scheduler.db`（如 `C:\Users\<用户>\AppData\Roaming\task-scheduler\scheduler.db`） |
| Linux / macOS | `~/.config/task-scheduler/scheduler.db` |

同一目录下还有 `logs/`（部分示例脚本的日志）。注意：Windows 上不要去找 `~/.config/...`，
那里可能存在历史遗留的空库文件（无 `tasks` 表），排查时以本表为准。

```sql
-- 任务表
CREATE TABLE tasks (
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
);

-- 执行日志表
CREATE TABLE execution_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    task_name TEXT,
    executed_at TEXT,
    status TEXT,
    duration REAL,
    output TEXT,
    error TEXT,
    retry_attempt INTEGER DEFAULT 0
);
```

## 命令参考

| 命令 | 说明 | 示例 |
|------|------|------|
| `add` | 添加新任务 | `schedule add "名称" --command "cmd" --daily 02:00` |
| `list` | 列出所有任务 | `schedule list` |
| `remove` | 删除任务 | `schedule remove "名称"` |
| `pause` | 暂停任务 | `schedule pause "名称"` |
| `enable` | 启用任务 | `schedule enable "名称"` |
| `update` | 修改任务任一字段（触发时间/命令/分组/依赖/重试/通知/改名） | `schedule update "名称" --daily 18:30` |
| `run-now` | 立即执行 | `schedule run-now "名称"` |
| `logs` | 查看日志 | `schedule logs --task "名称"` |
| `export` | 导出配置 | `schedule export config.json` |
| `import` | 导入配置（`--overwrite` 覆盖同名任务，`--dry-run` 只看不写） | `schedule import config.json` |
| `repair` | 按数据库重建/校正系统任务（被误删后一键恢复） | `schedule repair --dry-run` |
| `renumber` | 序号压实为 1..N 连续（系统任务名同步改名） | `schedule renumber --dry-run` |

## 跨平台支持

### Windows
- ✅ 使用 Task Scheduler 作为后端
- ✅ 显示原生 Windows 通知
- ✅ 修复编码问题，支持中文
- ✅ 自动处理权限问题

### macOS
- ✅ 使用 launchd 作为后端
- ✅ 支持 plist 配置生成（修复时间硬编码）
- ✅ 与 macOS 系统集成

### Linux
- ✅ 使用 cron 作为后端
- ✅ 支持 crontab 管理
- ✅ 兼容所有发行版

## 故障排除

### 导入报 `TypeError: Task.__init__() missing 1 required positional argument: 'id'`（已修复）
- 原因：旧版 `import` 先 `task_data.pop('id')` 再 `Task(**task_data)`，而 `id` 在 dataclass 里是**必填**字段，
  去掉它必然 TypeError，一条任务都进不来。
- 现在的行为：
  - `id` 一律丢弃，由 `_get_next_id()` 重新发号（id 已编进系统任务名，不能照搬别的机器的号）；
  - 未知字段（如手工加的 `group_name`、`comment`）自动忽略并提示，不再整份导入失败；
  - 单条脏数据（缺 `name`、时间写成 `25:00`、`env_vars` 不是对象）只跳过该条并报错；
  - `env_vars` 允许是对象或 JSON 字符串；`running/failed` 归位为 `active`，`disabled` 归位为 `paused`；
  - 同名任务默认**跳过**（保住现有执行历史与系统任务名），加 `--overwrite` 才就地覆盖；
  - `--dry-run` 只解析并打印将导入什么，不写库、不建系统任务。
- `startup`（ONLOGON）任务在**非管理员** PowerShell 里创建会报 `ERROR: Access is denied.`，
  这是 Windows 的权限限制而非脚本问题：数据库行仍会写入，用管理员 PowerShell 跑
  `schedule repair` 即可补建系统任务。

### 任务没有执行
1. 检查任务状态：`schedule list`
2. 查看错误日志：`schedule logs --failed`
3. 手动测试命令：`schedule run-now "任务名"`
4. 检查系统调度器：
   - Windows: `schtasks /query /tn "TaskScheduler-*" /v /fo LIST`
   - Linux: `crontab -l`
   - macOS: `launchctl list | grep com.taskscheduler`
5. Windows 上"状态已启用但到点不跑"的固定排查（v2.0.1 起已自动修正）：
   - 任务最后一次结果若是 `0x800710E0`（操作员或管理员拒绝了请求），通常就是电源条件拦下的
   - 用 `Get-ScheduledTask -TaskName 'TaskScheduler-*' | Select TaskName,Settings` 确认
     `DisallowStartIfOnBatteries=False`、`StopIfGoingOnBatteries=False`、`StartWhenAvailable=True`
   - `schtasks /Create` 默认给的是 `True/True/False`（用电池不启动、切电池杀进程、错过不补跑），
     笔记本睡眠/拔电场景下就会表现为"没定期执行"；`schedule enable <名称>` 会重新修正这三项
   - 任务默认只在用户登录时运行（Interactive），要在未登录时也跑需要额外的凭据配置

### 系统任务被删掉（杀毒 / 清理软件隔离）
- 症状：`schedule list` 里任务还在，但任务计划程序里 `TaskScheduler-<id>-*` 不见了，到点不跑
- 一条命令按数据库重建：`schedule repair`
- 先看差异不落盘：`schedule repair --dry-run`（检测到不一致时退出码为 1）
- 只修某一条：`schedule repair --task "任务名"`
- 清理系统里已无数据库记录的孤儿实体：`schedule repair --prune`
- 判定规则：实体缺失 / 入口命令不符 / 触发器与库里调度不一致 → 整条重建；
  只有电源设置或启用状态不符 → 原地修正（保留历史与统计）
- 重建时用库里的 `next_run` 作网格锚点，保住"每 N 天"任务的相位，不会被挪到今天

### `update` 改了，但任务计划程序里没变化
- 症状：`schedule update ... --daily 03:30` 之后 `schedule list` 已是新时间，但任务计划程序里
  触发器还是旧的，或者实体干脆不见了
- 原因：`schtasks /Create` 返回 0 只说明命令没报错 —— 权限、杀软拦截、`/TN` 超长（>237 字符）、
  `Set-ScheduledTask` 失败等情况都可能让实体**没真正落到系统里**，旧实现照样打印“已创建”
- 现在的处理：`update` 建完会**回读系统实体**逐项核对（触发时间 / 间隔 / 入口命令 / 启用状态），
  不一致自动重建一次；两次仍不一致 → 报错 + 退出码非 0 + 提示 `schedule repair`，不再静默成功
- 手工兜底：管理员 PowerShell 执行 `schedule repair`（先看差异用 `schedule repair --dry-run`）
- 顺带修复：暂停任务不再被回填 `next_run`（重建 / `repair` 都会保持为空）

### 重试间隔在任务计划程序里看不到
- 症状：`--retry-count 3 --retry-delay 600` 执行成功，但任务计划程序 → 设置 →「如果任务失败，
  尝试重新启动」仍是空/旧值
- 原因：这组参数要写到系统任务上（`Set-ScheduledTask`）。若实体由**更高权限**的上下文创建，
  普通用户会得到 `Access is denied`（连删除都会被拒），旧实现只看数据库、不回报失败
- 现在的处理：`add` / `update` / `repair` 都会把 `retry_count`/`retry_delay` 写进
  `<RestartOnFailure>` 并回读校验（日志里显示 `RST=PT10M/3`），不一致就重建一次；
  仍失败会明确报错并以非 0 退出，同时提示去管理员 PowerShell 处理
- 手工确认：`schtasks /Query /TN "TaskScheduler-<id>-<名称>" /XML | findstr /C:"RestartOnFailure" /C:"Count" /C:"Interval"`
- 受限时：在**管理员 PowerShell** 里执行 `schedule repair`

### 序号想从 1 开始连续
- 一条命令压实：`schedule renumber`（保持现有顺序，把 id 改成 1..N）
- 先看不改：`schedule renumber --dry-run`
- `remove` 默认删除后自动压实（否则"序号 = 第几条"的直觉会失效）；想保留原号加 `--no-renumber`
- Windows 侧优先原地改名（`Register-ScheduledTask -InputObject`），StartBoundary / Repetition / 运行历史都保留；改名失败才退回按库重建
- 库侧用两阶段置换（先改负数占位再落终值），避免主键冲突时被 `INSERT OR REPLACE` 静默删行
- 改完会把 `id_counter` 对齐到 N，下一次 `add` 从 N+1 接着来

### Windows 通知不显示
- 确保 PowerShell 可用
- 检查通知权限设置

### 命令找不到
- 使用绝对路径：`/usr/bin/python` 而非 `python`
- 设置工作目录：`--working-dir`
- 设置PATH环境变量：`--env "PATH=/usr/local/bin:$PATH"`

## 最佳实践

1. **命名规范**：使用简洁、描述性的任务名称
2. **测试先行**：使用 `run-now` 测试任务后再设置定时
3. **日志监控**：定期查看日志，及时发现失败任务
4. **错误处理**：为重要任务设置重试和通知
5. **依赖管理**：合理使用依赖关系组织任务链
6. **资源管理**：避免同时运行大量任务
7. **备份配置**：定期导出配置：`schedule export`

## 升级说明（从 v1.0）

v2.0 使用 SQLite 代替 JSON 文件存储，自动迁移数据。

如需保留旧数据：
```bash
# 导出旧配置
schedule export old_config.json

# 导入到 v2.0
schedule import old_config.json
```

## 相关技能

- `file-organizer` - 文件自动整理
- `video-content-manager` - 视频管理自动化
- `video-downloader` - YouTube 视频下载
- `connect` - 自动化发送通知
- `invoice-organizer` - 财务文档自动处理
