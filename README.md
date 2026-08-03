# LOG 数据扫描工具 · 全套功能说明

> 项目目录：`c:/Users/SONG/Desktop/jiajnkong`
> 依赖：**仅 Python 标准库**（`tkinter` / `sqlite3` / `threading` / `queue` / `csv` …），零第三方包。
> 核心数据：`scan_state.db`（SQLite 单文件，**当前的库结构 = `processed` 已处理文件表 + `metrics` 指标宽表**；配置不在库里，而在 `scan_state.ini`）。

---

## 一、文件用途速查表

### 1. 工具 / 代码文件

| 文件 | 作用 | 运行方式 |
|------|------|----------|
| `scanner_gui.py` | **主程序**：增量扫描器 GUI。扫描路径/类型过滤、开始/暂停/停止持续监控、状态持久化（写入 `scan_state.ini` + `scan_state.db`）、Z 盘设备备份盘滚动策略、网络盘健壮性、控制栏含「查看数据库」入口。 | `python scanner_gui.py` |
| `analysis.py` | **指标计算唯一真相来源**：所有「文件级分析指标」的计算与建表/落库/查询逻辑都集中在此（在线扫描算指标、离线重算、查看器列定义共用同一份）。核心是一个 `METRICS` 注册表，新增指标只改这里、无需动其它文件。 | 被其它程序 `import`，不直接运行 |
| `db_view.py` | **数据库查看器（只读）**：表格展示 + 设备/月份多选 + 路径通配符筛选 + 导出 CSV（复用 `analysis.column_defs` 决定列）。 | `python db_view.py` 或主程序内嵌 |
| `db_maintain.py` | **数据库维护**：全量备份 + 给 `processed` 补 `device`/`ym` 列 + 把超过 1 年的旧记录归档到独立库/CSV 并从主库删除 + 确保 `metrics` 宽表存在、开 WAL。 | `python db_maintain.py` |
| `export_csv.py` | **按月导出 CSV**：按数据月份（`ym`）逐个生成 `FRST LOG-<ym>.csv`；只读连接并发安全；默认只重写「当前月 + 仍在带扫的上月」，历史月原样保留；原子写。 | `python export_csv.py --outdir <目录> [--all\|--ym ...]` |
| `README.md` | 本说明文档 | — |

### 2. 数据 / 配置 / 日志文件

| 文件 | 作用 | 是否可删 |
|------|------|----------|
| `scan_state.db` | **主状态库**（SQLite，WAL 模式）：`processed` 已处理文件记录表 + `metrics` 指标宽表。库体即全部扫描与指标结果。**核心，勿删。** | 核心 |
| `scan_state.db-shm` / `scan_state.db-wal` | SQLite WAL（预写日志）的**工作文件**：`-wal` 暂存未 checkpoint 的写、`-shm` 是共享内存索引。程序关闭/checkpoint 后 `-wal` 会被并入主库而变空或删除。**运行时勿手动删，随主库一并保留即可。** | 附属（勿单独删） |
| `scan_state.db.bak` | 主库的**手动备份副本**（整库拷贝）。与主库同时存在，用于误删/误改后回退。 | 备份，建议保留 |
| `scan_state_backup_20260729.db` | 由 `db_maintain.py` 在维护开始时做的**全量自动备份**（文件名带运行日期 `YYYYMMDD`，所有数据原样保留，作为安全网）。 | 备份，建议保留 |
| `scan_state.ini` | **主配置文件**：所有运行设置（扫描根路径、起始月、轮询间隔、跨月兜底窗口、是否扫后算指标、设备清单、产线、CSV 自动导出开关等）。程序每次保存配置会整体重写本文件。逐设备上月确认标志为内存态、不写此文件。 | 核心（误删会回到默认配置） |
| `csv_export_cols.json` | 手动导出 CSV 时**用户勾选的列清单**（JSON 字符串数组，顺序即表头顺序）。不存在则默认导出全部列。 | 配置，可重建 |
| `csv_export_state.json` | CSV 增量导出的**状态文件**：记录上次成功导出时间 `last_run`，下次只重写自那之后变动的月份。程序整体覆盖，勿手动加字段。 | 状态，可重建 |
| `csv_export/` | `export_csv.py` 默认的输出目录（按 `DEFAULT_OUTDIR`）。当前为空（月度 CSV 此前导出到项目根目录）。 | 产物目录 |
| `FRST LOG-202607.csv` | 由 `export_csv.py` 产出的**某月 CSV 样本**（数据月份 202607），列含 设备/月份/路径/大小/处理时间/各指标。 | 产物，可随时重新导出 |
| `scan_state_recalc_log.txt` | 「重新计算指标 / 补算缺失指标」操作的**运行日志**：每次重算的时间、范围（产线/月份/设备）、处理文件数、是否被取消。排查「为什么没数据」时看这里。 | 日志 |
| `csv_export.log` | `export_csv.py` 的**运行日志**：每次导出的时间、模式（全量/增量）、导出了哪几个月、是否无变动。 | 日志 |
| `__pycache__/` | Python 编译缓存（`.pyc`），非源码。 | 可随时删，不影响运行 |

