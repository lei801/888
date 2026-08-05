"""analysis.py —— 所有「文件级分析指标」的唯一真相来源

设计目标：
- 抽离计算逻辑，在线（scanner_gui.py）/ 离线重算（主 UI「重新计算指标」按钮）/ 查看器（db_view.py）
  三处共用同一份逻辑，新增指标只改本文件的 METRICS 注册表，无需改动其它文件。
- 在线：本轮扫描结束后对本轮新增文件批量计算。
- 离线：主 UI 「重新计算指标」按钮（可按月份局部/全量重算）。

新增指标步骤：
1. 在 METRICS 里加一项（key / label / type / table / column / compute）。
2. 写一个 compute(path) -> value 函数并赋值给该项的 compute。
   文件不可达等异常时回落为默认值（0 / 空串），由调用方统一捕获。
3. 完成。建表、落库、查看器列、导出表头都会自动适配。
"""
from __future__ import annotations

import os
import re
import csv
import io
import json
import functools
import sqlite3
import datetime
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager


@contextmanager
def _nullcontext():
    yield


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------
# 设备号 -> 产线 分类
# 规则：E 开头 -> 产线 E；2 开头 -> 产线 C；D 开头 -> 产线 D；A 开头 -> 产线 A；其余 -> 空。
# ----------------------------------------------------------------------------
def classify_line(device):
    """根据设备号前缀判定所属产线，用于按产线自动筛选设备。"""
    if not device:
        return ""
    d = device.strip().upper()
    if d.startswith("E"):
        return "E"
    if d.startswith("2"):
        return "C"
    if d.startswith("D"):
        return "D"
    if d.startswith("A"):
        return "A"
    return ""


# ----------------------------------------------------------------------------
# 指标注册表（新增指标只改这里）
#   key     : 指标唯一标识
#   label   : 显示名（查看器列名 / CSV 表头）
#   type    : SQL 类型
#   column  : 在合并后的 metrics 宽表中的列名（path 只存一份，零迁移）
#   compute : (path) -> value
# ----------------------------------------------------------------------------
METRICS = {}


def _decode_text(path):
    """统一以 cp932(日文 Shift-JIS) 解码源 CSV（你们的日志均为日文编码）。
    用字节读取后按 cp932 解码；cp932 失败再试一次 utf-8（兼容混排的纯 utf-8 文件）；
    两者都失败才回退忽略坏字节，避免日文第二字节(0x2C/0x09)被误判为分隔符、或坏字节导致整文件解析失败。
    返回 (text, encoding_name)。"""
    with open(path, "rb") as fb:
        raw = fb.read()
    try:
        return raw.decode("cp932"), "cp932"
    except UnicodeDecodeError:
        pass
    # cp932 失败，再试一次 utf-8（兼容纯 utf-8 文件，避免被 cp932 误解码成乱码）
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # cp932 与 utf-8 均失败：忽略坏字节兜底，避免解析崩溃
    return raw.decode("cp932", errors="ignore"), "cp932(ignore)"


# ----------------------------------------------------------------------------
# 共享解析（带缓存）：同一文件被多个指标计算时只解析一次，省网络盘 I/O
# ----------------------------------------------------------------------------
def _parse_csv(path):
    """读取并按逗号分隔解析 CSV，返回 (rows, delim, enc)。
    网络不可达时由 _decode_text/open 抛 OSError，调用方回落为默认值(0/空)。"""
    text, enc = _decode_text(path)
    delim = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    return rows, delim, enc


@functools.lru_cache(maxsize=256)
def _parse_csv_cached(path):
    return _parse_csv(path)


def _num(cell):
    """把单元格内容解析成 int/float，失败返回 None。"""
    cell = (cell or "").strip()
    if not cell:
        return None
    try:
        return int(cell)
    except ValueError:
        try:
            return float(cell)
        except ValueError:
            return None


def _metric_hinmei(path):
    """E 列（第 5 列）第 2 行（表头下首条数据行）的内容，名称：原品名。"""
    try:
        rows, _delim, enc = _parse_csv_cached(path)
    except OSError:
        return ""
    val = ""
    if len(rows) > 1 and len(rows[1]) > 4:
        val = (rows[1][4] or "").strip()
    return val


