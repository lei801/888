#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_csv.py —— 按「数据月份(ym)」导出 CSV（只读连接、并发安全、只更新变动月）。

特点：
  - 只读连接（mode=ro）连活库，扫描器照常写，互不阻塞（WAL 天然支持）。
  - 按 ym 拆成每月一个文件：FRST LOG-<ym>.csv（如 FRST LOG-202607.csv）。
  - 【只更新变动的月】：推导「当前月 + 扫描器仍在带扫的上月」（读 scan_state.ini 逐设备确认状态），
    仅重写这些月的文件；与扫描器落库范围完全一致（不扫库、不依赖 ts/run_at 索引）。
    历史月文件原样保留（省 IO，百万级也不扫全表）。
  - 原子写：先写 .tmp 再 os.replace 改名，读取方永远读到完整文件。
  - 列定义与查看器(db_view.py)一致：默认全列；若 csv_export_cols.json 存在则按其中选中列导出。
  - 中文表头、UTF-8-SIG（Excel / 各类工具直接打开不乱码）。
  - 库里已无数据的月份，会自动删除对应 CSV，避免残留。

用法：
  python export_csv.py --outdir "D:\\CSV\\scan_data"              # 每小时定时：只更新变动的月
  python export_csv.py --outdir "D:\\CSV\\scan_data" --all        # 强制导出所有月份
  python export_csv.py --outdir "D:\\CSV\\scan_data" --ym 202607           # 只导某月
  python export_csv.py --outdir "D:\\CSV\\scan_data" --ym 202601..202607   # 导月份区间
  python export_csv.py --outdir "D:\\CSV\\scan_data" --device D32 --status ok   # 带筛选