> 说明：早期监控工具残留（`monitor_state.json` / `monitor.lock` / `monitor.log`）已清理；旧 schema 的 `meta` 配置表、`results` 统计表、`met_*` 指标分表**均已迁移合并**，不再存在（详见第三章「数据库状态与详细情况」）。

---

## 二、文件之间的数据与调用关系

```
                scanner_gui.py (主程序, 后台线程扫描)
                  │  扫描发现新文件 → 写 processed 表
                  │  若 compute_after_scan=true → 调 analysis.upsert_batch 写 metrics
                  ├──「查看数据库」→ 内嵌 db_view.py (只读连 scan_state.db)
                  ├──「重新计算指标」→ analysis.recalc_by_months / recalc_missing (写 metrics)
                  └── CSV 自动导出 → 调 export_csv 逻辑 (只读连 scan_state.db)
                        │
   analysis.py ──被 scanner_gui / db_view / export_csv / db_maintain 共同 import
   （指标计算 + metrics 建表/落库/列定义 的唯一来源）
                        │
   db_maintain.py ──备份 scan_state.db、补列、归档旧数据、确保 metrics 表与 WAL
```

要点：
- **`analysis.py` 是中枢**：所有指标口径（第 2 列计数、品名/原品名、LOT/Block/日期、总枚数、内外层各报警代码计数、剥离值方差等，均为 Excel Power Query 口径的迁移）都定义在这里的 `METRICS` 注册表；扫描落库、离线重算、查看器列、CSV 表头全部从这里取，保证三处口径一致。
- **配置走 `scan_state.ini`**，不是数据库；只有「已扫描的文件」和「算出的指标」才进 `scan_state.db`。

---

## 三、数据库状态与详细情况

### 1. 当前库结构（两张表）

主库 `scan_state.db`（SQLite，WAL 模式）现含以下两张业务表（外加 SQLite 内部表）：

#### 表 `processed` —— 已处理文件记录（扫描时写入）
| 列 | 类型 | 含义 |
|----|------|------|
| `id` | INTEGER PK | 自增主键（rowid 结构） |
| `path` | TEXT UNIQUE | 文件完整路径（去重主键） |
| `ts` | TEXT | 处理时间（`YYYY-MM-DD HH:MM:SS`） |
| `size` | INTEGER | 文件大小（字节） |
| `mtime` | REAL | 文件修改时间（Unix 时间戳，用于诊断） |
| `device` | TEXT | 设备号（从路径解析，如 `D32`/`E03`/`E31`/`E32`；非设备路径为 NULL） |
| `ym` | TEXT | 数据月份 `YYYYMM`（从路径 `BACKUP_YYYYMM` 或 `CPU1-YYYYMMDD` 解析；解析不到为 NULL） |

> 索引：`idx_processed_ym`(ym)、`idx_processed_device`(device)，供查看器按月份/设备筛选与排序。