# ----------------------------------------------------------------------------
# PQ 迁移：报警代码清单（原 Excel MISS_CODE 表）。
# 改清单只动这一行 —— 内层/外层各代码列自动生成，ensure_tables 自动 ALTER 补列。
# ----------------------------------------------------------------------------
MISS_CODES = [570, 586, 587, 588]

# 源 CSV 列索引（0 基）。CSV 列按字母 A,B,C,... 排列，A=第1列=索引0。
#   A=0  B=1  C=2  D=3  E=4  F=5  G=6  H=7  I=8  J=9  K=10 L=11 M=12 ...
#   Z=25 AA=26 AB=27
# 注意：PQ 公式里的「L」（报警码/区分判定值）取自 CSV 的第 I 列（積重ね枚数，索引 8），
#       而非字母 L 列（索引 11 = ﾒｯｾｰｼﾞNo(積重ね部)）。
_C_HINMEI = 4    # E 列（品名原）
_C_K = 10        # K 列（区分：0内層/1外層/10/11）
_C_L = 8         # I 列（積重ね枚数）—— PQ 公式的「L」实际取此列
_C_Z = 25        # Z 列
_C_AB = 27       # AB 列


def _parse_filename(name):
    """按 PQ 口径解析文件名，返回 (lot, block, date_str)；任一项失败给空串。

    形如 CPU1-20250703-095722-253UNJR000-005.csv：
      日期  = [5:13]                    -> 2025-07-03
      LOT   = [21:31]                   -> 253UNJR000
      Block = 去扩展名后最后一个 '-' 之后的段 -> 005
              （PQ 原式取首个 '_' 前 3 字符，但真实文件名无 '_'，该式在 PQ 里会报错；
                此处按文件名真实结构取末段）
    """
    stem = os.path.splitext(name or "")[0]
    lot = stem[21:31]
    block = stem.rsplit("-", 1)[-1] if "-" in stem else ""
    fdate = ""
    ds = stem[5:13]
    if len(ds) == 8 and ds.isdigit():
        try:
            datetime.date(int(ds[0:4]), int(ds[4:6]), int(ds[6:8]))
            fdate = f"{ds[0:4]}-{ds[4:6]}-{ds[6:8]}"
        except ValueError:
            fdate = ""
    return lot, block, fdate


def _format_hinmei(raw):
    """PQ 品名格式化：s[0:2]-s[3:5]-s[10:13]-s[-3:]（越界部分自然为空串）。"""
    s = raw or ""
    tail = s[-3:] if len(s) >= 3 else s
    return f"{s[0:2]}-{s[3:5]}-{s[10:13]}-{tail}"


def _stdev(vals):
    """样本标准差（n-1，与 PQ 的 List.StandardDeviation 一致）；不足 2 个值返回 None。"""
    n = len(vals)
    if n < 2:
        return None
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return round(var ** 0.5, 2)


def _empty_stats():
    """文件不可达 / 解析失败时的空结果（所有指标回落为默认值）。"""
    st = {
        "lot": "", "block": "", "fdate": "",
        "hinmei_fmt": "",
        "is_mass": "", "total": 0, "outer_total": 0,
        "inner_sum": 0, "outer_sum": 0, "inner_odd": 0,
        "std_inner": None, "std_outer": None,
    }
    for c in MISS_CODES:
        st[f"inner_{c}"] = 0
        st[f"outer_{c}"] = 0
    return st