接入：获取数据 → 文件夹 → 选 --outdir 目录 → 合并所有月度 CSV 为一张大表（含「月份」列）。
"""
from __future__ import annotations
import os
import re
import csv
import json
import argparse
import datetime
import sqlite3
import time

import analysis  # 复用指标列定义：column_defs / join_clause

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "scan_state.db")
INI_FILE = os.path.join(HERE, "scan_state.ini")
EXPORT_COLS_PATH = os.path.join(HERE, "csv_export_cols.json")
STATE_PATH = os.path.join(HERE, "csv_export_state.json")
DEFAULT_OUTDIR = os.path.join(HERE, "csv_export")


# ---- 列的 SQL 表达式（与 db_view.py 对齐）----
def _build_columns():
    base = [
        ("设备", "p.device"),
        ("月份", "p.ym"),
        ("路径", "p.path"),
        ("大小", "p.size"),
        ("处理时间", "p.ts"),
    ]
    metric = [(d[1], d[0]) for d in analysis.column_defs()]  # (label, expr)
    return base + metric


ALL_COLUMNS = _build_columns()           # [(label, expr)]
LABEL_INDEX = {label: i for i, (label, _) in enumerate(ALL_COLUMNS)}


def _load_ini_outdir():
    """读 scan_state.ini 的 [csv_export] outdir（可选兜底）。"""
    if not os.path.isfile(INI_FILE):
        return None
    try:
        section = False
        outdir = None
        with open(INI_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = (line[1:-1].strip() == "csv_export")
                    continue
                if section and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == "outdir":
                        outdir = v.strip()
        return outdir or None
    except Exception:
        return None


def _read_config_value(key, default):
    """读 scan_state.ini [config] 段的标量配置（值按 json 还原类型），缺失/异常回退 default。

    扫描器的 prev_confirmed 等运行状态同处持久化，导出侧读同一源即可与扫描器严格对齐。
    """
    if not os.path.isfile(INI_FILE):
        return default
    try:
        in_section = False
        with open(INI_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("[") and line.endswith("]"):
                    in_section = (line[1:-1].strip() == "config")
                    continue
                if in_section and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        return json.loads(v.strip())
        return default
    except Exception:
        return default


def _load_export_cols(cols_file=EXPORT_COLS_PATH):
    """返回要导出的列 [(label, expr)]；cols_file 不存在则返回全列。"""
    if os.path.isfile(cols_file):
        try:
            with open(cols_file, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, list) and saved:
                chosen = [ALL_COLUMNS[LABEL_INDEX[label]]
                          for label in saved if label in LABEL_INDEX]
                if chosen:
                    return chosen
        except Exception:
            pass
    return list(ALL_COLUMNS)


def _resolve_columns(labels):
    """由列名列表解析出 [(label, expr)]；空或全非法则返回全列。"""
    if not labels:
        return list(ALL_COLUMNS)
    chosen = [ALL_COLUMNS[LABEL_INDEX[label]]
              for label in labels if label in LABEL_INDEX]
    return chosen if chosen else list(ALL_COLUMNS)


def _conn():
    try:
        return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return sqlite3.connect(DB)


def _parse_ym_arg(ym):
    """202607 -> ['202607']；202601..202607 -> 区间内所有月份字符串列表。"""
    if not ym:
        return None
    if ".." in ym:
        a, b = ym.split("..", 1)
        cur = int(a[:4]) * 12 + int(a[4:6]) - 1
        end = int(b[:4]) * 12 + int(b[4:6]) - 1
        out = []
        while cur <= end:
            y, m = divmod(cur, 12)
            out.append("%04d%02d" % (y, m + 1))
            cur += 1
        return out
    return [ym]


def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        # last_export_max_id：每 ym 上次导出时的 MAX(id) 快照（dict）；旧版标量/缺失归一为空 dict
        snap = d.get("last_export_max_id", {})
        snap = snap if isinstance(snap, dict) else {}
        return d.get("last_run"), snap
    except Exception:
        return None, {}


def _save_state(last_run, last_export_max_id):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"last_run": last_run, "last_export_max_id": last_export_max_id}, f)
    except Exception:
        pass


def _write_export_log(entry, max_lines=1000):
    """追加一行导出日志到 csv_export.log（与脚本同目录），并限制最多保留 max_lines 行。

    首次创建带 UTF-8 BOM（utf-8-sig），使 Windows 记事本也能正确识别；
    后续追加用普通 UTF-8，避免重复写入 BOM 损坏文件。
    超过 max_lines 时只保留末尾 max_lines 行。
    """
    try:
        log_path = os.path.join(HERE, "csv_export.log")
        existed = os.path.exists(log_path)
        with open(log_path, "w" if not existed else "a",
                  encoding="utf-8-sig" if not existed else "utf-8") as f:
            f.write(entry + "\n")
        # 截断：仅保留末尾 max_lines 行
        with open(log_path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            with open(log_path, "w", encoding="utf-8-sig") as f:
                f.writelines(lines[-max_lines:])
    except Exception:
        pass


def _month_max_id(conn, ym):
    """返回该月当前最大自增 id（rowid）；无数据返回 0。用于增量裁决：上月 id 是否自上次导出后增长。"""
    row = conn.execute("SELECT MAX(id) FROM processed WHERE ym=?", (ym,)).fetchone()
    return row[0] or 0


def _changed_months(conn, last_run, last_export_max_id):
    """增量裁决：仅最近两个月（当前月 + 上月），各自按「与上次导出比有无新增」决定是否重写（纯 DB 比较）。

    裁决原则：某月 CSV 是否重写，唯一看该月 MAX(id) 是否比上次导出时记录的快照更大（即库里有新增行）。
    - 不再「当前月恒导出」：当前月没新增同样跳过；
    - 上月有新增才导出，无新增跳过；
    - 范围限定为最近两个月（cur + prev），更早的历史月份不自动导出（用 --ym / --all 显式导一次）。
    每次判定都是纯 DB 比较，不依赖时间窗口/扫描器状态；只要库里该月有新增就导出，没新增绝不导出。
    """
    now = time.localtime()
    cur = "%04d%02d" % (now.tm_year, now.tm_mon)
    if last_run is None:
        return _existing_months(conn)   # 首次运行：全量兜底一次
    prev = prev_ym(cur)
    target = set()
    for ym in (cur, prev):
        if _month_max_id(conn, ym) > last_export_max_id.get(ym, 0):
            target.add(ym)              # 该月有新增 → 重写其 CSV
    return target


def prev_ym(ym):
    """给定 'YYYYMM' 返回上一个月的 'YYYYMM' 字符串（与扫描器 scanner_gui.prev_ym 同义）。"""
    y = int(ym[:4]); m = int(ym[4:6])
    if m == 1:
        return "%04d12" % (y - 1)
    return "%04d%02d" % (y, m - 1)


def _existing_months(conn):
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT ym FROM processed WHERE ym IS NOT NULL AND ym <> ''")}


def _month_has_rows(conn, ym):
    return conn.execute(
        "SELECT 1 FROM processed WHERE ym=? LIMIT 1", (ym,)).fetchone() is not None


def _query_month(conn, ym, columns, filters):
    """查询某月（可选 device/status/path 筛选）的数据行，按 columns 顺序返回。"""
    sel = ", ".join(expr for _, expr in columns)
    where = ["p.ym = ?"]
    params = [ym]
    devs = filters.get("device")
    if devs:
        where.append("p.device IN (%s)" % ",".join("?" * len(devs)))
        params.extend(devs)
    path_q = filters.get("path")
    if path_q:
        pats = [p.replace("*", "%").replace("?", "_")
                for p in re.split(r"[|,;\s]+", path_q) if p]
        if pats:
            where.append("(" + " OR ".join(["p.path LIKE ?"] * len(pats)) + ")")
            params.extend(pats)
    sql = (f"SELECT {sel} FROM processed p {analysis.join_clause()} "
           f"WHERE {' AND '.join(where)} ORDER BY p.path")
    return conn.execute(sql, params).fetchall()


def _atomic_write(path, rows, columns):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([label for label, _ in columns])
        for row in rows:
            w.writerow(["" if v is None else v for v in row])
    os.replace(tmp, path)


def _cleanup_obsolete(conn, outdir):
    """删除导出目录里、库中已不存在月份的 CSV（仅增量模式调用）。"""
    db_months = _existing_months(conn)
    for fn in os.listdir(outdir):
        m = re.match(r"^FRST LOG-(\d{6})\.csv$", fn)
        if m and m.group(1) not in db_months:
            try:
                os.remove(os.path.join(outdir, fn))
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(
        description="按数据月份导出 CSV（只读、增量、原子写）")
    ap.add_argument("--outdir", help="导出目录（缺省读 scan_state.ini [csv_export] outdir，再否则 ./csv_export）")
    ap.add_argument("--device", nargs="*", help="只导指定设备（可多个）")
    ap.add_argument("--ym", help="月份 202607 或区间 202601..202607")
    ap.add_argument("--path", help="路径通配符（* ?），多条件用 | 分隔")
    ap.add_argument("--cols-file", help="列配置文件（json 列名列表）；缺省全列")
    ap.add_argument("--cols", nargs="*", help="列名列表（覆盖 --cols-file）；缺省全列")
    ap.add_argument("--all", action="store_true", help="忽略增量，导出所有月份")
    args = ap.parse_args()

    outdir = args.outdir or _load_ini_outdir() or DEFAULT_OUTDIR
    os.makedirs(outdir, exist_ok=True)

    if args.cols:
        columns = _resolve_columns(args.cols)
    elif args.cols_file:
        columns = _load_export_cols(args.cols_file)
    else:
        columns = list(ALL_COLUMNS)
    filters = {"device": args.device, "path": args.path}

    conn = _conn()
    try:
        if args.ym:
            target = set(_parse_ym_arg(args.ym))
            incremental = False
        elif args.all:
            target = _existing_months(conn)
            incremental = False
        else:
            last_run, last_export_max_id = _load_state()
            target = _changed_months(conn, last_run, last_export_max_id)
            incremental = True

        done = []
        for ym in sorted(target):
            path = os.path.join(outdir, f"FRST LOG-{ym}.csv")
            if not _month_has_rows(conn, ym):
                # 库里该月已无数据：删除旧文件（若有），不写空文件
                if os.path.exists(path):
                    os.remove(path)
                continue
            rows = _query_month(conn, ym, columns, filters)
            _atomic_write(path, rows, columns)
            done.append(ym)

        if incremental:
            _cleanup_obsolete(conn, outdir)
        # 更新增量状态：last_run 时间 + 本次重写的各月 MAX(id) 快照（用于下次判「该月是否新增」）
        # 必须在 conn.close() 之前执行，否则会操作已关闭的数据库连接
        snap = {}
        for ym in done:
            snap[ym] = _month_max_id(conn, ym)
        _save_state(_now_str(), snap)
    finally:
        conn.close()
    mode = "全量" if args.all else ("指定月" if args.ym else "增量(变动月)")
    summary = (f"{_now_str()} | 模式={mode} | 导出 {len(done)} 个月 -> {outdir}"
               + (f" | 月份: {', '.join(sorted(done))}" if done else " | 无变动月份"))
    print(f"[export] 模式={mode}，导出 {len(done)} 个月 -> {outdir}")
    if done:
        print("[export] 月份: " + ", ".join(sorted(done)))
    _write_export_log(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _write_export_log(f"{_now_str()} | 失败: {e}")
        raise