#### 表 `metrics` —— 指标宽表（算指标时写入）
| 列 | 类型 | 含义（口径由 `analysis.METRICS` 定义） |
|----|------|------|
| `id` | INTEGER PK | 自增主键 |
| `path` | TEXT UNIQUE | 文件完整路径（与 `processed` 一对一） |
| `run_at` | TEXT | 本次计算时间 |
| `hinmei` | TEXT | **原品名**：E 列（第 5 列）第 2 行（首条数据行）原文 |
| `hinmei_fmt` | TEXT | **品名**：原品名格式化 `s[0:2]-s[3:5]-s[10:13]-s[-3:]` |
| `lot` | TEXT | **LOT**：文件名 `[21:31]`，如 `253UNJR000` |
| `block` | TEXT | **Block**：文件名去扩展名后末段（最后一个 `-` 之后），如 `005` |
| `fdate` | TEXT | **日期**：文件名 `[5:13]` → `YYYY-MM-DD` |
| `total_max` | INTEGER | **总枚数**：最后一个有效行的 L 值（第 11 列） |
| `outer_total` | INTEGER | **外层枚数**：K（第 9 列）= 1 的行数 |
| `inner_570/586/587/588` | INTEGER | **内层{code}**：K = 20 且 L = code 的行数 |
| `outer_570/586/587/588` | INTEGER | **外层{code}**：K = 21 且 L = code 的行数 |
| `inner_sum` / `outer_sum` | INTEGER | **内层/外层 MISS 总回数**：上述各代码计数之和 |
| `inner_odd` | INTEGER | **内层 MISS 奇数**：K = 20、L ∈ 代码清单且 L 为奇数的行数 |
| `std_inner` / `std_outer` | REAL | **内层/外层剥离值方差**：K=0 的 Z（第 12 列）/ K=1 的 AB（第 26 列）**样本标准差**（n-1，≥2 个值才算），round 2 |

> **口径说明（自 Excel Power Query 迁移）**：以上除 `col2_count` 外均为 PQ 口径的忠实复刻。
> 所有统计先做「有效数据」前置过滤：**K、L 两列均可转为数字的行**才参与计算。
> 报警代码清单在 `analysis.MISS_CODES`（当前 `[570, 586, 587, 588]`），改此常量即自动增减内层/外层列。
> 注：`std_*` 沿用「方差」叫法，实际计算与 PQ 的 `List.StandardDeviation` 一致，为**标准差**。

> `metrics` 是**合并宽表**：旧版 `results` 表 + `met_*` 多张分表已一次性合并进此表（见下方「历史演进」），`path`/`run_at` 只存一份，库体更小、零冗余迁移。
> 在 `METRICS` 注册表新增一项指标 → 下次 `analysis.ensure_tables` 会自动 `ALTER TABLE` 补列，无需手动迁移。

### 2. 两张表的关系（为什么有时「有记录却没数据」）

`processed` 与 `metrics` 是**分开写入**的，通过 `path` 左连接（查看器/导出用 `LEFT JOIN metrics`）：

- **扫描到文件 → 立即写 `processed`**（你有设备/月份/路径/大小/处理时间）。
- **指标行只有「算过指标」才建**：
  1. 在线：若 `scan_state.ini` 中 `compute_after_scan = true`，本轮扫描末会把新增文件批量算指标并写 `metrics`；若为 `false`，则只记 `processed`、跳过指标（当前 ini 默认 `false`）。
  2. 离线：主程序「重新计算指标 / 补算缺失指标」按钮，或 `analysis.recalc_*` 触发 `compute_all` → 写 `metrics`。

因此「数据库里没有数据」通常准确含义是：**该文件在 `processed` 有记录，但在 `metrics` 无对应行（指标列全部 NULL，查看器 `COALESCE(...,'')` 显示成空白）**。常见原因：
- 当初 `compute_after_scan = false`，只记了文件没算指标 → 用「补算缺失指标」（专门找 `metrics` 缺行的文件）即可补齐。
- 该文件在 `compute_all` 时抛异常被跳过（如网络盘 `Z:/` 读取抖动）→ 主程序日志会打 `指标计算失败 <文件名>: <异常>`，看那条信息定位根因。

### 3. 当前数据规模（动态值，仅供参考）

- 设备：`D32` / `E03` / `E31` / `E32` 四台；数据月份跨度约 `202504` ~ `202607`。
- 最近一次全量重算（2026-07-29）处理 **约 1522 个文件**，覆盖上述全部设备与月份（详见 `scan_state_recalc_log.txt`）。
- 运行 `db_maintain.py` 会把超过 1 年（`KEEP_MONTHS=12`）的旧记录归档并删除，主库只留最近 12 个月（旧数据进 `scan_state_archive_<日期>.db`/`.csv`，不丢）。

### 4. 并发与一致性