@functools.lru_cache(maxsize=256)
def _file_stats(path):
    """单遍解析源 CSV，产出 PQ 的全部派生量（各指标 compute 只从此 dict 取数）。

    忠实复刻 PQ：
      有效数据 = K、L 均可转为数字的行（PQ 的「有效数据」前置过滤，所有统计基于此）；
      总枚数   = 最后一个有效行的 L 值（量产文件分支；__csv 非量产文件走 K∈{0,1} 计数，
                 但扫描器只收 *.csv，该分支实际不会触发，保留作兜底）；
      内层code = K=20 且 L=code 的行数；外层code = K=21 且 L=code 的行数；
      内层MISS奇数 = K=20、L∈清单 且 L 为奇数的行数；
      外层枚数 = K=1 的行数；
      内层/外层剥离值方差 = K=0 的 Z / K=1 的 AB 的样本标准差（n-1，≥2 个值），round 2。
    """
    st = _empty_stats()
    name = os.path.basename(path)
    st["lot"], st["block"], st["fdate"] = _parse_filename(name)
    # 是否量产（PQ 的「是否量产」）：以 "__.csv" 结尾的文件判定为非量产，其余为量产。
    is_mass = not name.endswith("__.csv")
    try:
        rows, _delim, _enc = _parse_csv_cached(path)
    except OSError:
        return st

    # 品名（格式化）：取第 2 行的品名原（PQ 是分组内第 2 行）
    if len(rows) > 1 and len(rows[1]) > _C_HINMEI:
        st["hinmei_fmt"] = _format_hinmei((rows[1][_C_HINMEI] or "").strip())
    else:
        st["hinmei_fmt"] = _format_hinmei("")

    codes = set(MISS_CODES)
    inner = {c: 0 for c in MISS_CODES}
    outer = {c: 0 for c in MISS_CODES}
    z_vals, ab_vals = [], []
    last_l = None
    k01 = 0
    k1 = 0
    odd = 0

    for row in rows:
        n = len(row)
        if n <= _C_L:
            continue
        k = _num(row[_C_K])
        l = _num(row[_C_L])
        if k is None or l is None:      # PQ「有效数据」过滤
            continue
        last_l = l
        if k == 0 or k == 1:
            k01 += 1
        if k == 1:
            k1 += 1
        if k == 20:
            if l in codes:
                inner[l] += 1
                if int(l) % 2 == 1:
                    odd += 1
        elif k == 21:
            if l in codes:
                outer[l] += 1
        if k == 0 and n > _C_Z:
            z = _num(row[_C_Z])
            if z is not None:
                z_vals.append(z)
        elif k == 1 and n > _C_AB:
            ab = _num(row[_C_AB])
            if ab is not None:
                ab_vals.append(ab)

    if is_mass:
        st["total"] = int(last_l) if last_l is not None else 0
    else:
        st["total"] = k01
    st["outer_total"] = k1
    for c in MISS_CODES:
        st[f"inner_{c}"] = inner[c]
        st[f"outer_{c}"] = outer[c]
    st["inner_sum"] = sum(inner.values())
    st["outer_sum"] = sum(outer.values())
    st["inner_odd"] = odd
    st["std_inner"] = _stdev(z_vals)
    st["std_outer"] = _stdev(ab_vals)
    st["is_mass"] = "非量产" if not is_mass else "量产"
    return st


def _make_stat_getter(field, default=0):
    """生成从 _file_stats 取某字段的 compute。"""
    def _f(path):
        try:
            return _file_stats(path).get(field, default)
        except OSError:
            return default
    return _f


# ---- 指标注册（新指标统一走 metrics 宽表的各列，path 只存一份，零迁移）----
# 原品名：E 列第 2 行原文
METRICS["hinmei"] = {
    "label": "原品名",
    "type": "TEXT",
    "column": "hinmei",
    "compute": _metric_hinmei,
}
# 品名：PQ 格式化品名 AA-BB-CCC-DDD
METRICS["hinmei_fmt"] = {
    "label": "品名",
    "type": "TEXT",
    "column": "hinmei_fmt",
    "compute": _make_stat_getter("hinmei_fmt", ""),
}
# 文件名解析三要素
METRICS["lot"] = {
    "label": "LOT", "type": "TEXT", "column": "lot",
    "compute": _make_stat_getter("lot", ""),
}
METRICS["block"] = {
    "label": "Block", "type": "TEXT", "column": "block",
    "compute": _make_stat_getter("block", ""),
}
METRICS["fdate"] = {
    "label": "日期", "type": "TEXT", "column": "fdate",
    "compute": _make_stat_getter("fdate", ""),
}
# 是否量产：以 "__.csv" 结尾判定为非量产，其余为量产
METRICS["is_mass"] = {
    "label": "是否量产", "type": "TEXT", "column": "is_mass",
    "compute": _make_stat_getter("is_mass", ""),
}
# 总枚数（PQ 口径：末个有效行的 L 值）
METRICS["total_max"] = {
    "label": "总枚数", "type": "INTEGER", "column": "total_max",
    "compute": _make_stat_getter("total"),
}
# 外层枚数：K=1 行数
METRICS["outer_total"] = {
    "label": "外层枚数", "type": "INTEGER", "column": "outer_total",
    "compute": _make_stat_getter("outer_total"),
}
# 内层/外层 各报警代码计数（随 MISS_CODES 自动增减）
for _c in MISS_CODES:
    METRICS[f"inner_{_c}"] = {
        "label": f"内层{_c}", "type": "INTEGER", "column": f"inner_{_c}",
        "compute": _make_stat_getter(f"inner_{_c}"),
    }
