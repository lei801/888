#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库维护脚本：
  1) 全量备份当前 scan_state.db（安全网，所有数据原样保留）。
  2) 给 processed 表补 device / ym 两列（从路径解析：设备号、BACKUP_YYYYMM 月份），便于未来对齐与按月份裁剪。
  3) 把「超过 1 年」的旧记录导出到独立归档库 + CSV（满足“把之前的数据备份一下”）。
  4) 主库只保留最近 KEEP_MONTHS 个月（默认 12），删除更旧的记录（满足“保留一年的结果是最新的数据库”）。
  5) 确保 metrics 宽表存在（存放每文件处理结果），并开启 WAL。

“1 年”的口径：以路径里的 BACKUP_YYYYMM（数据月份）为准，保留 (当前月 - KEEP_MONTHS) 及之后的记录；
即 数据月份 < (当前月 - KEEP_MONTHS) 的记录视为“超过 1 年”被归档并删除。
ym 解析不到（None）的记录默认保留，避免误删，并在结尾提示。

可调整：修改 KEEP_MONTHS 即可改变保留窗口。
"""
from __future__ import annotations
import os, re, shutil, sqlite3, csv, datetime
import analysis  # 复用 ensure_tables（建 metrics 宽表 + 旧表迁移）

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "scan_state.db")
KEEP_MONTHS = 12   # 保留最近多少个月（含当前月）

TS = datetime.date.today().strftime("%Y%m%d")


def ym_int(path: str):
    """从路径解析数据月份 YYYYMM（优先 BACKUP_YYYYMM，其次 CPU1-YYYYMMDD）。"""
    m = re.search(r"BACKUP_(\d{6})", path)
    if m:
        return int(m.group(1))
    m = re.search(r"CPU1-(\d{8})", path)
    if m:
        return int(m.group(1)[:6])
    return None


def device_of(path: str) -> str:
    """从路径解析设备号，如 Z:/D32/CPU1/... -> D32。"""
    parts = re.split(r"[\\/]+", path)
    if "CPU1" in parts:
        i = parts.index("CPU1")
        if i > 0:
            return parts[i - 1]
    return ""


def cur_ym_int() -> int:
    d = datetime.date.today()
    return d.year * 12 + (d.month - 1)


def ym_lit_to_months(ym: int) -> int:
    """把 YYYYMM 字面量(如 202507) 转成『距公元的月数』(2025*12+6)，便于同尺度比较。"""
    y, m = divmod(ym, 100)
    return y * 12 + (m - 1)


def run_maintain(log_cb=None, keep_months=None):
    """执行数据库维护：备份 + 补列 + 归档超期旧数据 + 主库瘦身。

    log_cb: 可选回调，接收一条进度文本（用于 GUI 实时显示）。
    keep_months: 保留最近多少个月（默认用模块 KEEP_MONTHS）。
    返回最后的汇总文本。
    """
    km = keep_months if keep_months else KEEP_MONTHS

    def _log(msg):
        if log_cb:
            log_cb(msg)
        else:
            print(msg)

    ts = datetime.date.today().strftime("%Y%m%d")
    if not os.path.isfile(DB):
        _log("未找到 %s" % DB)
        return "未找到数据库文件"

    # 1) 全量备份
    full_bak = os.path.join(HERE, "scan_state_backup_%s.db" % ts)
    shutil.copy2(DB, full_bak)
    _log("[1/5] 全量备份 -> %s" % full_bak)

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    analysis.ensure_tables(conn)   # 确保 metrics 宽表存在
    cur = conn.cursor()

    # 2) 回填 device/ym（主库已含这两列时 ALTER 会跳过；此处兜底）
    for col in ("device", "ym"):
        try:
            cur.execute("ALTER TABLE processed ADD COLUMN %s TEXT" % col)
        except sqlite3.OperationalError:
            pass  # 已存在
    rows = cur.execute("SELECT path, device, ym FROM processed").fetchall()
    upd = []
    for path, dev, ym in rows:
        nd = device_of(path)
        ny = ym_int(path)
        ny_s = None if ny is None else str(ny)
        if not dev or dev == "" or ny_s is None or (ym is not None and ym != ny_s):
            upd.append((nd, ny_s, path))
    if upd:
        cur.executemany("UPDATE processed SET device=?, ym=? WHERE path=?", upd)
    conn.commit()
    _log("[2/5] 已补/回填 device、ym 列，更新 %d 行" % len(upd))

    # 3) 计算裁剪边界，分离旧记录（统一用『距公元的月数』比较）
    cutoff_months = cur_ym_int() - km
    cy, cm = divmod(cutoff_months, 12)
    cutoff_ym_lit = cy * 100 + (cm + 1)   # 转回 YYYYMM 便于阅读
    all_rows = cur.execute(
        "SELECT path, ts, device, ym FROM processed").fetchall()
    old_rows = []
    keep_cnt = 0
    no_ym = 0
    for r in all_rows:
        ym_s = r[3]
        if ym_s is None or not str(ym_s).isdigit():
            no_ym += 1
            keep_cnt += 1               # ym 未知：保守保留
            continue
        if ym_lit_to_months(int(ym_s)) < cutoff_months:
            old_rows.append(r)
        else:
            keep_cnt += 1

    _log("[3/5] 裁剪边界(数据月份) < %d 视为超 1 年；旧记录 %d 条，保留 %d 条%s"
         % (cutoff_ym_lit, len(old_rows), keep_cnt,
            ("（其中 %d 条无月份，已保守保留）" % no_ym) if no_ym else ""))

    # 3a) 归档旧记录到独立库 + CSV
    if old_rows:
        arc_db = os.path.join(HERE, "scan_state_archive_%s.db" % ts)
        if os.path.exists(arc_db):
            os.remove(arc_db)
        a = sqlite3.connect(arc_db)
        a.execute("CREATE TABLE processed (id INTEGER PRIMARY KEY, "
                  "path TEXT UNIQUE NOT NULL, ts TEXT, "
                  "device TEXT, ym TEXT)")
        a.executemany(
            "INSERT INTO processed (path, ts, device, ym) "
            "VALUES (?, ?, ?, ?)", old_rows)
        a.commit()
        a.close()
        arc_csv = os.path.join(HERE, "scan_state_archive_%s.csv" % ts)
        with open(arc_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["path", "ts", "device", "ym"])
            w.writerows(old_rows)
        _log("       旧记录归档 -> %s" % arc_db)
        _log("       旧记录导出 -> %s" % arc_csv)

    # 4) 主库删除旧记录（指标随 metrics 一并删除）
    if old_rows:
        old_paths = [r[0] for r in old_rows]
        ph = ",".join("?" * len(old_paths))
        cur.execute("DELETE FROM metrics WHERE path IN (%s)" % ph, old_paths)
        cur.execute("DELETE FROM processed WHERE path IN (%s)" % ph, old_paths)
        conn.commit()
        _log("[4/5] 主库已删除 %d 条旧记录（仅保留最近 %d 个月）"
             % (len(old_paths), km))
    else:
        _log("[4/5] 无超 1 年的记录，主库保持不变")

    conn.commit()
    conn.close()
    _log("[5/5] 已确保 metrics 宽表存在，主库开启 WAL")
    _log("完成。主库现为“最近 %d 个月”的最新数据库；旧数据已归档备份。" % km)
    return "归档完成"


def preview_old(keep_months=None):
    """预览（不修改库）：返回 (old_count, keep_count, cutoff_ym_lit, no_ym)。"""
    km = keep_months if keep_months else KEEP_MONTHS
    if not os.path.isfile(DB):
        return (0, 0, None, 0)
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error:
        c = sqlite3.connect(DB)
    try:
        cutoff_months = cur_ym_int() - km
        cy, cm = divmod(cutoff_months, 12)
        cutoff_ym_lit = cy * 100 + (cm + 1)
        rows = c.execute("SELECT ym FROM processed").fetchall()
    finally:
        c.close()
    old_cnt = keep_cnt = no_ym = 0
    for (ym_s,) in rows:
        if ym_s is None or not str(ym_s).isdigit():
            no_ym += 1
            keep_cnt += 1
            continue
        if ym_lit_to_months(int(ym_s)) < cutoff_months:
            old_cnt += 1
        else:
            keep_cnt += 1
    return (old_cnt, keep_cnt, cutoff_ym_lit, no_ym)


def main():
    run_maintain(log_cb=None)


if __name__ == "__main__":
    main()