- 主库开 **WAL**：扫描器（写）、查看器/导出的只读连接可并发，互不阻塞。
- 写库统一走 `db_lock`（线程锁），`upsert`/`upsert_batch`/`recalc_*` 都在锁内批量提交（每 1000 文件一批），减少事务开销。
- 指标计算（`compute_all`）对 `Z:/` 网络盘文件以 `cp932`（日文 Shift-JIS）解码为主、失败时回退 `utf-8`、再回退忽略坏字节，避免日文编码导致整文件解析失败。

### 5. 数据库历史演进（已自动化，无需手动处理）

- 旧 `meta` 配置表 → 迁移进 `scan_state.ini` 后 DROP。
- 旧 `scan_state.json` → 配置迁移进 `scan_state.ini` 后删除。
- 旧 `results` + `met_*` 多表 → 合并进 `metrics` 宽表后 DROP。
- 旧 schema（`path` 为主键）→ 迁移为 `INTEGER rowid` 主键（带自动备份 `scan_state.db.rowid_migrate_bak`，用 `PRAGMA user_version` 标记，仅跑一次）。
- 以上迁移在首次启动 / 跑 `db_maintain.py` 时**自动幂等完成**。

---

## 四、功能总览

| 模块 | 文件 | 说明 |
|------|------|------|
| 增量扫描器（主程序） | `scanner_gui.py` | GUI 扫描、增量去重、持续监控、状态持久化 |
| 指标计算中枢 | `analysis.py` | 指标定义/计算/建表/落库/列定义，被三方共用 |
| 数据库维护 | `db_maintain.py` | 全量备份 + 补列 + 归档 1 年前数据 + 确保 metrics/WAL |
| 数据库查看器 | `db_view.py` | 只读表格查看 + 多选/通配符筛选 + 导出 CSV |
| 月度 CSV 导出 | `export_csv.py` | 按月生成 `FRST LOG-<ym>.csv`，只更新变动月 |
| 说明文档 | `README.md` | 本文件 |

---

## 五、增量扫描器（主功能）

### 1. 核心能力
- **选择扫描路径**：文件夹浏览；文件类型过滤（如 `*.csv`，留空=全部；支持 `*.ext` / `.ext` / `ext` 三种写法）。
- **开始 / 暂停 / 停止**：点「开始」进入持续监控，按设定「轮询间隔」（默认 20 秒）自动扫描一轮，状态栏显示距下次扫描的倒计时；点「停止」才退出本轮。暂停可恢复、冻结倒计时。
- **实时显示**：当前处理路径、各文件夹本轮新增/累计文件数（左右分隔面板）、事件日志、状态栏（本轮候选/已处理/累计/上次扫描时间）、进度条。
- **状态持久化**：已处理路径存 `scan_state.db`；配置存 `scan_state.ini`（旧版 `scan_state.json` 首次启动自动迁移并删除）；下次启动自动加载，继续增量。
- **查看数据库入口**：控制按钮区「查看数据库」内嵌打开 `db_view.py` 只读查看器。

### 2. 增量判定规则（文件名去重，最稳）
- 去重只看「文件名是否见过」，没见过即候选（新增）。
- 迟到文件下一轮自然被当作新增抓到，不会因 mtime 旧而漏。
- mtime / size 不参与判定，扫到没见过的新文件名直接处理（不做写完判定）。

### 3. 设备备份盘策略（Z 盘：`{设备}\CPU1\BACKUP_YYYYMM`）
- **起始月份**：UI「起始月份」设定首次回填起点（默认 2025-01）；首轮从起始月一路回填到当前月。
- **历史回填完成后**：之后每轮仅轮询【当前月】目录（更快、状态有界）。
- **上月确认机制（逐设备）**：每台设备独立判断。仅在月初窗口（`prev_scan_hours`，默认本月起 6 小时）内对「本月目录尚未出现」的设备顺带扫上月兜底；本月目录一旦出现，当轮带扫【上月】作「最终确认」后仅扫本月；窗口外仍未出现的设备自然停扫上月（本月无生产，上月也不会有新文件）。确认标志为内存态不落盘；窗口内重启仅多扫一轮上月目录、当轮即全部重新确认，无漏扫（去重靠 processed_set + path UNIQUE）。
- **自动重发现**：每轮重算目标目录，运行中新增的设备目录、跨月切换的新月份目录都自动纳入，无需重启。
- **通用回退**：非设备结构路径（普通文件夹/单个 BACKUP 目录）自动回退为通用递归全量扫描。