for _c in MISS_CODES:
    METRICS[f"outer_{_c}"] = {
        "label": f"外层{_c}", "type": "INTEGER", "column": f"outer_{_c}",
        "compute": _make_stat_getter(f"outer_{_c}"),
    }
# 总回数 / 奇数 / 剥离值方差
METRICS["inner_sum"] = {
    "label": "内层MISS总回数", "type": "INTEGER", "column": "inner_sum",
    "compute": _make_stat_getter("inner_sum"),
}
METRICS["outer_sum"] = {
    "label": "外层MISS总回数", "type": "INTEGER", "column": "outer_sum",
    "compute": _make_stat_getter("outer_sum"),
}
METRICS["inner_odd"] = {
    "label": "内层MISS奇数", "type": "INTEGER", "column": "inner_odd",
    "compute": _make_stat_getter("inner_odd"),
}
METRICS["std_inner"] = {
    "label": "内层剥离值方差", "type": "REAL", "column": "std_inner",
    "compute": _make_stat_getter("std_inner", None),
}
METRICS["std_outer"] = {
    "label": "外层剥离值方差", "type": "REAL", "column": "std_outer",
    "compute": _make_stat_getter("std_outer", None),
}
# 文件大小（字节）：原 processed.size 列，现并入 metrics 宽表。
# 仅在「计算指标」阶段取值（os.path.getsize 一次网络 stat），扫描阶段不取，避免性能回退。
METRICS["size"] = {
    "label": "大小", "type": "INTEGER", "column": "size",
    "compute": lambda path: os.path.getsize(path),
}


# ----------------------------------------------------------------------------
# 对外统一接口（在线 / 离线 / 查看器共用）
# ----------------------------------------------------------------------------

def ensure_tables(conn, lock=None):
    """建合并后的指标宽表 metrics（id 主键 + path + 各指标列 + run_at）。

    数据库会整体重置，故只做干净建表，不做任何 schema 迁移 / 兼容补列。
    ym / device 属于 processed 表，此处不建（避免索引指向不存在列）。
    """
    cm = lock if lock is not None else _nullcontext()
    with cm:
        with conn:
            cols = ", ".join(f"{m['column']} {m['type']}" for m in METRICS.values())
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS metrics ("
                f"id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, "
                f"run_at TEXT, {cols})"
            )


def compute_all(path):
    """对单个文件跑【所有】指标，返回 {key: value}。在线离线同此函数。"""
    out = {}
    for key, m in METRICS.items():
        try:
            out[key] = m["compute"](path)
        except Exception:  # 单个指标出错不影响其它指标
            out[key] = None
    return out