### 4. 网络盘健壮性
- 用 `os.scandir` 递归（性能优于 `os.walk`），顶层目录失败会重试（应对网络盘未就绪）并上报错误，而非静默 0 候选。
- 针对 SMB 网络盘 `DirEntry.is_dir` 不抛异常却返回 False 的坑：再用 `os.path.isdir` 独立 stat 二次确认，避免整棵子树被静默跳过。
- 枚举到 0 文件时给出根目录级诊断（exists / isdir / 顶层条目 / 是否网络盘 stale 映射 / 是否后缀不匹配），并建议改用 UNC 路径。

### 5. 处理逻辑（可替换）
- `process_file(path)` 为处理占位：默认仅读取文件大小确认可读。用户可在此替换为自己的逻辑。失败会 raise 并被捕获，下次仍是候选。

### 6. 线程模型
- 扫描在后台 `daemon` 线程进行，UI 通过 `queue` 安全更新不卡界面；`pause_event` / `stop_event` 控制暂停与停止；状态在停止/完成/暂停时落盘。

---

## 六、指标重算（主 UI「重新计算指标」/「补算缺失指标」按钮）

- 重算入口：主程序两按钮（可限定月份/设备局部重算，或全量重算）。所有指标计算逻辑集中在 `analysis.py` 的 `METRICS` 注册表，新增指标只改该表、无需另写脚本。
- **「重新计算指标」** → `analysis.recalc_by_months`：对指定范围（月份/设备）内 `processed` 的**全部文件**重算并覆盖写 `metrics`（幂等）。
- **「补算缺失指标」** → `analysis.recalc_missing`：只找「`processed` 有记录但 `metrics` 无对应行」或「指标列为 NULL」的文件补齐，避免全量重算耗时。常用于当初 `compute_after_scan=false` 扫入、没算指标的文件（见第三章第 2 节）。
- 口径：把每个文件当 CSV 表，先按「K、L 均为数字」过滤出有效行，再统计总枚数（末行 L 值）、外层枚数（K=1 行数）、内层/外层各报警代码计数、MISS 总回数/奇数、剥离值标准差，以及文件名解析出的 LOT/Block/日期、品名与原品名（详见第三章 `metrics` 表）。原「第二列数据个数」已删除。
- 每次操作都会追加记录到 `scan_state_recalc_log.txt`（时间、范围、处理数、是否取消）。
- CSV 解码：源日志为日文编码，`_decode_text` 以 `cp932` 为主、`utf-8` 兜底、再忽略坏字节，保证网络盘/混排文件可解析。

---

## 七、数据库维护（`db_maintain.py`）

按路径里的 `BACKUP_YYYYMM`（数据月份）判断年龄，主库只保留最近 `KEEP_MONTHS`（默认 12）个月：
- **[1/5] 全量备份**：复制为 `scan_state_backup_<YYYYMMDD>.db`（当前为 `scan_state_backup_20260729.db`），作为安全网。
- **[2/5] 补列/回填**：给 `processed` 补 `device`/`ym` 两列（从路径解析），便于按月份/设备筛选与裁剪。
- **[3/5] 分离旧记录**：`数据月份 < (当前月 - KEEP_MONTHS)` 视为超 1 年；`ym` 解析不到的记录保守保留，不误删。
- **[4/5] 归档并删除**：旧记录写入 `scan_state_archive_<YYYYMMDD>.db` + `.csv`（可读文本），并从主库 `processed`/`metrics` 删除。
- **[5/5] 确保 metrics 宽表 + WAL**：建表/迁移阶段保证存在，主库开 WAL。
- `KEEP_MONTHS = 12` 控制保留窗口；改小（如 `11`）可严格保留 12 个月，重跑即可（旧数据在归档库不丢）。

---

## 八、数据库查看器（`db_view.py`）与 CSV 导出（`export_csv.py`）