def compute_paths(paths, workers=1, stop_event=None):
    """生成器：对 paths 逐个 compute_all，按完成顺序产出 (path, results)。

    - workers 为 None 或 <=1（或文件数<=1）时退化为串行，与旧实现行为一致；
    - workers>1 时用线程池并行计算（读网络盘 I/O 等待为主，多线程有实际收益）；
    - stop_event 置位时停止派发新任务，已在飞行的任务结果直接丢弃；
    - 调用方负责把产出的结果批量落库（SQLite 写库必须单线程串行）。
    """
    total = len(paths)
    if workers is None:
        workers = 1
    if workers <= 1 or total <= 1:
        for p in paths:
            if stop_event is not None and stop_event.is_set():
                break
            yield p, compute_all(p)
        return
    it = iter(paths)
    futs = {}
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        def _fill():
            # 窗口式派发：最多保持 workers*2 个在飞任务，避免一次性为大列表建海量 Future
            while len(futs) < workers * 2:
                if stop_event is not None and stop_event.is_set():
                    return
                try:
                    p = next(it)
                except StopIteration:
                    return
                futs[ex.submit(compute_all, p)] = p
        _fill()
        while futs:
            if stop_event is not None and stop_event.is_set():
                break
            done_set, _ = wait(futs, return_when=FIRST_COMPLETED)
            for fut in done_set:
                p = futs.pop(fut)
                try:
                    res = fut.result()
                except Exception:
                    res = {k: None for k in METRICS}
                yield p, res
            _fill()
    finally:
        # 取消/正常结束：撤销未开始的任务；在飞任务跑完后线程自然退出（结果不再使用）
        ex.shutdown(wait=False, cancel_futures=True)


def _upsert_rows(conn, path, results, run_at):
    """实际落库（不含锁），由 upsert / recalc_by_months 调用，避免重复加锁导致死锁。
    合并写入单张 metrics 宽表：path 只存一次，各指标各一列。"""
    with conn:  # 单条事务
        cols = ["path", "run_at"] + [m["column"] for m in METRICS.values()]
        col_sql = ", ".join(cols)
        placeholders = ",".join("?" * len(cols))
        vals = [path, run_at] + [results[k] for k in METRICS]
        conn.execute(
            f"INSERT OR REPLACE INTO metrics ({col_sql}) VALUES ({placeholders})",
            vals,
        )


def _upsert_rows_batch(conn, items, run_at):
    """批量落库：items 为 [(path, results), ...]，一次性 executemany 提交（默认每 1000 个调用一次）。
    减少事务提交次数，显著降低大批量重算的落盘开销。"""
    if not items:
        return
    with conn:  # 整批一个事务
        cols = ["path", "run_at"] + [m["column"] for m in METRICS.values()]
        col_sql = ", ".join(cols)
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT OR REPLACE INTO metrics ({col_sql}) VALUES ({placeholders})"
        conn.executemany(sql, [
            [path, run_at] + [results[k] for k in METRICS]
            for path, results in items
        ])


def upsert(conn, lock, path, results, run_at=None):
    """把 compute_all 的结果落库（覆盖写，保证在线/离线一致）。"""
    run_at = run_at or _now()
    cm = lock if lock is not None else _nullcontext()
    with cm:
        _upsert_rows(conn, path, results, run_at)


def upsert_batch(conn, lock, items, run_at=None):
    """批量落库（含锁）：items 为 [(path, results), ...]。供扫描/历史回填本轮末统一写库，
    一次事务提交整批，减少事务提交开销（用法同 upsert，但按批调用）。"""
    if not items:
        return
    run_at = run_at or _now()
    cm = lock if lock is not None else _nullcontext()
    with cm:
        _upsert_rows_batch(conn, items, run_at)


def recalc_by_months(conn, lock, ym_list, dev_list=None, on_progress=None, stop_event=None,
                     workers=None):
    """按月份（可选）和/或设备（可选）重算（局部重算）。

    - ym_list 为空/None  => 不限月份
    - dev_list 为空/None => 不限设备
    二者都为空 => 全量重算。返回处理的文件数。

    workers 为并行计算线程数（None/1=串行）；落库仍单线程批量串行。
    仅加一次锁，循环内调用 _upsert_rows 不再重复加锁，避免与扫描线程死锁。
    """
    cm = lock if lock is not None else _nullcontext()
    with cm:
        conds, params = [], []
        if ym_list:
            ph = ",".join("?" * len(ym_list))
            conds.append(f"ym IN ({ph})")
            params.extend(ym_list)
        if dev_list:
            ph = ",".join("?" * len(dev_list))
            conds.append(f"device IN ({ph})")
            params.extend(dev_list)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        paths = [
            r[0]
            for r in conn.execute(f"SELECT path FROM processed {where}", params)
        ]
        run_at = _now()
        # 批量提交：每 1000 个文件一次性落库，减少事务提交开销
        _BATCH = 1000
        total = len(paths)
        step = max(1, total // 100)
        buf = []
        done = 0
        for p, res in compute_paths(paths, workers, stop_event):
            if stop_event is not None and stop_event.is_set():
                break  # 用户取消：停止取后续结果，已算出的 buf 照常落库
            buf.append((p, res))
            done += 1
            if len(buf) >= _BATCH:
                _upsert_rows_batch(conn, buf, run_at)
                buf = []
            if on_progress is not None and done % step == 0:
                on_progress(done, total)
        if buf:
            _upsert_rows_batch(conn, buf, run_at)
        if on_progress is not None:
            on_progress(done, total)
    return done


def recalc_missing(conn, lock, ym_list, dev_list=None, on_progress=None, stop_event=None,
                   workers=None):
    """仅补齐「缺失指标」的文件（避免全量重算），覆盖两类情况：
    - processed 有记录但 metrics 无对应行（历史上关掉『找到新增后算指标』扫入的文件）；
    - metrics 行存在，但注册表新增了指标列、该行该列为 NULL（旧文件缺新指标）。
    命中文件执行 compute_all + upsert（幂等，覆盖写全列，已完整的列值不变）。返回补齐的文件数。

    workers 为并行计算线程数（None/1=串行）；落库仍单线程批量串行。
    """
    cm = lock if lock is not None else _nullcontext()
    with cm:
        conds, params = [], []
        if ym_list:
            ph = ",".join("?" * len(ym_list))
            conds.append(f"p.ym IN ({ph})")
            params.extend(ym_list)
        if dev_list:
            ph = ",".join("?" * len(dev_list))
            conds.append(f"p.device IN ({ph})")
            params.extend(dev_list)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        # 缺失判定：无 metrics 行，或任一指标列为 NULL
        null_conds = ["m.path IS NULL"]
        for m in METRICS.values():
            null_conds.append(f"m.{m['column']} IS NULL")
        missing_where = "(" + " OR ".join(null_conds) + ")"
        sql = (f"SELECT p.path FROM processed p "
               f"LEFT JOIN metrics m ON p.path = m.path "
               f"{where} {'AND' if where else 'WHERE'} {missing_where}")
        paths = [r[0] for r in conn.execute(sql, params)]
        run_at = _now()
        # 批量提交：每 1000 个文件一次性落库，减少事务提交开销
        _BATCH = 1000
        total = len(paths)
        step = max(1, total // 100)
        buf = []
        done = 0
        for p, res in compute_paths(paths, workers, stop_event):
            if stop_event is not None and stop_event.is_set():
                break  # 用户取消：停止取后续结果，已算出的 buf 照常落库
            buf.append((p, res))
            done += 1
            if len(buf) >= _BATCH:
                _upsert_rows_batch(conn, buf, run_at)
                buf = []
            if on_progress is not None and done % step == 0:
                on_progress(done, total)
        if buf:
            _upsert_rows_batch(conn, buf, run_at)
        if on_progress is not None:
            on_progress(done, total)
    return done


def column_defs():
    """供查看器动态生成列：返回 [(sql_expr, label, width), ...]。"""
    defs = []
    for key, m in METRICS.items():
        expr = f"COALESCE(m.{m['column']},'')"
        defs.append((expr, m["label"], 130))
    return defs


def join_clause():
    """供查看器拼 SQL：单 LEFT JOIN 合并后的 metrics 宽表。"""
    return "LEFT JOIN metrics m ON p.path=m.path"


def summarize(conn):
    """生成汇总文本（供命令行离线重算后打印）。"""
    total = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    lines = [
        f"文件总数: {total}",
        f"已计算指标: {done}",
    ]
    rows = conn.execute(
        "SELECT p.ym, COUNT(*) "
        "FROM processed p LEFT JOIN metrics m ON p.path=m.path "
        "GROUP BY p.ym ORDER BY p.ym"
    ).fetchall()
    lines.append("按年月:")
    for ym, cnt in rows:
        ym = ym or "(空)"
        lines.append(f"  {ym}: 文件 {cnt}")
    return "\n".join(lines)