### 1. 查看器（只读）
- **只读连接** `scan_state.db`（`mode=ro`，可与扫描器 GUI 并发查看、不改动数据；可与主 UI 同进程内嵌打开）。
- **表格列**：序号 / 设备 / 月份 / 路径 / 大小 / 处理时间 + `analysis.METRICS` 的全部指标列（第二列数据个数 / 原品名 / 品名 / LOT / Block / 日期 / 总枚数 / 外层枚数 / 内层·外层各报警代码 / MISS 总回数·奇数 / 剥离值方差）。列由 `analysis.column_defs()` 动态生成，`processed` 左联 `metrics`，指标列缺则显示空白。
- **筛选（实时生效，支持通配符 + 多选）**：
  - 设备 / 月份：多选列表框（Ctrl/Shift 多选），带「全选 / 清空」；`(空)` 代表字段为 NULL；不选 = 全部。
  - 路径：通配符 `* ?`（自动转 SQL `% _`），多条件用 `| , ; 空格` 分隔（OR 关系），如 `D32*|E03*`。
  - 状态：全部 / ok / simulated / 无结果；第二列个数范围（≥/≤）；文件大小范围（≥/≤，字节）。
- **导出 CSV**：「导出 CSV」按钮导出当前筛选结果，`utf-8-sig` 编码（Excel 中文不乱码）。

### 2. 月度 CSV 导出
- 按数据月份 `ym` 逐个生成 `FRST LOG-<ym>.csv`；列定义与查看器一致（默认全列，受 `csv_export_cols.json` 控制）。
- 只读连接并发安全；**只更新「当前月 + 仍在带扫的上月」**，历史月原样保留（省 IO）。库里已无数据的月份自动删除对应 CSV。
- 原子写（先 `.tmp` 再 `os.replace`），读取方永远读到完整文件。
- 用法示例：
  ```bash
  python export_csv.py --outdir "D:\CSV\scan_data"            # 增量：只更新变动月
  python export_csv.py --outdir "D:\CSV\scan_data" --all      # 全量导出所有月份
  python export_csv.py --outdir "D:\CSV\scan_data" --ym 202607          # 只导某月
  python export_csv.py --outdir "D:\CSV\scan_data" --ym 202601..202607   # 导月份区间
  ```

### 3. 手动导出（主界面「手动导出」按钮）
- 与「自动导出（按月窗口增量）」不同，**手动导出按你当场选择的维度精确导出**：弹窗里可勾选 **产线（E/C/D/A，联动设备）**、**设备号（多选，留空=全部）**、**月份（多选，留空=全部月份）**，以及 **导出列（可复选，留空=全部列）**，并指定 **专用导出目录**（与自动导出目录相互独立）。
- 底层同样调用 `export_csv.py` 子进程（只读 DB）：选中的设备经 `analysis.classify_line` 从产线映射得到确切设备号后，按 `--device` 过滤；选中的各月份逐个按 `--ym <月>` 导出（`FRST LOG-<月>.csv`），未选月份则按 `--all` 全量导出；列通过 `--cols` 传入。
- 配置持久化：导出目录存 `scan_state.ini` 的 `csv_manual_outdir`，列选择存 `csv_manual_cols`，下次打开弹窗自动回填。
- 导出在后台线程进行（UI 不卡），状态显示在弹窗底部；失败/完成均有提示。

---

## 九、使用方式

```bash
# 主扫描器（GUI，需桌面环境）
python scanner_gui.py

# 维护：备份 + 补列 + 裁剪到最近一年（先按需改 KEEP_MONTHS）
python db_maintain.py

# 查看器（独立窗口）
python db_view.py

# 月度 CSV 导出（只读，按需指定 outdir / --all / --ym）
python export_csv.py --outdir csv_export
```

> 扫描器内点「查看数据库」可直接弹出查看器（同一进程、只读）；「重新计算指标 / 补算缺失指标」在控制栏。

---

## 十、配置文件字段说明（逐行）

> 以下文件会被程序**整体重写**（保存配置 / 每次导出都会覆盖），不便在文件内写注释。本节为权威文档。

### 1. `scan_state.ini`（主配置，程序运行核心设置）
> 虽是 `.ini` 后缀，实际按 `键 = 值` 自定义格式解析（值用 JSON 还原类型，字符串带引号）。

```ini
[config]                          # 分组标识，所有设置都归在 config 段下
last_scan_time = 1785076366.2275414   # 上次扫描结束的 Unix 时间戳（浮点秒），用于显示“最后扫描时间”
scan_root = "Z:/"                 # 扫描根路径（此处扫 Z 盘）
start_year = 2021                 # 历史回填起始年（首次扫描从该月开始补数据）
start_month = 8                   # 历史回填起始月
poll_interval = 20                # 轮询间隔（秒）：每轮扫描之间等待 20 秒
prev_scan_hours = 6                 # 月初上月兜底窗口（小时）：设备本月目录未出现时仅本月起前 N 小时顺带扫上月（默认 6，UI「跨月兜底」可设置）
compute_after_scan = false        # 扫描后是否立即计算指标（true=算并写 metrics，false=只记 processed 跳过指标）
device_list = ["D32", "E03", "E31", "E32"]  # 设备清单（手动指定或点「从扫描路径嗅探」填充；为空则不扫描）
scan_lines = ["E", "C", "D", "A"] # 勾选要扫的产线列表（多选；空=全部）
csv_auto_outdir = "C:/Users/SONG/Desktop/jiajnkong"   # 月度CSV自动导出目录
csv_auto_cols = ["设备", "月份", ...]  # 自动导出时勾选要导出的列（顺序即CSV表头顺序）
csv_auto_enabled = false          # 是否启用月度CSV自动导出（true=启用并后台定时导出）
csv_auto_interval_min = 1         # 自动导出间隔（分钟）：每 1 分钟检查一次变动月份
csv_manual_outdir = "C:/Users/SONG/Desktop/jiajnkong"   # 手动导出专用目录（「手动导出」弹窗设置，独立于自动导出目录）
csv_manual_cols = ["设备", "月份", ...]  # 手动导出时勾选要导出的列（顺序即CSV表头顺序）
```

### 2. `csv_export_cols.json`（手动导出 CSV 的列选择）
```json
[ "设备", "月份", "路径", "大小", "处理时间",
  "原品名", "品名", "LOT", "Block", "日期",
  "总枚数", "外层枚数",
  "内层570", "内层586", "内层587", "内层588",
  "外层570", "外层586", "外层587", "外层588",
  "内层MISS总回数", "外层MISS总回数", "内层MISS奇数",
  "内层剥离值方差", "外层剥离值方差" ]
```
- 手动导出（`export_csv.py` 或查看器「导出 CSV」）时**用户勾选的列清单**，JSON 字符串数组。
- 文件存在 → 按这里选中的列导出；**不存在 → 默认导出全部列**。
- 数组顺序 = 导出 CSV 的表头顺序。当前为“全列”状态。

### 3. `csv_export_state.json`（CSV 增量导出状态）
```json
{"last_run": "2026-07-26 22:32:04"}
```
- `last_run`：上次成功导出的时间（本地时间字符串）。
- 作用：下次导出时对比各月数据的 `ts/run_at`，**只重写自上回导出以来变动的月份**，历史月文件原样保留（省 IO）；首次无此文件则全量导出。
- 注意：每次导出都会被 `export_csv.py` 用 `json.dump` **整体覆盖**，不要手动往里加字段。

---

## 十一、待确认的假设

1. ~~「第二列」口径~~ → **已删除**：该指标非 PQ 口径，已从 `METRICS` 与库中移除。
2. ~~总枚数 / 品名 / 576·586·587 个数的旧口径~~ → **已按 Excel Power Query 口径重写**，不再是假设（见第三章 `metrics` 表）。旧列 `cnt_576/586/587` 已从库中删除。
3. **Block 口径**：PQ 原式取首个 `_` 前 3 字符，但真实文件名（如 `CPU1-20250703-095722-253UNJR000-005.csv`）无 `_`，该式在 PQ 中会报错；现按文件名真实结构取**末段**（`005`）。
4. **`__csv` 非量产分支**：PQ 里该类文件已被前置排除、分支实际走不到；本项目扫描器只收 `*.csv` 同样收不到，代码中保留该分支仅作兜底。
5. **保留窗口边界**：`KEEP_MONTHS=12` → 偏保守保留约 13 个月。严格 12 个月改 `11` 重跑。
6. **路径通配分隔符**：当前用 `| , ; 空格` 作多条件分隔。如需正则可改。
7. **`process_file` 处理逻辑**：当前为占位（仅读大小）。真实业务处理需替换该函数体。

## 十二、可扩展方向

- 把指标接 FastAPI + ECharts 做按设备/月份的对齐图表与 Web 展示。
- 查看器增加按设备/月份聚合统计与图表。
- 为 `process_file` 接入真实解析/入库逻辑，使「处理」不再只是占位。
- 大批量重算/补算（尤其 `Z:/` 网络盘）可考虑 `multiprocessing` 并行计算（GIL 限制下多线程无效），并缩短 DB 锁持有时间，避免重算期间阻塞扫描写库。
