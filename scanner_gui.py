#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
LOG数据扫描工具 · GUI 版（Python 标准库 tkinter）

功能：
  - 选择扫描路径（文件夹），扫描目录下全部文件（无后缀过滤）
  - 开始 / 暂停 / 停止 控制扫描；【开始】后进入持续监控，按界面设定的「轮询间隔」（默认 20 秒）
    自动扫描一轮，状态栏显示距下次扫描的倒计时，直到点【停止】才退出
  - 实时显示：当前处理的文件路径、已处理文件列表、扫描进度、统计数字
  - 增量逻辑：基于【文件名去重】，只处理「文件名没见过」的新增文件
  - 状态持久化：已处理文件路径 + 配置保存到本地 SQLite（scan_state.db，单文件、零依赖）；
    旧版 JSON（scan_state.json）在首次启动时自动迁移到 DB 并删除；下次启动自动加载，继续增量

增量判定规则（文件名去重，最快且最稳）：
  - 文件名全局唯一、不覆盖 → 去重只看「文件名是否见过」：没见过 = 候选（新增）。
  - 因此「迟到文件」下一轮扫描自然被当作新增抓到（做法 A），不会因 mtime 旧而被漏。
  - mtime / size 完全不参与判定，扫到没见过的新文件名就直接处理（不做写完判定）。
  - 设备备份盘（Z 盘：{设备}\CPU1\BACKUP_YYYYMM）支持「起始月份」：设首次搜索的起始月后，
    首次运行从起始月一路回填到当前月；之后每轮只轮询【当前月】目录（更快、状态有界）。
    起始月之前的历史目录不扫描（按需求默认 2025-01 起）。
  - 上月确认机制（逐设备）：每台设备独立判断。仅在月初窗口（prev_scan_hours，默认本月起 6 小时）内
    对「本月目录尚未出现」的设备顺带扫上月；本月目录一旦出现，当轮带扫上月作「最终确认」后仅扫本月。
    窗口外未确认的设备自然停扫上月（本月无生产，上月也不会有新文件）。确认标志为内存态不落盘；
    窗口内重启仅多扫一轮上月目录、当轮即全部重新确认，无漏扫（去重靠 processed_set + path UNIQUE）。
  - 非设备结构路径（如普通文件夹 / 单个 BACKUP 目录）自动回退为通用递归全量扫描。
  - 状态不裁剪、保留全部已处理记录，去重永久有效（状态随扫描过的文件数增长，属预期）。

线程模型：
  - 扫描在后台线程进行，UI 通过 queue 安全更新，不卡界面
  - pause_event / stop_event 控制暂停与停止
  - 状态在 停止 / 完成 / 暂停 时落盘，兼顾意外退出可恢复

依赖：仅 Python 标准库（tkinter, threading, queue, os, json ...）
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import sqlite3
import threading
from queue import Queue, Empty
from typing import Any

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import analysis  # 指标逻辑唯一来源：compute_all / upsert / ensure_tables / recalc_by_months
import export_csv  # 自动导出：复用的列定义(ALL_COLUMNS)与按月导出逻辑
import ui_common  # 共享筛选组件 LineDeviceMonthFilter / 状态色常量

_HERE = os.path.dirname(os.path.abspath(__file__))
RECALC_LOG = os.path.join(_HERE, "scan_state_recalc_log.txt")
DB_FILE = os.path.join(_HERE, "scan_state.db")          # 状态库（SQLite，单文件，存已处理文件路径）
INI_FILE = os.path.join(_HERE, "scan_state.ini")         # 配置（ini，存扫描设置等标量配置）

# ====================== 配置区 ======================
POLL_INTERVAL = 20   # 默认轮询间隔（秒），可在 UI「轮询间隔」中输入覆盖，持久化到 state
MAX_LOG_LINES = 500   # 列表区最多保留的已处理文件条目
_FLUSH_BATCH = 20000  # 内存优化：新增候选累积满该数量即落库 + 算指标 + 清空，避免整轮堆叠 pending_rows
# ====================================================

# ====================== 居中消息框 ======================
# tkinter 原生 messagebox 在 Windows 上始终居中于屏幕，无法随所属界面定位。
# 这里把 messagebox 的方法替换为居中版：弹出时精确对齐到父窗口几何中心。
# 现有 messagebox.showinfo/showwarning/showerror/askyesno(...) 调用全部自动生效，
# 无需逐处修改（parent 关键字照常支持）。
import tkinter.messagebox as _mb
import tkinter as _tk
from tkinter import ttk as _ttk


def _mb_root(parent):  # type: ignore[no-untyped-def]
    """弹窗父窗口：优先用传入的 parent，否则用 tkinter 默认根窗口。"""
    if parent is not None:
        return parent
    r = getattr(_tk, "_get_default_root", lambda: None)()
    assert r is not None
    return r


def _mb_center(dlg, parent):  # type: ignore[no-untyped-def]
    parent = _mb_root(parent)
    dlg.update_idletasks()
    dlg.geometry("+%d+%d" % (
        parent.winfo_rootx() + max(0, (parent.winfo_width() - dlg.winfo_width()) // 2),
        parent.winfo_rooty() + max(0, (parent.winfo_height() - dlg.winfo_height()) // 2)))


def _mb_box(parent, title, msg, buttons=("确定",)):  # type: ignore[no-untyped-def]
    parent = _mb_root(parent)
    dlg = _tk.Toplevel(parent)
    dlg.withdraw()
    dlg.title(title or "提示")
    dlg.resizable(False, False)
    res = {}
    body = _ttk.Frame(dlg, padding=(20, 16, 20, 8))
    body.pack(fill="both", expand=True)
    _tk.Label(body, text=msg, justify="left", anchor="w",
              wraplength=max(240, parent.winfo_width() - 100)).pack(fill="x")
    bar = _ttk.Frame(dlg, padding=(0, 0, 0, 12))
    bar.pack(fill="x")
    def _fin(v):
        res["v"] = v
        dlg.destroy()
    for b in reversed(buttons):
        _ttk.Button(bar, text=b, width=9, command=lambda b=b: _fin(b)) \
            .pack(side="right", padx=4)
    dlg.protocol("WM_DELETE_WINDOW", lambda: _fin(None))
    dlg.bind("<Return>", lambda e: _fin(buttons[-1]))
    dlg.grab_set()
    _mb_center(dlg, parent)
    dlg.deiconify()
    dlg.lift()
    parent.wait_window(dlg)
    return res.get("v")


_mb.showinfo = lambda title, msg, **kw: _mb_box(kw.get("parent"), title, msg)
_mb.showwarning = lambda title, msg, **kw: _mb_box(kw.get("parent"), title, msg)
_mb.showerror = lambda title, msg, **kw: _mb_box(kw.get("parent"), title, msg)
_mb.askyesno = lambda title, msg, **kw: \
    _mb_box(kw.get("parent"), title, msg, buttons=("是", "否")) == "是"
# ====================================================


# -------------------------- 状态持久化（SQLite） --------------------------
_DEFAULT_CONFIG = {
    "last_scan_time": 0,
    "scan_root": "",
    "start_year": 2025,
    "start_month": 1,
    "poll_interval": 20,
    # 月初上月兜底窗口（小时）：本月目录未出现的设备，仅在本月起前 N 小时内顺带扫上月（默认 6 小时）
    "prev_scan_hours": 6,
    "compute_after_scan": True,
    # 计算指标并行线程数：历史回填/重新计算/补算缺失共用；1=不并行，网络盘可适当调大
    "compute_workers": 4,
    # 并行方式：thread=串行（监控小批量够用、不养子进程、内存最省）；process=多进程（绕开 GIL，
    # CPU 密集回填/重算真并行提速，算完自动释放子进程内存）。已移除多线程。
    "compute_mode": "process",
    "device_list": [],   # 设备清单：首次运行由扫描路径嗅探并持久化；之后直接拼路径省去每轮 listdir
    # 监控阶段 mtime 优先缓存：目录路径 -> 上轮记录到的 mtime（浮点秒）。
    # 仅监控阶段使用；回填（历史全量）始终全量 scandir，不经此缓存。
    "dir_mtime_cache": {},
}


class _nullcontext:
    """无锁时的占位上下文管理器。"""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _open_db():
    """打开（必要时建表）SQLite 状态库；check_same_thread=False 以便后台线程共用，调用方用锁保护。

    新库直接采用 INTEGER rowid 主键（id 自增 + path UNIQUE 业务唯一键），二级索引指针仅 8 字节，
    百万级下库体几乎不膨胀；数据库会重置，故仅干净建表，不做任何 schema 迁移。
    """
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    # 开启 WAL：读写并发（自动导出/查看器的只读连接与扫描写互不阻塞）。
    # 该设置持久化于库文件，重复执行幂等；不再依赖手动跑 db_maintain.py。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS processed "
                 "(id INTEGER PRIMARY KEY, "
                 "path TEXT UNIQUE NOT NULL, ts TEXT, "
                 "device TEXT, ym TEXT)")
    conn.commit()
    return conn


def _read_ini() -> dict[str, Any]:
    """读取 scan_state.ini（[config] 段），值以 json 解析还原类型。"""
    cfg: dict[str, Any] = {}
    if not os.path.isfile(INI_FILE):
        return cfg
    section = False
    with open(INI_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = (line[1:-1].strip() == "config")
                continue
            if not section or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            try:
                cfg[k] = json.loads(v)
            except Exception:
                cfg[k] = v
    return cfg


def _write_ini(cfg: dict[str, Any]) -> None:
    """把配置写入 scan_state.ini（[config] 段），值以 json 序列化保留类型。"""
    with open(INI_FILE, "w", encoding="utf-8") as f:
        f.write("[config]\n")
        for k, v in cfg.items():
            f.write(f"{k} = {json.dumps(v, ensure_ascii=False)}\n")


def load_config() -> dict[str, Any]:
    """从 scan_state.ini 读取配置；缺失项用 _DEFAULT_CONFIG 补齐。"""
    cfg: dict[str, Any] = dict(_DEFAULT_CONFIG)
    cfg.update(_read_ini())
    return cfg


def save_config(cfg: dict[str, Any], lock: threading.Lock | None = None) -> None:
    """把配置写入 scan_state.ini（lock 仅为兼容旧签名，ini 单进程写入无需锁）。"""
    _write_ini(cfg)


def load_processed_set_for_targets(conn, targets: list[str],
                                   lock: threading.Lock | None = None) -> set[str]:
    """仅加载「本轮目标目录」下的已处理路径（窗口化去重集）。

    正常监控下 targets 只含当前月(+月初窗口内的上月兜底)，故内存恒定在
    当月量级（约 30~60 万条），而不再全量载入全年记录（可上千万条）。
    历史去重语义不变：本轮仅会枚举这些目录里的文件，历史目录本轮根本不扫，
    无需为它们保留去重信息。

    targets 中的目录路径与库内 path 同分隔符（OS 原生），按 'dir/%(或\\%)' 前缀匹配。
    path 列为 UNIQUE（有索引），'prefix%' 形式的 LIKE 可命中索引前缀匹配。
    """
    cm = lock if lock is not None else _nullcontext()
    if not targets:
        return set()
    with cm:
        out: set[str] = set()
        for d in targets:
            prefix = d.rstrip("\\/") + os.sep  # 尾部分隔符，保证匹配目录内文件而非同名前缀目录
            # SQLite LIKE 视 % 为通配；目录路径本身通常不含 %/_，无需转义
            for (p,) in conn.execute("SELECT path FROM processed WHERE path LIKE ?", (prefix + "%",)):
                out.add(p)
        return out


_DEV_RE = re.compile(r"[\\/]([^\\/]+)[\\/]")
_YM_RE = re.compile(r"BACKUP_(\d{6})")


def _dev_ym(path: str) -> tuple[str, str]:
    """从路径推断设备名与年月（与 db_view 展示口径一致）。

    标准备份结构为 {设备}/CPU1/BACKUP_YYYYMM/...，故设备名取 CPU1 的上一级目录；
    当路径不符合该结构时，回退为“第一个目录段”。这样即使扫描根目录是多级路径
    （如 C:/Users/xxx/Z 盘映射）也能正确归类设备，避免产线筛选误判。
    """
    m = re.search(r"[\\/]([^\\/]+)[\\/]CPU1(?:[\\/]|$)", path, re.IGNORECASE)
    if m:
        device = m.group(1)
    else:
        m2 = _DEV_RE.search(path)
        device = m2.group(1) if m2 else ""
    mm = _YM_RE.search(path)
    ym = mm.group(1) if mm else ""
    return device, ym


def insert_processed(conn, rows: list[tuple[Any, ...]], lock: threading.Lock | None = None) -> None:
    """批量写入新增记录（path, ts）；同时回填 device/ym，供查看与按月份重算。

    size 已挪到 metrics 表（由「计算指标」阶段填），processed 不再存 size/mtime。
    INSERT OR IGNORE 去重，仅新增行落盘。
    """
    if not rows:
        return
    cm = lock if lock is not None else _nullcontext()
    with cm:
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO processed "
                "(path, ts, device, ym) VALUES (?, ?, ?, ?)",
                [r + _dev_ym(r[0]) for r in rows])


def reset_processed_db(conn, lock: threading.Lock | None = None) -> None:
    """清空已处理记录表（重置状态用）。"""
    cm = lock if lock is not None else _nullcontext()
    with cm:
        with conn:
            conn.execute("DELETE FROM processed")


# -------------------------- 文件遍历（os.scandir 递归，性能优于 os.walk） --------------------------
def _safe_stat(path, fn, timeout=5.0):
    """带超时执行 os.path.isdir/isfile 等 stat：网络盘无响应目录的 stat 可能永久挂起，
    用子线程 + join(timeout) 兜底，超时返回 False（视为不可访问、跳过，避免永久卡死）。"""
    res = {}
    def _w():
        try:
            res["v"] = fn(path)
        except OSError:
            res["v"] = False
    th = threading.Thread(target=_w, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return False
    return res.get("v", False)


def _safe_scandir(root, timeout=15.0):
    """带超时的 os.scandir：无响应目录超时抛 OSError 由调用方上报，避免永久挂起卡死。"""
    res, err = {}, {}
    def _w():
        try:
            res["it"] = os.scandir(root)
        except OSError as e:
            err["e"] = e
    th = threading.Thread(target=_w, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise OSError("scandir 超时（目录可能无响应）：%s" % root)
    if "e" in err:
        raise err["e"]
    return res["it"]


def iter_files(root, retries=0, stop_event=None):
    """枚举 root 下所有【文件】并 yield (path, DirEntry)。
    约定：root 为结构固定的最底层目录（如 {设备}/CPU1/BACKUP_YYYYMM），其中只含文件、
          无子目录需要递归。因此仅用 DirEntry 缓存的 is_file() 判定，
          不再做目录判定 / 递归 / 二次网络 stat，单个 2000 条目的网络目录也不会触发上千次往返。
    顶层目录若无法访问，抛出异常由调用方上报（不再静默吞掉）。
    retries: 仅对【最顶层】scandir 生效的重试次数（应对网络盘启动瞬间未就绪）。
    stop_event: 传入则遍历每个条目前检查，命中即退出（使「停止」在枚举阶段也能及时响应）。
    """
    # 顶层 scandir 失败要抛出（或重试），让界面能显示错误，而不是静默变成 0 候选
    it = None
    for attempt in range(retries + 1):
        try:
            it = _safe_scandir(root)
            break
        except OSError as e:
            # 目录根本不存在（路径拼错/未生成）属确定失败：首次即放弃，不重试，避免 3×15s 空等
            msg = str(e)
            if getattr(e, "winerror", None) == 3 or "系统找不到" in msg or "No such file" in msg:
                raise
            if attempt >= retries:
                raise
            time.sleep(1.0)  # 网络盘偶尔未就绪，稍等再试
    assert it is not None
    with it:
        for entry in it:
            if stop_event is not None and stop_event.is_set():
                return  # 用户点了停止：结束枚举
            # 仅用 DirEntry 缓存判定（性能最优，无额外网络 stat / 无递归）
            try:
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                is_file = False
            if is_file:
                yield entry.path, entry


# -------------------------- 增量判定（纯文件名去重） --------------------------
def is_candidate(path: str, processed_set: set[str]) -> bool:
    """增量判定：文件名全局唯一、不覆盖 → 只要「文件名没见过」就是候选（新增）。
    processed_set: 已处理路径的内存集合（O(1) 判定）。"""
    return path not in processed_set


# -------------------------- 设备备份目录发现（Z 盘：{设备}\CPU1\BACKUP_YYYYMM） --------------------------
CPU_SUBDIR = "CPU1"  # 设备下存放备份的子目录名


def current_month_folder():
    """返回当前月备份目录名，如 'BACKUP_202607'。"""
    return "BACKUP_%04d%02d" % (time.localtime().tm_year, time.localtime().tm_mon)


def prev_ym(ym):
    """给定 'YYYYMM' 返回上一个月的 'YYYYMM' 字符串。"""
    y = int(ym[:4]); m = int(ym[4:6])
    if m == 1:
        return "%04d12" % (y - 1)
    return "%04d%02d" % (y, m - 1)


def ym_range(start_year, start_month):
    """返回 [起始年月起, 当前年月] 闭区间内所有 'YYYYMM' 字符串列表（含两端）。"""
    now = time.localtime()
    end_ym = now.tm_year * 100 + now.tm_mon
    start_ym = start_year * 100 + start_month
    if start_ym > end_ym:  # 起始月晚于当前月，直接退化为只扫当前月
        start_ym = end_ym
    out = []
    ym = start_ym
    while ym <= end_ym:
        out.append("%04d%02d" % (ym // 100, ym % 100))
        y, m = divmod(ym, 100)
        if m == 12:
            ym = (y + 1) * 100 + 1
        else:
            ym = y * 100 + (m + 1)
    return out


def discover_backup_targets(root, months, device_list, lines=None):
    """根据持久化的设备清单直接拼接形如 {设备}/{CPU_SUBDIR}/BACKUP_YYYYMM 的目录路径。
    不再做每轮 isdir 存在性探测（之前的 D + D×M 次网络往返全部省掉）；目录是否真实存在、
    能否扫描，交由下游 iter_files 的 scandir 超时/重试与「已处理跳过」逻辑处理。
    months: 'YYYYMM' 列表；返回目标目录绝对路径列表（按设备+月份排序）。
    device_list: 设备号清单（非空，首次运行已由扫描路径嗅探并持久化到配置）。直接用清单里的设备号
        逐个拼路径，不做 listdir 自动发现、不做 isdir 探测，纯本地拼接。
    lines: 可选产线筛选集合（如 {'E','D'}）。提供时对设备号先按 classify_line 前缀过滤再拼路径。
        None/空 = 不过滤。"""
    targets = []
    if not root or not device_list:
        return targets
    for dev in sorted(device_list):
        if lines and analysis.classify_line(dev) not in lines:
            continue  # 非目标产线设备：拼接前剔除
        dev_path = os.path.join(root, dev)
        for ym in months:
            cand = os.path.join(dev_path, CPU_SUBDIR, "BACKUP_" + ym)
            targets.append(cand)
    return targets


# -------------------------- GUI --------------------------
class ScannerApp:
    def __init__(self, root):
        self.root = root
        self._batch_cols = False  # 批量设置导出列期间跳过逐项写盘
        self.root.title("LOG数据扫描工具")
        # 默认尺寸按实际 DPI 自适应放大：基准 1100x800 乘 scale，并受屏幕 92%/90% 上限约束防溢出
        scale = _dpi_scale()
        _sw = self.root.winfo_screenwidth()
        _sh = self.root.winfo_screenheight()
        _w = min(int(700 * scale), int(_sw * 0.92))
        _h = min(int(600 * scale), int(_sh * 0.9))
        self.root.geometry(f"{_w}x{_h}")
        self.root.minsize(int(_w * 0.8), int(_h * 0.72))  # 防止缩太小导致控件错乱/裁剪

        self.db_lock: threading.Lock = threading.Lock()
        self.conn = _open_db()
        analysis.ensure_tables(self.conn, self.db_lock)   # 建指标宽表 metrics（数据库会重置，仅干净建表）
        self.state: dict[str, Any] = load_config()                      # 配置（ini）
        # 逐设备上月确认标志（内存态，仅月初窗口期用）：{设备号: 已确认到的月份YYYYMM}。
        # 不持久化——窗口外本就不读它；窗口内重启仅首轮多扫一次上月目录、当轮即全部重新确认，无漏扫。
        self._prev_confirmed: dict[str, str] = {}
        self.processed_set: set[str] = set()  # 本轮去重集（每轮按 targets 窗口化重建，见 _run）
        self.pending_rows: list[tuple[Any, ...]] = []           # 本轮待写入 DB 的新增记录
        self.thread = None
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.q = Queue()

        # 自动导出（按月 CSV）运行态：配置 UI 在任务中心「CSV 导出」页；线程挂在主程序，
        # 生命周期长于任务中心对话框——关闭对话框不影响已启动的定时导出。
        self.auto_stop = None          # threading.Event；None / 已 set = 未运行
        self.auto_thread = None
        # 导出列统一为一份 csv_cols（兼容迁移：优先旧 csv_auto_cols，回退 csv_manual_cols）
        if "csv_cols" not in self.state:
            self.state["csv_cols"] = (self.state.get("csv_auto_cols")
                                      or self.state.get("csv_manual_cols") or [])

        self._apply_style()
        self._build_ui()
        self._refresh_from_state()
        # 若配置启用了自动导出，则启动时自动恢复定时导出（失败仅记日志，不弹窗）
        if self.state.get("csv_auto_enabled"):
            self.start_auto_export(silent=True)
        # 启动 UI 轮询
        self.root.after(50, self._poll)

    def _save_config(self):
        """把配置（self.state）持久化到 scan_state.ini。"""
        save_config(self.state, self.db_lock)

    # ---------- 布局 ----------
    def _apply_style(self):
        """应用原生视觉风格：优先系统原生主题 + Microsoft YaHei 字体 + 蓝色强调。

        进程已声明 DPI 感知（见 _enable_high_dpi），Tk 会按系统缩放自动放大
        point 字号与像素布局，因此固定字号在 125%/150% 缩放下也会等比例放大、不模糊。
        """
        style = ttk.Style()
        # 优先 Windows 原生 vista；非 Windows 回退 xpnative / clam，保证跨平台一致且好看
        for pref in ("vista", "xpnative", "clam"):
            if pref in style.theme_names():
                try:
                    style.theme_use(pref)
                    break
                except Exception:
                    continue
        style.configure(".", font=("Microsoft YaHei", 9))
        style.configure("Accent.TButton", font=("Microsoft YaHei", 10, "bold"))
        # vista 原生主题下背景色被系统忽略，仅用文字颜色区分：开始=绿 / 暂停=橙 / 停止=红（纯色）
        style.configure("Start.TButton", foreground="#1aa12e",
                        font=("Microsoft YaHei", 10, "bold"))
        style.configure("Pause.TButton", foreground="#e07b00",
                        font=("Microsoft YaHei", 10, "bold"))
        style.configure("Stop.TButton", foreground="#d11a1a",
                        font=("Microsoft YaHei", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei", 14, "bold"))
        try:
            style.configure("TLabelframe.Label", font=("Microsoft YaHei", 9, "bold"))
        except Exception:
            pass

    def _build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)  # 主区域(行4)可伸缩

        # --- 标题栏（Vista 蓝色强调带）---
        header = tk.Frame(root, bg="#0078D7")
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="LOG数据扫描工具", bg="#0078D7", fg="white",
                 font=("Microsoft YaHei", 14, "bold")).pack(side="left", padx=14, pady=8)
        self.mode_var = tk.StringVar(value="—")          # 监控模式文字（数据源）
        self.title_status_var = tk.StringVar(value="—")   # 标题栏状态：运行中=模式 / 已暂停 / 已停止
        self.lbl_title_status = tk.Label(header, textvariable=self.title_status_var,
                                         bg="#0078D7", fg="#ffffff",
                                         font=("Microsoft YaHei", 12, "bold"))
        self.lbl_title_status.pack(side="left")

        # --- 扫描设置（可折叠：点整行展开/收起，默认展开）---
        self.frm_scan_box, frm_scan_head_right, frm_settings = self._make_collapsible(
            root, "扫描设置", 1, expanded=True, pady=(8, 4))
        self.frm_scan_inner = frm_settings

        # 路径选择 + 文件类型过滤（同一行：扫描路径 → 打开路径 → 文件类型）
        frm_path = ttk.Frame(frm_settings, padding=(2, 2, 2, 6))
        frm_path.pack(fill="x")
        frm_path.columnconfigure(1, weight=1)   # 扫描路径输入框可拉伸
        ttk.Label(frm_path, text="扫描路径：").grid(row=0, column=0, sticky="w")
        self.path_var = tk.StringVar(value=self.state.get("scan_root", ""))
        # 起始年月（历史回填起点，显示在主界面「历史回填」按钮右侧供设定）
        self.start_year_var = tk.StringVar(value=str(self.state.get("start_year", 2025)))
        self.start_month_var = tk.StringVar(value="%02d" % self.state.get("start_month", 1))
        self.entry_path = ttk.Entry(frm_path, textvariable=self.path_var)
        self.entry_path.grid(row=0, column=1, sticky="ew", padx=(4, 4))
        self.btn_browse = ttk.Button(frm_path, text="浏览...", command=self._browse)
        self.btn_browse.grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.btn_open_path = ttk.Button(frm_path, text="打开路径", command=self._open_scan_path)
        self.btn_open_path.grid(row=0, column=3, sticky="w", padx=(0, 10))

        # 起始月份已移至「任务中心」的扫描页设定，主界面不再显示。

        # 间隔（置于「扫描设置」折叠标题栏同一排右侧、靠右对齐，运行中即时生效）
        ttk.Label(frm_scan_head_right, text="秒").pack(side="right", padx=(0, 6))
        self.poll_var = tk.StringVar(value=str(self.state.get("poll_interval", 20)))
        self.entry_poll = ttk.Entry(frm_scan_head_right, textvariable=self.poll_var, width=8)
        self.entry_poll.pack(side="right", padx=(2, 0))
        ttk.Label(frm_scan_head_right, text="间隔：").pack(side="right", padx=(8, 2))
        self.poll_var.trace_add("write", self._on_poll_change)

        # 第二行：产线 + 计算 + 设备清单
        frm_poll2 = ttk.Frame(frm_settings, padding=(2, 2, 2, 6))
        frm_poll2.pack(fill="x")
        ttk.Label(frm_poll2, text="产线:").pack(side="left")
        line_box = ttk.Frame(frm_poll2)
        line_box.pack(side="left", padx=(4, 2))
        _lines_cfg = self.state.get("scan_lines", [])
        self.line_vars = {}
        for _ln in ("E", "C", "D", "A"):
            _v = tk.BooleanVar(value=_ln in _lines_cfg)
            self.line_vars[_ln] = _v
            ttk.Checkbutton(line_box, text=_ln, variable=_v,
                            command=self._on_line_change_main).pack(side="left", padx=2)
        # 找到新增文件后是否立即计算指标
        self.compute_var = tk.BooleanVar(value=bool(self.state.get("compute_after_scan", True)))
        self.chk_compute = ttk.Checkbutton(
            frm_poll2, text="找到新增后算指标", variable=self.compute_var,
            command=self._on_compute_change)
        self.chk_compute.pack(side="left", padx=(14, 2))
        self.btn_devlist = ttk.Button(frm_poll2, text="设备清单…", command=self.open_device_list_dialog)
        self.btn_devlist.pack(side="left", padx=(14, 2))
        self.btn_backfill = ttk.Button(frm_poll2, text="历史回填", command=self._start_backfill_from_main)
        self.btn_backfill.pack(side="left", padx=(6, 2))
        # 历史回填起始年月（紧随历史回填按钮，供回填设定起始月）
        ttk.Label(frm_poll2, text="起始:").pack(side="left", padx=(12, 2))
        ttk.Spinbox(frm_poll2, from_=2000, to=2100, width=6,
                    textvariable=self.start_year_var, wrap=True).pack(side="left", padx=(0, 2))
        ttk.Label(frm_poll2, text="年").pack(side="left")
        ttk.Spinbox(frm_poll2, from_=1, to=12, width=4,
                    textvariable=self.start_month_var, wrap=True, format="%02.0f").pack(side="left", padx=(2, 2))
        ttk.Label(frm_poll2, text="月").pack(side="left")

        # 第三行：跨月兜底 + 线程数量（运行中即时生效）
        frm_workers = ttk.Frame(frm_settings, padding=(2, 2, 2, 2))
        frm_workers.pack(fill="x")
        # 跨越兜底（线程数量同一行前面，运行中即时生效）
        ttk.Label(frm_workers, text="跨月兜底：").pack(side="left", padx=(0, 2))
        self.prev_hours_var = tk.StringVar(value=str(self.state.get("prev_scan_hours", 6)))
        self.entry_prev_hours = ttk.Entry(frm_workers, textvariable=self.prev_hours_var, width=5)
        self.entry_prev_hours.pack(side="left", padx=(4, 2))
        ttk.Label(frm_workers, text="小时").pack(side="left", padx=(0, 10))
        self.prev_hours_var.trace_add("write", self._on_prev_hours_change)
        ttk.Label(frm_workers, text="并行数：").pack(side="left")
        self.workers_var = tk.StringVar(value=str(self.state.get("compute_workers", 4)))
        self.spin_workers = ttk.Spinbox(frm_workers, from_=1, to=32, width=4,
                                        textvariable=self.workers_var)
        self.spin_workers.pack(side="left", padx=(4, 2))
        self.workers_var.trace_add("write", self._on_workers_change)
        ttk.Label(frm_workers, text="方式：").pack(side="left", padx=(10, 2))
        # 显示用中文，内部映射到 thread(串行)/process(多进程)
        _mode_zh = "多进程" if self.state.get("compute_mode", "process") == "process" else "串行"
        self.compute_mode_var = tk.StringVar(value=_mode_zh)
        self.combo_mode = ttk.Combobox(frm_workers, state="readonly", width=7,
                                       values=("串行", "多进程"),
                                       textvariable=self.compute_mode_var)
        self.combo_mode.pack(side="left")
        self.compute_mode_var.trace_add("write", self._on_compute_mode_change)

        # --- 控制按钮：开始/暂停/停止负责普通轮询扫描；历史回填在扫描设置区单独按钮 ---
        frm_btn = ttk.Frame(root, padding=(8, 4))
        frm_btn.grid(row=3, column=0, sticky="ew")
        self.btn_start = ttk.Button(frm_btn, text="开始", command=self.start_scan,
                                    style="Start.TButton")
        self.btn_pause = ttk.Button(frm_btn, text="暂停", command=self.pause_scan,
                                    style="Pause.TButton", state="disabled")
        self.btn_stop = ttk.Button(frm_btn, text="停止", command=self.stop_scan,
                                   style="Stop.TButton", state="disabled")
        self.btn_reset = ttk.Button(frm_btn, text="重置状态", command=self.reset_state)
        self.btn_view = ttk.Button(frm_btn, text="查看数据库", command=self.open_db_view)
        self.btn_tasks = ttk.Button(frm_btn, text="任务中心", command=self.open_task_center)
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_pause.grid(row=0, column=1, padx=(0, 6))
        self.btn_stop.grid(row=0, column=2, padx=(0, 6))
        self.btn_reset.grid(row=0, column=3, padx=(0, 6))
        self.btn_view.grid(row=0, column=4, padx=(0, 6))
        self.btn_tasks.grid(row=0, column=5, padx=(0, 6))
        frm_btn.columnconfigure(6, weight=1)  # 说明列吸收右侧多余空白，按钮区左对齐更整齐

        # --- 主区域：上方=当前扫描行，下方=可拖动分隔的左右面板 ---
        frm_main = ttk.Frame(root, padding=(8, 4, 8, 4))
        frm_main.grid(row=4, column=0, sticky="nsew")
        frm_main.columnconfigure(0, weight=1)
        frm_main.rowconfigure(2, weight=1)

        self.cur_var = tk.StringVar(value="（无）")
        ttk.Label(frm_main, text="当前扫描：").grid(row=0, column=0, sticky="w")
        self.lbl_cur = ttk.Label(frm_main, textvariable=self.cur_var, foreground="#0078D7",
                                 wraplength=880, justify="left")
        self.lbl_cur.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        # 路径较长时按容器实际宽度自动换行，避免固定 880 在缩放/缩窗时错位
        self.lbl_cur.bind("<Configure>",
                          lambda e: self.lbl_cur.configure(wraplength=max(120, e.width - 8)))

        # 左右可拖动分隔的面板（PanedWindow 提供 sash）
        pw = ttk.PanedWindow(frm_main, orient="horizontal")
        pw.grid(row=2, column=0, sticky="nsew")

        # 左：各文件夹文件统计
        frm_folders = ttk.LabelFrame(pw, text="各文件夹文件统计", padding=(4, 4, 4, 4))
        frm_folders.rowconfigure(0, weight=1)
        frm_folders.columnconfigure(0, weight=1)
        self.folder_list = tk.Listbox(frm_folders, width=64, font=("Microsoft YaHei", 8))
        self.folder_list.grid(row=0, column=0, sticky="nsew")
        sb_f = ttk.Scrollbar(frm_folders, orient="vertical", command=self.folder_list.yview)
        sb_f.grid(row=0, column=1, sticky="ns")
        self.folder_list.config(yscrollcommand=sb_f.set)

        # 右：事件 / 日志（诊断、错误、重置等仍保留）
        frm_log = ttk.LabelFrame(pw, text="事件日志", padding=(4, 4, 4, 4))
        frm_log.rowconfigure(0, weight=1)
        frm_log.columnconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(frm_log, wrap="word", state="disabled",
                                                  font=("Microsoft YaHei", 8))
        self.log_text.grid(row=0, column=0, sticky="nsew")

        pw.add(frm_folders, weight=3)
        pw.add(frm_log, weight=2)

        # --- 状态栏 ---
        frm_stat = ttk.Frame(root, padding=(8, 6, 8, 8))
        frm_stat.grid(row=5, column=0, sticky="ew")
        for i in range(6):
            frm_stat.columnconfigure(i, weight=1 if i in (1, 3, 5) else 0)
        ttk.Label(frm_stat, text="状态：").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="空闲")
        self.lbl_status = ttk.Label(frm_stat, textvariable=self.status_var, foreground=ui_common.COLOR_OK)
        self.lbl_status.grid(row=0, column=1, sticky="w")

        ttk.Label(frm_stat, text="本轮候选：").grid(row=0, column=2, sticky="w")
        self.cand_var = tk.StringVar(value="0")
        ttk.Label(frm_stat, textvariable=self.cand_var).grid(row=0, column=3, sticky="w")

        ttk.Label(frm_stat, text="已处理(本轮)：").grid(row=0, column=4, sticky="w")
        self.done_var = tk.StringVar(value="0")
        ttk.Label(frm_stat, textvariable=self.done_var).grid(row=0, column=5, sticky="w")

        ttk.Label(frm_stat, text="上次扫描：").grid(row=1, column=0, sticky="w")
        self.last_var = tk.StringVar(value=self._fmt_time(self.state.get("last_scan_time", 0)))
        ttk.Label(frm_stat, textvariable=self.last_var).grid(row=1, column=1, sticky="w")

        # --- 进度条（独立一行，避免压住状态栏文字）---
        self.progress = ttk.Progressbar(root, orient="horizontal", mode="determinate", maximum=100)
        self.progress.grid(row=6, column=0, sticky="ew", padx=8, pady=(0, 6))

    def _need_prev_scan(self, cur_ym):
        """是否需要在月初窗口内带扫上月。
        窗口 = 本月起前 prev_scan_hours 小时（默认 6）；窗口内存在「本月目录未确认」的监控设备才带扫。
        只看「实际监控的设备」：device_list 已为首次嗅探得到的固定全集（按产线筛选取交集精确判断，
        与确认块口径一致）；device_list 为空时（尚未嗅探）保守返回 True，避免漏扫上月。"""
        now = time.localtime()
        hours_into_month = now.tm_hour + 24 * (now.tm_mday - 1)
        if hours_into_month > self.state.get("prev_scan_hours", 6):
            return False  # 窗口外：本月无生产的设备上月也不会有新文件，自然停扫
        return True  # 窗口内：带扫上月（逐设备确认在 _run 中按 _prev_confirmed 剔除已确认设备）

    def _refresh_from_state(self):
        self.last_var.set(self._fmt_time(self.state.get("last_scan_time", 0)))
        devs = self.state.get("device_list") or []
        line = self.state.get("scan_lines", [])
        # 标题栏设备数：产线写前面，只显示选定设备台数；未选产线时退回显示全量设备清单
        if line:
            devs = [d for d in devs if analysis.classify_line(d) in line]
        dev_txt = (" · 选定设备%d台" % len(devs)) if line else (" · 设备清单%d台" % len(self.state.get("device_list") or []))
        line_txt = ("产线%s · " % "/".join(line)) if line else ""
        # 月初窗口内存在「未确认」设备时会顺带扫上月，标题栏如实反映，避免“仅当前月”误导
        _cur_ym = "%04d%02d" % (time.localtime().tm_year, time.localtime().tm_mon)
        self.mode_var.set("监控中：本月"
                          + ("（含上月兜底·逐设备确认）" if self._need_prev_scan(_cur_ym) else "")
                          + line_txt + dev_txt)
        if self.thread and self.thread.is_alive():
            self._set_title(self.mode_var.get(), "run")
        else:
            self._set_title("空闲", "idle")

    def _set_title(self, text, kind="idle"):
        """更新标题栏状态文字与颜色。kind: idle/run/pause/stop。
        统一加状态图标前缀，保证三态格式一致：运行▶ / 暂停⏸ / 停止⏹ / 空闲●。"""
        _icon = {"run": "▶ ", "pause": "⏸ ", "stop": "⏹ ", "idle": "● "}.get(kind, "")
        self.title_status_var.set(_icon + text)
        # 统一蓝底白字，不再用彩色高亮块；字号已在创建处加大加粗。
        self.lbl_title_status.config(foreground="#ffffff", background="#0078D7")

    def _on_line_change_main(self):
        selected = [ln for ln, v in self.line_vars.items() if v.get()]
        self.state["scan_lines"] = selected
        self._save_config()
        self._refresh_from_state()

    def _on_compute_change(self):
        self.state["compute_after_scan"] = bool(self.compute_var.get())
        self._save_config()  # 立即持久化，后台线程每轮从 state 读取最新值

    def _on_poll_change(self, *a):
        # 合法值即时写入 state（后台每轮读取，下轮生效）；非法/中间状态（空、非数字）静默跳过
        try:
            poll = int(self.poll_var.get().strip())
        except ValueError:
            return
        if poll <= 0:
            return
        self.state["poll_interval"] = poll
        self._save_config()

    def _on_prev_hours_change(self, *a):
        # 合法值即时写入 state（后台每轮经 _need_prev_scan 读取，下轮生效）；非法/中间状态静默跳过
        try:
            hours = int(self.prev_hours_var.get().strip())
        except ValueError:
            return
        if hours <= 0:
            return
        self.state["prev_scan_hours"] = hours
        self._save_config()

    def _on_workers_change(self, *a):
        # 合法值即时写入 state（历史回填/重算/补算启动时读取）；非法/中间状态静默跳过
        try:
            n = int(self.workers_var.get().strip())
        except ValueError:
            return
        if n < 1 or n > 32:
            return
        self.state["compute_workers"] = n

    def _on_compute_mode_change(self, *a):
        # 并行方式即时写入 state：UI 显示中文，映射为 thread=串行 / process=多进程（绕开 GIL 提速）
        zh = self.compute_mode_var.get()
        mode = "process" if zh == "多进程" else "thread"
        if self.state.get("compute_mode") != mode:
            self.state["compute_mode"] = mode
            self._save_config()
        self._save_config()

    def _get_compute_workers(self):
        """读取当前配置的计算并行线程数（非法值回落默认 4）。"""
        try:
            return max(1, min(32, int(self.state.get("compute_workers", 4))))
        except (TypeError, ValueError):
            return 4

    def _set_path_settings_enabled(self, enabled):
        """运行中锁定「开始才生效」的控件（路径/浏览）。
        产线、算指标、设备清单、导出区均即时生效，保持可用。"""
        state = "normal" if enabled else "disabled"
        for w in (self.entry_path, self.btn_browse, self.btn_open_path):
            w.config(state=state)
        self._refresh_poll_state(enabled)  # 扫描开始/结束同时刷新「间隔」可用性

    def _refresh_poll_state(self, enabled=True):
        """扫描设置「间隔（秒）」输入：扫描进行中锁定，否则可改。"""
        self.entry_poll.config(state="normal" if enabled else "disabled")

    def _dlg_parent(self):
        """返回当前激活的顶层窗口作为弹窗父窗口：
        任务中心等 Toplevel 被聚焦时锚定它，否则回退主界面 root，
        使弹窗居中于正在操作的界面（而非屏幕/主界面）。"""
        try:
            f = self.root.focus_get()
            while f is not None and not isinstance(f, (tk.Tk, tk.Toplevel)):
                f = f.master
            if f is not None and f.winfo_viewable():
                return f
        except Exception:
            pass
        return self.root


    # ---------- 控件回调 ----------
    def _browse(self):
        d = filedialog.askdirectory(parent=self._dlg_parent(),
                                    initialdir=self.path_var.get() or os.path.expanduser("~"))
        if d:
            self.path_var.set(d)
            self.state["scan_root"] = d
            self._save_config()

    def _open_scan_path(self):
        """在系统文件管理器中打开当前扫描路径（不存在则提示）。"""
        p = self.path_var.get().strip()
        if not p:
            messagebox.showinfo("打开路径", "请先设置扫描路径。", parent=self._dlg_parent())
            return
        if os.path.isfile(p):
            p = os.path.dirname(p)
        if not os.path.isdir(p):
            messagebox.showerror("打开路径", "路径不存在：\n%s" % p, parent=self._dlg_parent())
            return
        try:
            os.startfile(p)
        except Exception as e:
            messagebox.showerror("打开路径", "无法打开目录：%s" % e, parent=self._dlg_parent())

    def start_scan(self):
        path = self.path_var.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showerror("路径无效", "请先选择有效的扫描路径。", parent=self._dlg_parent())
            return
        # 读取并校验起始月份
        try:
            sy = int(self.start_year_var.get().strip())
            sm = int(self.start_month_var.get().strip())
        except ValueError:
            messagebox.showerror("起始月份无效", "起始年/月必须是数字。", parent=self._dlg_parent())
            return
        if not (1 <= sm <= 12):
            messagebox.showerror("起始月份无效", "月份必须在 1–12 之间。", parent=self._dlg_parent())
            return
        self.state["start_year"] = sy
        self.state["start_month"] = sm
        self._save_config()  # 立即持久化，保证后台线程读到最新设置
        # 刷新模式提示（文字稍后在校验完轮询/产线后再定，以如实反映上月确认状态）
        line = self.state.get("scan_lines", [])
        line_txt = (" · 产线%s" % "/".join(line)) if line else ""
        # 读取并校验轮询间隔
        try:
            poll = int(self.poll_var.get().strip())
        except ValueError:
            messagebox.showerror("轮询间隔无效", "轮询间隔必须是正整数（秒）。", parent=self._dlg_parent())
            return
        if poll <= 0:
            messagebox.showerror("轮询间隔无效", "轮询间隔必须大于 0 秒。", parent=self._dlg_parent())
            return
        self.state["poll_interval"] = poll
        self._save_config()  # 立即持久化，后台线程每轮从 state 读取最新间隔
        # 读取并校验跨月兜底窗口（小时）
        try:
            prev_hours = int(self.prev_hours_var.get().strip())
        except ValueError:
            messagebox.showerror("跨月兜底无效", "跨月兜底窗口必须是正整数（小时）。", parent=self._dlg_parent())
            return
        if prev_hours <= 0:
            messagebox.showerror("跨月兜底无效", "跨月兜底窗口必须大于 0 小时。", parent=self._dlg_parent())
            return
        self.state["prev_scan_hours"] = prev_hours
        self._save_config()
        # 读取并持久化产线筛选（复选）
        self.state["scan_lines"] = [ln for ln, v in self.line_vars.items() if v.get()]
        self._save_config()
        # 设备清单须手动嗅探（对话框「从扫描路径嗅探」）填充；为空则不扫（无目标目录）
        if not self.state.get("device_list"):
            self.q.put(("log", "⚠ 设备清单为空，请先在「设备清单」中手动嗅探或从扫描路径嗅探后再开始扫描。"))
            messagebox.showwarning("设备清单为空", "设备清单为空，请先在「设备清单」中手动嗅探后再开始扫描。", parent=self._dlg_parent())
            return
        # 月初窗口内存在未确认设备时会顺带扫上月，标题栏如实反映，避免“仅当前月”误导
        line = self.state.get("scan_lines", [])
        line_txt = (" · 产线%s" % "/".join(line)) if line else ""
        devs = self.state.get("device_list") or []
        # 标题栏设备数按当前产线筛选显示实际要扫的设备，而非嗅探全量
        if line:
            devs = [d for d in devs if analysis.classify_line(d) in line]
        dev_txt = (" · 选定设备%d台" % len(devs)) if line else (" · 设备清单%d台" % len(self.state.get("device_list") or []))
        _cur_ym = "%04d%02d" % (time.localtime().tm_year, time.localtime().tm_mon)
        self.mode_var.set("监控中：本月"
                          + ("（含上月兜底·逐设备确认）" if self._need_prev_scan(_cur_ym) else "")
                          + line_txt + dev_txt)
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.pause_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal", text="暂停")
        self.btn_stop.config(state="normal")
        self._set_path_settings_enabled(False)  # 运行中锁定「开始才生效」的控件
        self.status_var.set("扫描中")
        self.progress["value"] = 0
        self.thread = threading.Thread(target=self._run, args=(path, False), daemon=True)
        self.thread.start()
        self._set_title(self.mode_var.get(), "run")
        self.lbl_status.config(foreground=ui_common.COLOR_OK)

    def _start_backfill_from_main(self):
        """主界面「历史回填」按钮：使用主界面起始年月、产线与全部已保存设备，
        一次性扫描 起始月→当前月。日志与统计结果直接输出到主界面中间区（参照轮询）。"""
        sy = self.start_year_var.get().strip()
        sm = self.start_month_var.get().strip()
        if not messagebox.askyesno(
                "确认历史回填",
                "将从 %s 年 %s 月起，按已保存设备回填各月备份文件。\n\n是否开始历史回填？"
                % (sy, sm), parent=self.root):
            return
        self.history_backfill()

    def history_backfill(self):
        """历史回填：一次性扫描 起始月→当前月 设备备份目录（不轮询）。
        起始年月与产线取主界面设置区当前值；设备用全部已保存设备。"""
        path = self.path_var.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showerror("路径无效", "请先选择有效的扫描路径。", parent=self._dlg_parent())
            return
        try:
            sy = int(self.start_year_var.get().strip())
        except ValueError:
            messagebox.showerror("起始月份无效", "起始年必须是数字。", parent=self._dlg_parent())
            return
        try:
            sm = int(self.start_month_var.get().strip())
        except ValueError:
            messagebox.showerror("起始月份无效", "起始月必须是数字。", parent=self._dlg_parent())
            return
        if not (1 <= sm <= 12):
            messagebox.showerror("起始月份无效", "月份必须在 1–12 之间。", parent=self._dlg_parent())
            return
        lines = [ln for ln, v in self.line_vars.items() if v.get()]
        self.state["start_year"] = sy
        self.state["start_month"] = sm
        self.state["scan_lines"] = sorted(lines)
        self._save_config()
        line = self.state.get("scan_lines", [])
        line_txt = (" · 产线%s" % "/".join(line)) if line else ""
        self.mode_var.set("本轮范围：历史回填 %04d%02d→当前月" % (sy, sm) + line_txt)
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.pause_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="disabled")  # 一次性任务，不支持暂停
        self.btn_stop.config(state="normal")
        self._set_path_settings_enabled(False)  # 回填中锁定「开始才生效」的控件
        self.status_var.set("历史回填中")
        self.progress["value"] = 0
        self.thread = threading.Thread(target=self._run, args=(path, True), daemon=True)
        self.thread.start()
        self._set_title(self.mode_var.get(), "run")

    def pause_scan(self):
        if not self.thread or not self.thread.is_alive():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.btn_pause.config(text="暂停")
            self.status_var.set("扫描中")
            self._set_title(self.mode_var.get(), "run")
            self.lbl_status.config(foreground=ui_common.COLOR_OK)
        else:
            self.pause_event.set()
            self.btn_pause.config(text="继续")
            self.status_var.set("已暂停")
            self._set_title("已暂停", "pause")
            self.lbl_status.config(foreground="#b56800")

    def stop_scan(self):
        if not self.thread or not self.thread.is_alive():
            return
        self.stop_event.set()
        self.pause_event.clear()  # 解除暂停以便线程能检查 stop
        self.status_var.set("正在停止...")
        self._set_title("正在停止...", "stop")
        self.lbl_status.config(foreground=ui_common.COLOR_ERR)

    def reset_state(self):
        """清空已处理记录（保留扫描路径）。下次「开始」将全量重新扫描。"""
        if self.thread and self.thread.is_alive():
            messagebox.showwarning("请先停止", "扫描进行中，请先「停止」再重置状态。", parent=self._dlg_parent())
            return
        n = self.conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        self.processed_set = set()
        self.pending_rows = []
        reset_processed_db(self.conn, self.db_lock)
        save_config(self.state, self.db_lock)   # 持久化清空后的 mtime 缓存
        self.state["last_scan_time"] = 0
        self._save_config()
        self.last_var.set(self._fmt_time(0))
        self.cand_var.set("0")
        self.done_var.set("0")
        self.progress["value"] = 0
        self.status_var.set("状态已重置")
        self.lbl_status.config(foreground=ui_common.COLOR_OK)
        sy = self.state.get("start_year", 2025)
        sm = self.state.get("start_month", 1)
        self.mode_var.set("监控中：每轮仅当前月")
        self._set_title("空闲", "idle")
        self.folder_list.delete(0, tk.END)
        self._append_log("↺ 已重置状态（清除了 %d 条已记录文件），下次开始将从 %04d-%02d 起历史回填。"
                         % (n, sy, sm))

    def open_db_view(self):
        """打开数据库文件查看器（内嵌 Toplevel 窗口，只读查看 scan_state.db）。"""
        try:
            from db_view import open_db_view as _open
        except Exception as e:
            messagebox.showerror("无法打开查看器", "导入 db_view 失败：%s" % e, parent=self._dlg_parent())
            return
        try:
            _open(self.root)
        except Exception as e:
            messagebox.showerror("无法打开查看器", str(e), parent=self._dlg_parent())

    def _use_process(self):
        """是否启用多进程计算（state['compute_mode']=='process'）。默认 thread=串行。"""
        return self.state.get("compute_mode", "process") == "process"

    def _compute_metrics_batch(self, paths, stop_event=None):
        """本轮扫描结束后，对本轮新增的文件统一批量计算指标并落库（在线实时口径）。
        已移除多线程路径：compute_mode=process 时用多进程（绕开 GIL 真并行，大批量回填/重算
        显著提速）；否则串行（监控小批量够用，且不养子进程、内存最省）。落库仍单线程按批
        （每 1000 个）一次性提交。stop_event 置位时停止取后续结果，已算出的部分照常落库。"""
        if not paths:
            return
        _BATCH = 1000
        buf = []
        total = len(paths)
        done_cnt = 0
        self.q.put(("status", "计算指标中…"))
        self.q.put(("status_color", ui_common.COLOR_OK))
        # 计算指标时不再扫描文件夹，「当前扫描」框应离开『上一个文件夹』以免误以为还在扫那个目录
        self.q.put(("current", "计算指标中…（%d 个文件）" % total))
        mode = "多进程" if self._use_process() else "串行"
        for p, res in analysis.compute_paths(
                paths, self._get_compute_workers(), stop_event,
                use_process=self._use_process()):
            if stop_event is not None and stop_event.is_set():
                break  # 用户停止：已算出的 buf 照常落库，剩余文件由『补算缺失指标』兜底
            buf.append((p, res))
            done_cnt += 1
            if len(buf) >= _BATCH:
                analysis.upsert_batch(self.conn, self.db_lock, buf)
                buf = []
                # 按批回传进度（每批刷新一次，避免逐文件刷 UI）
                self.q.put(("status", "计算指标中…[%s] %d/%d" % (mode, done_cnt, total)))
        if buf:
            analysis.upsert_batch(self.conn, self.db_lock, buf)
            done_cnt = min(done_cnt, total)
            self.q.put(("status", "计算指标中…[%s] %d/%d" % (mode, done_cnt, total)))

    def _flush_pending(self):
        """把当前累积的 pending_rows 落库并计算指标（分批落库的落库动作）。

        与 _scan_cycle 内每满 _FLUSH_BATCH 触发的调用共用；外层 _run 在整轮结束后
        再兜底一次，处理末尾不足一批的残留。这样 pending_rows 不再整轮堆叠，历史
        回填的内存被锁死在每批 _FLUSH_BATCH 的量级，而非随文件数线性增长。"""
        if not self.pending_rows:
            return
        insert_processed(self.conn, self.pending_rows, self.db_lock)
        if self.state.get("compute_after_scan", True):
            self._compute_metrics_batch([r[0] for r in self.pending_rows], self.stop_event)
        else:
            self.q.put(("log", "✔ 已记录 %d 个新增文件（已跳过指标计算，按需在『重新计算指标』中手动计算）。"
                         % len(self.pending_rows)))
        self.pending_rows = []

    def open_task_center(self):
        """打开「任务中心」：重新计算指标 / 补算缺失指标 / CSV 导出（手动+自动）
        整合为一个多标签二级界面，复用产线/设备/月份筛选逻辑。
        扫描运行中也可打开（重算/补算启动时会校验并拦截；导出用只读子进程不冲突）。"""
        TaskCenterDialog(self.root, self.conn, self.db_lock, self)

    def open_device_list_dialog(self):
        """打开「设备清单」对话框：配置设备号清单（需手动点「从扫描路径嗅探」填充）。"""
        DeviceListDialog(self.root, self)

    # ---------- 后台监控（持续循环：每 POLL_INTERVAL 扫描一轮，直到点停止） ----------
    def _run(self, root_path, backfill=False):
        # 目录发现统一用 device_list 直接拼路径（见 discover_backup_targets），不做每轮 listdir 自动发现；
        # 区别仅在 months 集合：正常监控仅当前月（窗口内未确认设备顺带上月），回填则起始月→当前月全量。
        # months 在循环内每轮按当前月重算，从而跨月时自动切到新月份目录，无需重启（设备清单需手动嗅探更新）。
        start_year = self.state.get("start_year", 2025)
        start_month = self.state.get("start_month", 1)
        first_cycle = True
        enumerated_dirs = 0   # 本轮实际枚举目录数（无匹配目录时保持 0）
        skip_cnt = 0          # 本轮被 mtime 跳过目录数
        changed_cnt = 0       # 本轮有变化/保守枚举的目录数（无匹配目录时保持 0）
        new_cnt = 0           # 本轮新出现目录数（无匹配目录时保持 0）
        missing_cnt = 0       # 本轮不存在/无目录数（无匹配目录时保持 0）

        self.q.put(("status", "监控中…"))
        self.q.put(("status_color", ui_common.COLOR_OK))

        while not self.stop_event.is_set():
            # 每轮直接用已持久化的设备清单拼路径（首次运行前已完成嗅探，不再每轮 listdir）
            cur_ym = "%04d%02d" % (time.localtime().tm_year, time.localtime().tm_mon)
            device_list = self.state.get("device_list") or []
            lines = self.state.get("scan_lines", [])
            if backfill:
                months = ym_range(start_year, start_month)
                need_prev = False
            else:
                months = [cur_ym]
                # 逐设备上月确认机制：仅在月初窗口（prev_scan_hours，默认本月起 6 小时）内，对「本月目录
                # 尚未确认」的设备顺带扫上月；本月目录出现当轮带扫上月作「最终确认」后仅扫本月；窗口外
                # 未确认的设备自然停扫上月（本月无生产，上月也不会有新文件）。标志持久化，跨重启不重复。
                need_prev = self._need_prev_scan(cur_ym)
                if need_prev:
                    pm = prev_ym(cur_ym)
                    if pm not in months:
                        months.append(pm)
            # 统计框【总是列出】的月份集合：正常监控=当前月+待确认上月；历史回填=全部回填月份。
            # 这样回填的各历史文件夹都会稳定显示累计/新增，而非仅在有新增时闪现。
            if backfill:
                current_months = set(months)
            else:
                current_months = {cur_ym}
                if need_prev:
                    current_months.add(prev_ym(cur_ym))
            # 设备清单必非空（首次运行已嗅探持久化）：直接用清单里的设备号拼路径，
            # 不做每轮 listdir 自动发现，省去每轮 D 次顶层 isdir；选产线时也不必再枚举全历史目录。
            raw_targets = discover_backup_targets(
                root_path, months, device_list=device_list, lines=lines)  # 直接按设备清单拼接目录（不探测存在性）
            targets = raw_targets
            device_mode = bool(targets)
            # 逐设备剔除「已确认」设备的上月目录：本月目录总是保留，上月目录仅保留
            # 「未确认」设备的（仍需带扫上月兜底/确认）。backfill 全历史回填不走此过滤。
            if not backfill and need_prev and targets:
                _pm = prev_ym(cur_ym)
                _conf = self._prev_confirmed
                targets = [t for t in targets
                           if _dev_ym(t)[1] != _pm
                           or _conf.get(_dev_ym(t)[0]) != cur_ym]
                device_mode = bool(targets)
            matched_devs = sorted({_dev_ym(t)[0] for t in targets}) if lines else []
            if first_cycle:
                if device_mode:
                    _pm = prev_ym(cur_ym)
                    _cur_cnt = sum(1 for t in targets if _dev_ym(t)[1] == cur_ym)
                    _prev_cnt = sum(1 for t in targets if _dev_ym(t)[1] == _pm)
                    line_tag = (" · 按产线 %s 命中设备：%s"
                                % ("/".join(lines), "、".join(matched_devs))) if lines else ""
                    if backfill:
                        self.q.put(("log", "历史回填：从 %04d%02d 到当前月，跨 %d 个月、%d 个设备目录。%s"
                                     % (start_year, start_month, len(months), len(targets), line_tag)))
                    else:
                        if need_prev and _prev_cnt > 0:
                            extra = "（含上月兜底 %d 个 · 逐设备确认）" % _prev_cnt
                        elif need_prev and _prev_cnt == 0:
                            extra = "（上月已确认 · 仅扫本月）"
                        else:
                            extra = ""
                        self.q.put(("log", "轮询当前月 %d 个设备目录%s%s。"
                                     % (_cur_cnt, extra, line_tag)))
                elif device_list:
                    # 设备清单非空（结构认得出），但当前【产线/设备清单】筛选下本月无匹配目录
                    flt = ("产线 %s" % "/".join(lines)) if lines else "设备清单"
                    self.q.put(("log", "已识别设备备份结构，但【%s】筛选下本月无匹配目录，本轮跳过扫描。"
                                 % flt))
                else:
                    # 设备清单为空（未配置/丢失设备号），与“目录结构是否真实存在”无关
                    self.q.put(("log", "设备清单为空（未配置设备号），本轮跳过扫描。"
                                      "请先在【设置】中嗅探或填写设备清单。"))
            first_cycle = False

            # 重置本轮统计（候选/已处理/进度统一归零，避免无匹配目录时残留上一轮数据）
            self.q.put(("cand", 0))
            self.q.put(("done_count", 0))
            self.q.put(("progress", 0))
            # 有匹配目录时只扫命中的设备目录；无匹配时本轮跳过扫描（不回退递归全量扫描）
            if targets:
                # 窗口化重建去重集：只装「本轮目标目录」下的已处理路径，内存恒定当月
                # 量级（约 30~60 万条），而非全量载入全年记录。历史目录本轮不扫，无需其去重信息。
                self.processed_set = load_processed_set_for_targets(
                    self.conn, targets, self.db_lock)
                # 监控阶段用 mtime 优先；回填阶段始终全量。全量兜底已由「月初 6 小时
                # 窗口内逐设备上月兜底/确认」承担（prev_scan_hours），此处不再额外强制全量。
                force_full = False
                scanned, total, done, discovery_error, mstats = self._scan_cycle(
                    targets, root_path,
                    backfill=backfill, force_full=force_full)
                enumerated_dirs, skip_cnt, changed_cnt, new_cnt, missing_cnt = mstats
            else:
                scanned, total, done, discovery_error = 0, 0, 0, None
                # 跳过扫描时也刷新统计面板，避免残留上一产线/上一轮的数据
                self.q.put(("folders", {}))

            # 逐设备上月最终确认（仅正常监控；backfill 不走）：
            # 本月目录出现当轮已带扫上月 → 该设备确认（_prev_confirmed 置当月，内存态），之后仅扫本月；
            # 窗口外未出现的设备由 _need_prev_scan 自然停扫上月，无需额外记录。
            # 发现阶段出错或被中途停止时不更新，留待下轮重试，避免漏扫。
            if (not backfill and need_prev and targets and discovery_error is None
                    and not self.stop_event.is_set()):
                _pm = prev_ym(cur_ym)
                _conf = self._prev_confirmed
                _seen = {_dev_ym(t)[0] for t in targets}
                newly_confirmed = []
                for dev in sorted(_seen):
                    if _conf.get(dev) == cur_ym:
                        continue  # 已确认，跳过
                    dev_cur = any(_dev_ym(t) == (dev, cur_ym) for t in targets)
                    if dev_cur:
                        _conf[dev] = cur_ym
                        newly_confirmed.append(dev)
                if newly_confirmed:
                    self.q.put(("log", "✔ 设备 %s 本月目录已出现，上月（%s）确认完毕，后续仅扫本月。"
                                 % ("、".join(newly_confirmed), _pm)))

            # 上报本轮结果 + 持久化（仅新增行落盘，不再全量重写）
            self.state["last_scan_time"] = time.time()
            if self.pending_rows:
                # 分批落库已由 _scan_cycle 内每满 _FLUSH_BATCH 触发；此处兜底处理末尾残留（不足一批的）。
                self._flush_pending()
            self._save_config()
            # mtime 监控摘要：每轮都报三类目录情况（新增候选数由上方「已枚举 N 个文件，新增候选 X 个」呈现）。
            # 恒等：总目录 == 跳过 + 无目录 + 枚举，方便核对三类统计是否守恒。
            if not backfill:
                self.q.put(("log", "总目录%d 跳过%d 无目录%d 枚举%d"
                                   % (len(targets), skip_cnt, missing_cnt, enumerated_dirs)))
            # 一轮正常完成（含计算指标）：清空「当前扫描」框，避免停留在『计算指标中…』或上一个文件夹
            self.q.put(("current", "（无）"))
            if discovery_error:
                self.q.put(("status", "出错（见上方日志）" + (" · 监控继续" if not backfill else "")))
                self.q.put(("status_color", "#c00"))
            elif total == 0:
                if not backfill and skip_cnt == len(targets) and len(targets) > 0:
                    self.q.put(("status", "本轮全部目录未变化，已跳过（轮询 0）· 监控中"))
                else:
                    self.q.put(("status", ("本轮完成（无新增）· 监控中" if not backfill else "历史回填完成（无新增）")))
                self.q.put(("status_color", ui_common.COLOR_OK))
            else:
                self.q.put(("status", ("本轮完成（处理 %d 个）· 监控中" % done) if not backfill
                            else "历史回填完成（处理 %d 个）" % done))
                self.q.put(("status_color", ui_common.COLOR_OK))

            if backfill:
                break  # 历史回填为一次性任务，跑完一轮即结束
            # 等待下一轮（响应暂停/停止）；期间被停止则返回 False 结束循环
            if not self._wait_until_next():
                break

        self._save_config()
        self.q.put(("status", "已停止" if not backfill else "历史回填完成"))
        self.q.put(("status_color", ui_common.COLOR_WARN if not backfill else ui_common.COLOR_OK))
        self.q.put(("finished", 1 if backfill else 0))

    def _scan_cycle(self, targets, root_path,
                    backfill=False, force_full=False):
        """执行【一轮】扫描：发现候选 + 逐个处理。
        targets: 设备备份目录列表（如 [{设备}/CPU1/BACKUP_YYYYMM, ...]）；为 None 时回退为
                对整个 root_path 递归全量扫描（通用模式）。
        backfill: True=历史回填，始终全量 scandir（不经 mtime 缓存）；False=监控阶段。
        force_full: 监控阶段为 True 时忽略 mtime 缓存、强制全量 scandir 一次兜底（防漏扫）。
        返回 (scanned, total, done, discovery_error, mstats)；
        mstats=(enumerated_dirs, skip_cnt, changed_cnt, new_cnt, missing_cnt)：
        监控阶段——enumerated_dirs=实际枚举目录数、skip_cnt=mtime 未变跳过数、
        changed_cnt=缓存有记录且 mtime 变化数、new_cnt=缓存无记录新出现数、
        missing_cnt=不存在/无目录数；
        回填/强制全量阶段枚举数=目录数、跳过数=0。
        恒等关系：len(targets) == enumerated_dirs + skip_cnt + missing_cnt（监控阶段）。"""
        candidates = []
        scanned = 0          # 实际枚举到的文件总数（用于诊断“为什么 0 候选”）
        discovery_error = None
        new_per_folder = {}  # 本次扫描新增：文件夹 -> 新增文件数
        # 统计列的“目录->文件数”仅统计本轮实际枚举到的文件（即 mtime 变化的目录），不跨轮累计。
        folder_counts: dict[str, int] = {}
        last_folder = None
        mtime_cache = self.state.setdefault("dir_mtime_cache", {})
        updated_mtime = {}    # 本轮实际枚举到的目录 -> 新 mtime（仅监控阶段回写缓存）
        skip_cnt = 0          # 监控阶段因 mtime 未变而跳过的目录数（诊断用）
        missing_cnt = 0       # 目录不存在/无目录（stat 失败）的目录数，单独计“无目录”
        enumerated_dirs = 0   # 实际枚举（未跳过）的目录数
        changed_dirs = []     # 缓存有记录且 mtime 变化（或 stat 失败保守枚举）的目录
        new_dirs = []         # 缓存无记录、首次出现（本轮才有的目录）
        # 扫描范围：targets 为目录列表时逐个目录扫；为 None 时递归整个 root_path
        scan_roots = targets if targets is not None else [root_path]
        missing_devs: set[tuple[str, str]] = set()   # 本轮不存在目标：(设备号, 年月)，去重合并一行日志
        for scan_root in scan_roots:
            # 监控阶段（非回填、非强制全量）：先 stat 取 mtime，与缓存比较，未变则跳过枚举。
            # mtime 优先：省去未变目录的 scandir 网络往返；回填/兜底轮不跳过。
            if not backfill and not force_full:
                m = _safe_stat(scan_root, os.path.getmtime, timeout=5.0)
                if m is False or m is None:
                    # 目录不存在 / 无目录（stat 失败）：单独计“无目录”，不写 mtime 缓存、不计入跳过
                    missing_cnt += 1
                    _d, _ym = _dev_ym(scan_root)
                    missing_devs.add((_d, _ym))  # 记设备号+年月，循环后合并一行输出
                    continue
                if m == mtime_cache.get(scan_root):
                    skip_cnt += 1
                    continue          # mtime 未变：跳过该目录的 scandir
                # mtime 变了 / 首次无缓存 → 计为变化或新目录，继续枚举
                updated_mtime[scan_root] = m
                if scan_root in mtime_cache:
                    changed_dirs.append(scan_root)   # 缓存有旧值且变化
                else:
                    new_dirs.append(scan_root)       # 缓存无记录：首轮或新出现目录
            # 目录存在性不再逐目录探测（直接按设备清单拼接路径）；不存在的目录在 iter_files
            # 内部 scandir 重试失败后会被捕获，这里仅记录日志并跳过本轮该目录的枚举。
            if scan_root in changed_dirs:
                self.q.put(("log", "目录变化（枚举）：%s" % scan_root))
            elif scan_root in new_dirs:
                self.q.put(("log", "新目录（枚举）：%s" % scan_root))
            enumerated_dirs += 1
            self.q.put(("current", "🔍 开始枚举: " + scan_root))
            try:
                for fpath, entry in iter_files(scan_root, retries=1,
                                                stop_event=self.stop_event):
                    if self.stop_event.is_set():
                        break
                    scanned += 1
                    # 仅当所在文件夹变化时更新“当前扫描”，避免逐文件刷屏
                    folder = os.path.dirname(fpath)
                    # 本轮枚举到的文件计入所在目录文件数（仅反映本轮 mtime 变化的目录）。
                    folder_counts[folder] = folder_counts.get(folder, 0) + 1
                    if folder != last_folder:
                        self.q.put(("current", folder))
                        last_folder = folder
                    if scanned % 500 == 0:
                        self.q.put(("status", "发现中… 已枚举 %d 个文件" % scanned))
                    # 仅凭文件名去重，不再调用 entry.stat()（省去每个文件的额外网络往返）
                    # candidates 为单元素元组 (fpath,)，size 改由「计算指标」阶段写入 metrics
                    if is_candidate(fpath, self.processed_set):
                        candidates.append((fpath,))
            except Exception as e:
                if getattr(e, "winerror", None) == 3 or "系统找不到" in str(e) or "No such file" in str(e):
                    enumerated_dirs -= 1   # 目录根本不存在，未真正枚举，不计入轮询数
                    missing_cnt += 1       # 计入“无目录”
                    _d, _ym = _dev_ym(scan_root)
                    missing_devs.add((_d, _ym))  # 记设备号+年月，循环后合并一行输出
                    # 目录不存在属确定失败（设备当月目录尚未生成），不视为致命错误，
                    # 否则会污染 discovery_error 并阻止其他目录的 mtime 缓存回写。
                else:
                    discovery_error = e
                    self.q.put(("log", "发现阶段出错：%s" % e))
                    self.q.put(("status_color", "#c00"))
            if self.stop_event.is_set():
                break
        # 目标不存在的（设备·年月）合并为一行输出，不逐个打印完整路径，且能定位到缺失月份
        if missing_devs:
            _tags = sorted("%s·%s" % (d, ym) for d, ym in missing_devs)
            self.q.put(("log", "目标不存在，本轮跳过：%s" % "、".join(_tags)))
        # 监控阶段：本轮回写“实际枚举成功”的目录 mtime（updated_mtime 已排除报错/不存在目录），
        # 跳过项沿用旧缓存，保持“未变”语义。与 discovery_error 解耦：个别目录不存在/报错
        # 不再阻止其他目录的 mtime 缓存建立，否则缓存永建不起来、mtime 跳过永远不生效。
        if not backfill:
            if not self.stop_event.is_set():
                mtime_cache.update(updated_mtime)
                self.state["dir_mtime_cache"] = mtime_cache
            if changed_dirs:
                self.q.put(("log", "%d 个目录有变化，已枚举。" % len(changed_dirs)))
            if new_dirs:
                self.q.put(("log", "%d 个新目录（原无记录），已枚举。" % len(new_dirs)))
        mstats = (enumerated_dirs, skip_cnt, len(changed_dirs), len(new_dirs), missing_cnt)

        total = len(candidates)
        self.q.put(("cand", total))
        # 目录不存在的失败已在循环内逐目录记过“目录不存在，本轮跳过”，这里不再重复汇总；
        # 仅当存在“非目录不存在”的异常时才汇总报出（保留真正错误的可见性）。
        if discovery_error and not (getattr(discovery_error, "winerror", None) == 3
                                    or "系统找不到" in str(discovery_error)
                                    or "No such file" in str(discovery_error)):
            self.q.put(("log", "✘ 发现阶段失败：%s" % discovery_error))
        else:
            self.q.put(("log", "已枚举 %d 个文件，新增候选 %d 个。" % (scanned, total)))
            self.q.put(("status", "处理中（候选 %d 个）" % total))

        # 逐个处理候选文件（只统计到文件夹粒度，不逐个显示）
        # 内存优化：不再整轮累积 pending_rows 到结束才落库，而是累积满 _FLUSH_BATCH 个就
        # 立即落库并算指标、清空。历史回填一轮可能命中几十万候选，分批可把内存锁死在
        # 每批量级，避免"随文件数线性增长"。candidates 仍整轮持有（供进度条预估 total），
        # 但每个处理完的项不再被引用，交由 GC 释放。
        done = 0
        last_folder = None
        for (fpath,) in candidates:
            if self.stop_event.is_set():
                break
            # 暂停处理
            while self.pause_event.is_set() and not self.stop_event.is_set():
                time.sleep(0.1)
            if self.stop_event.is_set():
                break

            folder = os.path.dirname(fpath)
            if folder != last_folder:
                self.q.put(("current", folder))
                last_folder = folder
            # 扫到的新文件名直接处理（不做写完判定，无额外网络 I/O）
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self.processed_set.add(fpath)                 # 内存集合：O(1) 去重
            self.pending_rows.append((fpath, ts))  # 待落盘（不取 size/mtime；size 在算指标时入 metrics）
            done += 1
            new_per_folder[folder] = new_per_folder.get(folder, 0) + 1
            self.q.put(("done_count", done))
            # 分批落库：累积满 _FLUSH_BATCH 个即插库 + 算指标 + 清空，避免整轮堆叠 pending_rows
            if len(self.pending_rows) >= _FLUSH_BATCH:
                self._flush_pending()
            # 进度
            if total > 0:
                self.q.put(("progress", int(done * 100 / total)))

        if total > 0 and not self.stop_event.is_set():
            self.q.put(("progress", 100))

        # 各文件夹【实际文件数】：仅本轮枚举到的（mtime 变化的目录）文件数。
        total_per_folder = folder_counts
        # 展示范围：仅本轮枚举到的目录；若本轮没有任何目录被枚举（全部 mtime 未变被跳过），
        # 则统计列为空，界面显示“本轮未轮询到文件夹”。
        folders_to_show = set(folder_counts.keys())
        summary = {d: (total_per_folder.get(d, 0), new_per_folder.get(d, 0))
                   for d in folders_to_show}
        self.q.put(("folders", summary))

        return scanned, total, done, discovery_error, mstats


    def _wait_until_next(self):
        """等待 POLL_INTERVAL 秒后进入下一轮；期间响应暂停（冻结倒计时）/停止。
        间隔取自 state['poll_interval']（UI 可设定，默认 20 秒）。
        返回 True=等待正常结束应继续下一轮；False=等待期间被停止。"""
        # 进入等待（含暂停）阶段：清空“当前扫描”，避免残留上一轮最后一个文件夹路径
        self.q.put(("current", "（无）"))
        remaining = self.state.get("poll_interval", POLL_INTERVAL)
        while remaining > 0 and not self.stop_event.is_set():
            if self.pause_event.is_set():
                self.q.put(("status", "已暂停（监控中，距下次扫描约 %d 秒）" % int(remaining)))
                time.sleep(0.5)
                continue
            step = min(0.5, remaining)
            time.sleep(step)
            remaining -= step
            mm, ss = divmod(int(remaining), 60)
            self.q.put(("status", "监控中 · 距下次扫描 %02d:%02d" % (mm, ss)))
        return not self.stop_event.is_set()

    # ---------- UI 轮询（线程安全更新） ----------
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "current":
                    self.cur_var.set(msg[1])
                elif kind == "folders":
                    self._set_folders(msg[1])
                elif kind == "done_count":
                    self.done_var.set(str(msg[1]))
                elif kind == "cand":
                    self.cand_var.set(str(msg[1]))
                elif kind == "progress":
                    self.progress["value"] = msg[1]
                elif kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "status_color":
                    self.lbl_status.config(foreground=msg[1])
                elif kind == "log":
                    self._append_log(msg[1])
                elif kind == "finished":
                    self.btn_start.config(state="normal")
                    self.btn_pause.config(state="disabled", text="暂停")
                    self.btn_stop.config(state="disabled")
                    self._set_path_settings_enabled(True)  # 结束/停止后恢复设置区可编辑
                    if msg[1]:  # 历史回填完成：绿字，与正文状态一致
                        self._set_title("历史回填完成", "stop")
                        self.lbl_status.config(foreground=ui_common.COLOR_OK)
                    else:  # 主动停止：红字
                        self._set_title("已停止", "stop")
                        self.lbl_status.config(foreground=ui_common.COLOR_ERR)
                    self.last_var.set(self._fmt_time(self.state.get("last_scan_time", 0)))
                    self.cur_var.set("（无）")
        except Empty:
            pass
        self.root.after(50, self._poll)

    def _append_log(self, text):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        line = "[%s] %s" % (ts, text)
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")
        # 限制长度
        lines = self.log_text.get("1.0", tk.END).count("\n")
        if lines > MAX_LOG_LINES:
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "%d.0" % (lines - MAX_LOG_LINES + 1))
            self.log_text.config(state="disabled")

    def _set_folders(self, summary):
        """summary: {folder: (total, new)}。列出【本轮实际轮询到的】文件夹（含新增 0 的），
        直接显示 设备号 · 月份 · 文件数 · 新增数。"""
        self.folder_list.delete(0, tk.END)
        rows = [(d, tot, nw) for d, (tot, nw) in summary.items()]
        if not rows:
            self.folder_list.insert(tk.END, "（本轮未轮询到文件夹）")
            return
        # 按 设备号、月份 排序展示
        def _key(item):
            dev, ym = _dev_ym(item[0])
            return (dev, ym, item[0])
        for folder, total, new in sorted(rows, key=_key):
            dev, ym = _dev_ym(folder)
            # 直接显示设备号与月份，去掉产线/文字前缀
            self.folder_list.insert(
                tk.END, "%s · %s · 文件 %d 个 · 新增 %d 个"
                % (dev or "?", ym or "?", total, new))
        self.folder_list.see(0)

    @staticmethod
    def _fmt_time(ts):
        if not ts:
            return "（无）"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


    # ---------- 通用可折叠面板（一次定义，点箭头按钮展开/收起） ----------
    def _make_collapsible(self, parent, title, row, expanded=True, pady=(4, 4)):
        """创建一个点击箭头按钮展开/收起的折叠区块。

        布局：
          box
           ├─ head      顶部标题行：箭头按钮 + 标题 + 右侧常驻控件区 head_right（始终可见）
           └─ inner     内容 LabelFrame（展开显示 / 收起隐藏）

        返回 (box, head_right, inner)：
          - box        外层容器（已 grid 到 parent 的 row）
          - head_right 标题栏右侧常驻区域（始终可见），供调用方追加常驻控件（如启用/间隔）
          - inner      放内容的 LabelFrame（默认已展开或收起）
        """
        box = ttk.Frame(parent, padding=(0, 0))
        box.grid(row=row, column=0, sticky="ew", padx=8, pady=pady)
        box.columnconfigure(0, weight=1)

        expanded_var = tk.BooleanVar(value=expanded)

        # 顶部标题行：箭头按钮（加粗样式）+ 标题 + 右侧常驻控件区，始终可见
        head = ttk.Frame(box)
        head.pack(fill="x")
        _collapse_style = ttk.Style()
        _collapse_style.configure("CollapseTitle.TButton",
                                  font=("Microsoft YaHei", 9, "bold"), anchor="w")
        btn = ttk.Button(head, text=("▼ " if expanded else "▶ ") + title,
                         style="CollapseTitle.TButton")
        btn.pack(side="left", fill="x", expand=True)
        head_right = ttk.Frame(head)
        head_right.pack(side="left")

        inner = ttk.LabelFrame(box, text=title, padding=(12, 6, 12, 10))
        inner.pack(fill="x", pady=(4, 0))
        inner.columnconfigure(0, weight=1)

        def _toggle():
            if expanded_var.get():
                inner.pack_forget()
                expanded_var.set(False)
                btn.config(text="▶ " + title)
            else:
                inner.pack(fill="x", pady=(4, 0))
                expanded_var.set(True)
                btn.config(text="▼ " + title)

        btn.config(command=_toggle)

        if not expanded:
            inner.pack_forget()
            btn.config(text="▶ " + title)

        return box, head_right, inner

    # ---------- CSV 自动导出（定时增量；配置 UI 在任务中心「CSV 导出」页） ----------

    def start_auto_export(self, silent=False):
        """按 state 中已保存的配置启动定时自动导出（UI 值由任务中心先写入 state）。
        silent=True 供程序启动时自动恢复，失败仅记日志不弹窗。"""
        outdir = self.state.get("csv_auto_outdir", "")
        if not outdir:
            if not silent:
                messagebox.showerror("目录无效", "请先设置自动导出目录。", parent=self._dlg_parent())
            return False
        try:
            interval = int(self.state.get("csv_auto_interval_min", 60))
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            if not silent:
                messagebox.showerror("间隔无效", "自动导出间隔必须大于 0 分钟。", parent=self._dlg_parent())
            return False
        if not os.path.isdir(outdir):
            try:
                os.makedirs(outdir, exist_ok=True)
            except Exception as e:
                if not silent:
                    messagebox.showerror("目录无效", "导出目录无法创建：%s" % e, parent=self._dlg_parent())
                return False
        if self.auto_stop is not None and not self.auto_stop.is_set():
            return True  # 已在运行
        self.auto_stop = threading.Event()
        self.auto_thread = threading.Thread(
            target=self._auto_loop, args=(interval * 60,), daemon=True)
        self.auto_thread.start()
        self.q.put(("log", "▶ 自动导出已启动：每 %d 分 → %s" % (interval, outdir)))
        return True

    def stop_auto_export(self):
        if self.auto_stop is not None:
            self.auto_stop.set()
        self.q.put(("log", "■ 自动导出已停止。"))

    def _auto_loop(self, interval):
        stop = self.auto_stop
        if stop is None:
            return
        self._auto_export_once(log=True)
        while not stop.wait(interval):
            self._auto_export_once(log=True)

    def _auto_export_once(self, log=False, manual=False):
        outdir = self.state.get("csv_auto_outdir", "")
        cols = self.state.get("csv_cols") or []
        if not outdir:
            if log:
                self.q.put(("log", "自动导出跳过：未设置导出目录。"))
            return
        import subprocess
        # 手动触发（全量导出一次）加 --all 强制导出全部现有月份；
        # 定时自动导出不带 --all，走增量裁决（只导有新增的月份）。
        cmd = [sys.executable, os.path.join(_HERE, "export_csv.py"),
               "--outdir", outdir]
        if manual:
            cmd.append("--all")
        cmd += ["--cols"] + cols
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if log:
                out = (res.stdout or res.stderr or "").strip()
                self.q.put(("log", "自动导出：" + (out or "完成")))
        except Exception as e:
            if log:
                self.q.put(("log", "自动导出失败：" + str(e)))


class DeviceListDialog(tk.Toplevel):
    """配置设备清单：每行一个设备号。首次运行已由扫描路径嗅探并持久化；
    清空后下一次开始扫描会从扫描路径重新嗅探一次。"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.withdraw()   # 先隐藏，避免默认位置闪现后再跳到居中
        self.app = app
        self.title("设备清单配置")
        self.geometry("480x560")
        self.resizable(True, True)   # 可缩放，并保留最大/最小化按钮（不再 transient）
        # 居中于主窗口
        self.update_idletasks()
        self.geometry("+%d+%d" % (
            parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2),
            parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)))
        self._build()
        self.deiconify()   # 定位完毕后再显示，消除闪烁
        self.lift()

    def _build(self):
        ttk.Label(
            self,
            text="设备清单（用逗号分隔，如 A01,B02,C03；为空时需点「从扫描路径嗅探」填充）").pack(
            anchor="w", padx=10, pady=(8, 2))
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=10, pady=2)
        self.text = tk.Text(frm, height=10, wrap="word", font=("Microsoft YaHei", 9))
        self.text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frm, command=self.text.yview)
        sb.pack(side="right", fill="y")
        self.text.config(yscrollcommand=sb.set)
        cur = self.app.state.get("device_list") or []
        if cur:
            self.text.insert("1.0", ", ".join(cur))

        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=6)
        ttk.Button(bf, text="从扫描路径嗅探", command=self._sniff).pack(side="left", padx=4)
        ttk.Button(bf, text="清空清单", command=self._clear).pack(side="left", padx=4)

        af = ttk.Frame(self)
        af.pack(fill="x", padx=10, pady=6)
        ttk.Button(af, text="确定", command=self._on_ok).pack(side="right", padx=4)
        ttk.Button(af, text="取消", command=self.destroy).pack(side="right", padx=4)

        self.status = ttk.Label(self, text="", foreground=ui_common.COLOR_INFO, wraplength=440)
        self.status.pack(anchor="w", padx=10, pady=(2, 6))

    def _sniff(self):
        root = (self.app.state.get("scan_root") or "").strip()
        if not root or not os.path.isdir(root):
            self.status.config(text="请先在上方设置有效的扫描路径，再嗅探。")
            return
        try:
            # 用带超时的 scandir 单次枚举（网络盘避免 listdir+逐个 isdir 的 N 次往返），
            # DirEntry.is_dir() 走缓存、基本不额外走网络；单个条目失败跳过，不拖累整体。
            it = _safe_scandir(root)
            devs = []
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        devs.append(e.name)
                except OSError:
                    continue
            devs.sort()
        except OSError as e:
            self.status.config(text="嗅探失败：%s" % e)
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", ", ".join(devs))
        self.status.config(text="已从扫描路径嗅探到 %d 个设备目录。" % len(devs))

    def _clear(self):
        self.text.delete("1.0", "end")
        self.status.config(text="已清空设备清单；需重新点「从扫描路径嗅探」填充后再开始扫描。")

    def _on_ok(self):
        seen, devs = set(), []
        raw = self.text.get("1.0", "end").strip()
        # 兼容逗号、空格、换行多种分隔，统一按逗号/空白拆分
        for tok in raw.replace("\n", ",").replace("，", ",").replace(" ", ",").split(","):
            d = tok.strip()
            if d and d not in seen:
                seen.add(d)
                devs.append(d)
        devs.sort()
        self.app.state["device_list"] = devs
        self.app._save_config()
        self.app._refresh_from_state()
        self.status.config(
            text="已保存 %d 台设备%s" % (len(devs), "（空=需手动嗅探）" if not devs else ""))
        self.after(700, self.destroy)


class TaskCenterDialog(tk.Toplevel):
    """任务中心：把「重新计算指标 / 补算缺失指标 / CSV 导出（手动+自动）」三个操作
    整合为一个多标签二级界面。公共区域复用产线/设备/月份筛选逻辑；
    每个标签页放各自专属参数与操作按钮，底部共用进度条与状态区。"""

    def __init__(self, parent, conn, db_lock, app):
        super().__init__(parent)
        self.withdraw()   # 先隐藏，避免默认位置闪现后再跳到居中
        self.conn = conn
        self.db_lock = db_lock
        self.app = app
        self._alive = True
        self._last_wrap_w = 0
        self._stop_event = threading.Event()
        self._running = False
        self.export_outdir_var = tk.StringVar()
        self.entry_export_outdir = None
        self.export_col_vars = {}
        # 自动导出（UI 在本页；运行线程挂在主程序 app 上，关闭本窗口不影响已启动的定时导出）
        self._auto_toggling = False
        self.auto_enabled_var = tk.BooleanVar(
            value=bool(self.app.state.get("csv_auto_enabled", False)))
        self.auto_interval_var = tk.StringVar(
            value=str(self.app.state.get("csv_auto_interval_min", 60)))
        self.auto_path_var = tk.StringVar(
            value=self.app.state.get("csv_auto_outdir", ""))
        self.chk_auto = None            # 以下控件在 _build_csv_tab 中创建
        self.entry_auto_interval = None
        self.entry_auto_path = None
        # 导出列折叠控件
        self._export_cols_expanded = tk.BooleanVar(value=False)
        self.btn_toggle_cols = None
        self.export_cols_frame = None
        self.title("任务中心")
        self.geometry("900x860")
        self.resizable(True, True)
        # 居中于主窗口
        self.update_idletasks()
        self.geometry("+%d+%d" % (
            parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2),
            parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)))
        try:
            _st = ttk.Style()
            _st.configure("Small.TButton", font=("Microsoft YaHei", 9), padding=(2, 2))
        except Exception:
            pass
        self._build()
        self._load_devices()
        self._load_months()
        self.deiconify()   # 定位完毕后再显示，消除闪烁
        self.lift()

    def destroy(self):
        self._alive = False
        super().destroy()

    def _build(self):
        # 底部固定区：操作按钮 + 选择提示/状态/进度（side=bottom 先占位，始终可见）
        frm_bottom = ttk.Frame(self)
        frm_bottom.pack(side="bottom", fill="x", padx=10, pady=(4, 6))

        # 顶部公共筛选区：产线 + 设备 + 月份（各标签页共用，共享组件与查看器同源）
        frm_filter = ttk.LabelFrame(self, text="筛选（产线 / 设备 / 月份，各标签页共用）",
                                    padding=(8, 4))
        frm_filter.pack(side="top", fill="x", padx=10, pady=(8, 4))

        # 当前选择摘要（由组件显示在产线行右侧）
        self.sel_var = tk.StringVar(value="")
        self.filter = ui_common.LineDeviceMonthFilter(
            frm_filter, on_change=self._update_sel_label, status_var=self.sel_var)
        self.filter.pack(fill="x")

        # Notebook：三个任务标签页（重新计算指标 / 补算缺失指标 / 手动导出）。
        # 各标签内容不多，故不撑满剩余空间（expand=False），高度由内容决定；
        # 把纵向空间让给下方「操作日志」区。
        _st = ttk.Style()
        try:
            # 标签默认常规字体；选中的标签加粗 + 变色，未选中的恢复默认
            _st.configure("Task.TNotebook.Tab", font=("Microsoft YaHei", 9))
            _st.map("Task.TNotebook.Tab",
                    font=[("selected", ("Microsoft YaHei", 9, "bold"))],
                    foreground=[("selected", ui_common.COLOR_INFO), ("!selected", "#333333")])
        except Exception:
            pass
        self.nb = ttk.Notebook(self, style="Task.TNotebook")
        self.nb.pack(side="top", fill="x", padx=10, pady=(4, 2))
        self._build_recalc_tab()
        self._build_compute_new_tab()
        self._build_csv_tab()

        # 任务中心日志区：所有标签页的操作日志都显示在此，不再写入主界面。
        # 它是主要纵向空间（expand=True），高度拉高，保证操作日志清晰可见。
        log_frame = ttk.LabelFrame(self, text="操作日志", padding=(6, 2))
        log_frame.pack(side="top", fill="both", expand=True, padx=10, pady=(2, 4))
        self.log_text = tk.Text(log_frame, height=14, wrap="none", state="disabled",
                                borderwidth=0, relief="flat",
                                font=("Microsoft YaHei", 8))
        self.log_sb = ttk.Scrollbar(log_frame, orient="vertical",
                                    command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=self.log_sb.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(2, 4), pady=4)
        self.log_sb.pack(side="right", fill="y")

        # 进度条 + 状态区
        self.prog = ttk.Progressbar(frm_bottom, orient="horizontal",
                                    mode="determinate", maximum=100)
        self.prog.pack(fill="x", pady=(2, 0))
        status_holder = ttk.Frame(frm_bottom, height=24)
        status_holder.pack(fill="x", padx=10, pady=2)
        status_holder.pack_propagate(False)
        self.status = ttk.Label(status_holder, text="", foreground=ui_common.COLOR_INFO,
                                anchor="nw", justify="left")
        self.status.pack(fill="both", expand=True)
        self.bind("<Configure>", self._on_configure)
        self._on_configure()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build_recalc_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="♻ 重新计算指标")
        ttk.Label(tab, text="按上方筛选的「月份 / 设备」范围重算指标。",
                  foreground="#666").pack(anchor="w", pady=(0, 8))
        btns = ttk.Frame(tab)
        btns.pack(anchor="w", pady=(8, 0))
        ttk.Button(btns, text="开始重算",
                   command=self._run_recalc).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="取消",
                   command=self._stop_recalc).pack(side="left")

    def _stop_recalc(self):
        if self._running:
            self._stop_event.set()
            try:
                self.status.config(text="正在取消…")
            except tk.TclError:
                pass

    def _build_compute_new_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="➕ 补算缺失指标")
        ttk.Label(tab, text="按上方筛选的「月份 / 设备」范围，仅补齐缺失指标的文件。",
                  foreground="#666").pack(anchor="w", pady=(0, 8))
        btns = ttk.Frame(tab)
        btns.pack(anchor="w", pady=(8, 0))
        ttk.Button(btns, text="开始补算",
                   command=self._run_compute_new).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="取消",
                   command=self._stop_recalc).pack(side="left")

    def _build_csv_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="⇩ CSV 导出")

        # ===== 手动导出（按筛选，一次性） =====
        man = ttk.LabelFrame(tab, text="手动导出（按上方筛选，一次性）", padding=(6, 4))
        man.pack(fill="x")
        ttk.Label(man, text="按上方筛选的「月份 / 设备」范围导出 CSV，目录在开始导出时选择。",
                  foreground="#666").pack(anchor="w", pady=(0, 8))
        # 导出目录在「开始导出」时弹出选择（见 _run_export）

        # 导出列：内容较多，做成可折叠按钮 —— 平时收起（仅一个按钮），点击展开列勾选区
        col_toggle_row = ttk.Frame(man)
        col_toggle_row.pack(fill="x", pady=(8, 0))
        self._export_cols_expanded = tk.BooleanVar(value=False)
        self.btn_toggle_cols = ttk.Button(col_toggle_row, text="▶ 展开导出列",
                                          command=self._toggle_export_cols)
        self.btn_toggle_cols.pack(side="left")
        ttk.Label(col_toggle_row, text="（列与自动导出共用一份配置）",
                  foreground="#888").pack(side="left", padx=(6, 0))

        # 列勾选区（默认收起，由 _toggle_export_cols 控制显示/隐藏）
        self.export_cols_frame = ttk.Frame(man)
        cf = ttk.LabelFrame(self.export_cols_frame, text="导出列（可复选）")
        cf.pack(fill="x", pady=(6, 4))
        _cols = [label for label, _ in export_csv.ALL_COLUMNS]
        _saved = self.app.state.get("csv_cols") or []
        _ncol = 4
        _export_rows = max(1, (len(_cols) + _ncol - 1) // _ncol)
        box_h = min(max(90, _export_rows * 22 + 8), 220)
        canv_frame = ttk.Frame(cf, borderwidth=1, relief="solid")
        canv_frame.pack(fill="x", padx=6, pady=4)
        _canv = tk.Canvas(canv_frame, highlightthickness=0, height=box_h)
        _sb = ttk.Scrollbar(canv_frame, orient="vertical", command=_canv.yview)
        _inner = ttk.Frame(_canv)
        _inner.bind("<Configure>",
                    lambda e: _canv.configure(scrollregion=_canv.bbox("all")))
        _win = _canv.create_window((0, 0), window=_inner, anchor="nw")
        _canv.bind("<Configure>", lambda e: _canv.itemconfigure(_win, width=e.width))
        _canv.configure(yscrollcommand=_sb.set)
        _canv.pack(side="left", fill="both", expand=True)
        _sb.pack(side="right", fill="y")
        _canv.bind("<MouseWheel>",
                   lambda e: _canv.yview_scroll(int(-e.delta / 120), "units"))
        self.export_col_vars = {}
        _exp_widgets = []
        for _label in _cols:
            # 有保存的选择则按保存勾选，否则全部未选
            _v = tk.BooleanVar(value=_label in _saved)
            self.export_col_vars[_label] = _v
            _exp_widgets.append(ttk.Checkbutton(_inner, text=_label, variable=_v))
        _COL_W = 150
        def _relayout():
            _w = _canv.winfo_width()
            _n = max(1, _w // (_COL_W + 10))
            for _i, _wd in enumerate(_exp_widgets):
                _wd.grid(row=_i // _n, column=_i % _n, sticky="w", padx=4, pady=1)
            _canv.configure(scrollregion=_canv.bbox("all"))
        _canv.bind("<Configure>", lambda e: _relayout())
        # 导出列全选/清空
        bf_cols = ttk.Frame(self.export_cols_frame)
        bf_cols.pack(fill="x", pady=(2, 6))
        ttk.Button(bf_cols, text="全选列", style="Small.TButton",
                   command=self._set_export_cols_all).pack(side="left", padx=2)
        ttk.Button(bf_cols, text="清列", style="Small.TButton",
                   command=self._set_export_cols_none).pack(side="left", padx=2)
        # 收起状态：初始不显示列勾选区
        self.export_cols_frame.pack(fill="x")
        self.export_cols_frame.pack_forget()
        # 手动导出按钮：开始 / 取消
        btns = ttk.Frame(man)
        btns.pack(anchor="w", pady=(8, 0))
        ttk.Button(btns, text="开始导出",
                   command=self._run_export).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="取消",
                   command=self._stop_recalc).pack(side="left")

        # ===== 自动导出（定时增量；运行线程在主程序，关闭本窗口不停止） =====
        auto = ttk.LabelFrame(
            tab, text="自动导出（定时增量：只重写最近两个月有新增的月份）", padding=(6, 4))
        auto.pack(fill="x", pady=(10, 0))

        row1 = ttk.Frame(auto)
        row1.pack(fill="x", pady=(2, 2))
        # 启用自动导出只通过 trace 触发一次（避免 command + trace 双重调用）；
        # 回滚 auto_enabled_var.set(False) 也会被 trace 捕获，路径统一。
        self.chk_auto = ttk.Checkbutton(row1, text="启用自动导出（勾选即开始 / 取消即停止）",
                                        variable=self.auto_enabled_var)
        self.chk_auto.pack(side="left")
        ttk.Label(row1, text="间隔：").pack(side="right", padx=(10, 2))
        self.entry_auto_interval = ttk.Entry(row1, textvariable=self.auto_interval_var, width=7)
        self.entry_auto_interval.pack(side="right", padx=(2, 0))
        ttk.Label(row1, text="分").pack(side="right", padx=(0, 2))
        self.auto_interval_var.trace_add(
            "write", lambda *a: self._save_auto_state())
        self.auto_enabled_var.trace_add(
            "write", lambda *a: self._on_auto_enabled_toggle())

        row2 = ttk.Frame(auto)
        row2.pack(fill="x", pady=(2, 2))
        row2.columnconfigure(1, weight=1)
        ttk.Label(row2, text="导出目录：").grid(row=0, column=0, sticky="w")
        self.entry_auto_path = ttk.Entry(row2, textvariable=self.auto_path_var)
        self.entry_auto_path.grid(row=0, column=1, sticky="ew", padx=(4, 4))
        ttk.Button(row2, text="浏览...", command=self._browse_auto_path).grid(
            row=0, column=2, sticky="e")
        ttk.Button(row2, text="全量导出一次",
                   command=lambda: self.app._auto_export_once(log=True, manual=True)).grid(
            row=0, column=3, sticky="w", padx=(6, 0))
        ttk.Button(row2, text="打开目录",
                   command=self._open_auto_path).grid(row=0, column=4, sticky="w", padx=(6, 0))

        ttk.Label(auto, text="「全量导出一次」立即导出库中所有月份（带 --all）；"
                             "定时导出只增量更新最近两个月，历史月文件原样保留。",
                  foreground="#888").pack(anchor="w", pady=(2, 0))

    # ---------- 筛选联动 ----------
    def _load_devices(self):
        try:
            rows = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT device FROM processed "
                "WHERE device IS NOT NULL AND device <> '' ORDER BY device")]
        except Exception:
            rows = []
        self.filter.set_devices(rows)
        self.filter.apply_line()

    def _load_months(self):
        try:
            rows = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT ym FROM processed "
                "WHERE ym IS NOT NULL AND ym <> '' ORDER BY ym")]
        except Exception:
            rows = []
        self.filter.set_months(rows)
        # 月份默认全部不选；留空 = 不按月份过滤（等价全部），不预选
        self._update_sel_label()

    def _update_sel_label(self):
        if not getattr(self, "_alive", True):
            return
        line = "/".join(self.filter.selected_lines()) or "（不限）"
        ndev = len(self.filter.selected_devices())
        ndev_total = len(self.filter.device_items())
        nym = len(self.filter.selected_months())
        nym_total = self.filter.mth_listbox.size()
        dev_txt = "未选" if ndev == 0 else "%d/%d" % (ndev, ndev_total)
        ym_txt = "未选" if nym == 0 else "%d/%d" % (nym, nym_total)
        self.sel_var.set("当前选择 → 产线: %s | 设备: %s | 月份: %s"
                         % (line, dev_txt, ym_txt))

    def _on_configure(self, event=None):
        w = self.winfo_width() - 20
        if w < 60:
            w = 60
        if getattr(self, "_last_wrap_w", None) == w:
            return
        self._last_wrap_w = w
        try:
            self.status.configure(wraplength=w)
        except tk.TclError:
            pass

    # ---------- 当前筛选结果 ----------
    def _selected(self):
        ym = self.filter.selected_months()
        dev = self.filter.selected_devices()
        lines = self.filter.selected_lines()
        return ym, dev, lines

    def _enter_run(self):
        self._stop_event.clear()
        self.status.config(text="处理中…")
        self.prog.configure(value=0)
        self.filter.mth_listbox.configure(state="disabled")
        self.filter.dev_listbox.configure(state="disabled")
        self._running = True

    def _exit_run(self, msg):
        self.status.config(text=msg)
        self.filter.mth_listbox.configure(state="normal")
        self.filter.dev_listbox.configure(state="normal")
        self._running = False

    def _cancel(self):
        if getattr(self, "_stop_event", None) is None or not getattr(self, "_running", False):
            self.destroy()
            return
        self._stop_event.set()
        try:
            self.status.config(text="正在取消（请稍候）…")
        except tk.TclError:
            pass

    def _progress_cb(self):
        prog = self.prog
        def _cb(done, total):
            if total > 0:
                pct = int(done * 100 / total)
                self.after(0, lambda: prog.configure(value=pct))
        return _cb

    # ---------- 重新计算指标 ----------
    def _require_filter(self):
        """重算/补算/导出前检查：设备、月份都必须选择（留空=一个都没选，不允许）。
        任一缺失则提示用户，返回 False；否则返回 True。"""
        ym, dev, _lines = self._selected()
        if not dev:
            messagebox.showwarning(
                "未选择设备",
                "请先勾选要处理的设备。",
                parent=self)
            return False
        if not ym:
            messagebox.showwarning(
                "未选择月份",
                "请先勾选要处理的月份。",
                parent=self)
            return False
        return True

    def _run_recalc(self):
        if not self._require_filter():
            return
        if self.app.thread and self.app.thread.is_alive():
            messagebox.showwarning("扫描进行中",
                                   "请先停止扫描，再重新计算指标。", parent=self)
            return
        ym, dev, _lines = self._selected()
        if self._running:
            return
        self._log("重新计算指标开始 → 月份 %s，设备 %s"
                  % (ym or "全部", "、".join(dev) if dev else "全部"))
        self._enter_run()
        threading.Thread(target=self._recalc_worker,
                         args=(ym, dev, False), daemon=True).start()

    # ---------- 补算缺失指标 ----------
    def _run_compute_new(self):
        if not self._require_filter():
            return
        if self.app.thread and self.app.thread.is_alive():
            messagebox.showwarning("扫描进行中",
                                   "请先停止扫描，再补算缺失指标。", parent=self)
            return
        ym, dev, _lines = self._selected()
        if self._running:
            return
        self._log("补算缺失指标开始 → 月份 %s，设备 %s"
                  % (ym or "全部", "、".join(dev) if dev else "全部"))
        self._enter_run()
        threading.Thread(target=self._recalc_worker,
                         args=(ym, dev, True), daemon=True).start()

    def _recalc_worker(self, ym_sel, dev_sel, missing_only):
        self.after(0, lambda: self.prog.configure(value=0))
        try:
            if missing_only:
                n = analysis.recalc_missing(self.conn, self.db_lock, ym_sel, dev_sel,
                                            on_progress=self._progress_cb(),
                                            stop_event=self._stop_event,
                                            use_process=self.app._use_process() if self.app else False)
            else:
                n = analysis.recalc_by_months(self.conn, self.db_lock, ym_sel, dev_sel,
                                              on_progress=self._progress_cb(),
                                              stop_event=self._stop_event,
                                              workers=self.app._get_compute_workers() if self.app else None,
                                              use_process=self.app._use_process() if self.app else False)
            msg = "已%s %d 个文件" % ("补齐" if missing_only else "重算", n)
        except Exception as e:
            n = 0
            msg = "出错：" + str(e)
        if self._stop_event.is_set() and "出错" not in msg:
            msg = "已取消（已处理 %d 个）" % n
        self.after(0, lambda: self._finish_recalc(msg))

    def _finish_recalc(self, msg):
        self._exit_run(msg)
        self._log(msg)
        if self.app:
            try:
                self.app._refresh_from_state()
            except Exception:
                pass

    # ---------- 手动导出 ----------
    def _set_export_cols_all(self):
        for v in self.export_col_vars.values():
            v.set(True)
        self._save_export_cols()

    def _set_export_cols_none(self):
        for v in self.export_col_vars.values():
            v.set(False)
        self._save_export_cols()

    def _toggle_export_cols(self):
        """展开 / 收起 CSV 导出页的列勾选区。"""
        if self.export_cols_frame is None or self.btn_toggle_cols is None:
            return
        if self._export_cols_expanded.get():
            self.export_cols_frame.pack_forget()
            self.btn_toggle_cols.config(text="▶ 展开导出列")
            self._export_cols_expanded.set(False)
        else:
            self.export_cols_frame.pack(fill="x")
            self.btn_toggle_cols.config(text="▼ 收起导出列")
            self._export_cols_expanded.set(True)

    def _save_export_cols(self):
        """导出列（与自动导出共用一份 csv_cols）持久化。"""
        cols = [lbl for lbl, v in self.export_col_vars.items() if v.get()]
        self.app.state["csv_cols"] = cols
        self.app._save_config()

    # ---------- 自动导出（定时增量；运行线程挂在主程序 app 上） ----------
    def _save_auto_state(self):
        """把自动导出 UI 值写入 state 并持久化；间隔非法时跳过本次保存（不弹窗）。"""
        try:
            interval = int(self.auto_interval_var.get().strip())
        except ValueError:
            return False
        if interval <= 0:
            return False
        self.app.state["csv_auto_enabled"] = bool(self.auto_enabled_var.get())
        self.app.state["csv_auto_interval_min"] = interval
        self.app.state["csv_auto_outdir"] = self.auto_path_var.get().strip()
        self.app._save_config()
        return True

    def _on_auto_enabled_toggle(self):
        """勾选「启用自动导出」即立即开始定时导出；取消勾选即停止并持久化偏好。"""
        if getattr(self, "_auto_toggling", False):
            return
        self._auto_toggling = True
        try:
            self._save_auto_state()
            if self.auto_enabled_var.get():
                ok = self.app.start_auto_export(silent=False)
                if not ok:
                    # 启动失败（如未设目录/间隔非法）则回滚勾选并持久化
                    self.auto_enabled_var.set(False)
                    self._save_auto_state()
            else:
                self.app.stop_auto_export()
        finally:
            self._auto_toggling = False

    def _browse_auto_path(self):
        d = filedialog.askdirectory(parent=self,
                                    initialdir=self.auto_path_var.get() or os.path.expanduser("~"))
        if d:
            self.auto_path_var.set(d)
            self._save_auto_state()

    def _open_auto_path(self):
        """在系统文件管理器中打开自动导出目录（不存在则先创建）。"""
        d = self.auto_path_var.get().strip()
        if not d:
            messagebox.showinfo("导出目录", "请先设置自动导出目录。", parent=self)
            return
        if not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:
                messagebox.showerror("导出目录", "目录不存在且无法创建：%s" % e, parent=self)
                return
        try:
            os.startfile(d)  # Windows：用默认文件管理器打开
        except Exception as e:
            messagebox.showerror("导出目录", "无法打开目录：%s" % e, parent=self)

    def _run_export(self):
        if not self._require_filter():
            return
        # 弹出选择导出目录
        outdir = filedialog.askdirectory(
            parent=self,
            initialdir=self.app.state.get("csv_manual_outdir") or os.path.expanduser("~"))
        if not outdir:
            return
        self.export_outdir_var.set(outdir)
        cols = [lbl for lbl, v in self.export_col_vars.items() if v.get()]
        if not cols:
            messagebox.showwarning("列未选", "请至少选择一列导出。", parent=self)
            return
        if self._running:
            return
        ym, dev, lines = self._selected()
        dev_set = set(dev)
        # 产线联动：把产线下所有设备也纳入
        if lines:
            for d in self.filter.device_items():
                if analysis.classify_line(d) in lines:
                    dev_set.add(d)
        months = ym
        if not months:
            try:
                months = [r[0] for r in self.conn.execute(
                    "SELECT DISTINCT ym FROM processed "
                    "WHERE ym IS NOT NULL AND ym <> '' ORDER BY ym")]
            except Exception:
                months = []
        self.app.state["csv_manual_outdir"] = outdir
        self.app.state["csv_cols"] = cols
        self.app._save_config()
        self._enter_run()
        threading.Thread(target=self._export_worker,
                         args=(outdir, sorted(dev_set), months, cols),
                         daemon=True).start()

    def _export_worker(self, outdir, devices, months, cols):
        import subprocess
        script = os.path.join(_HERE, "export_csv.py")
        total = len(months)
        self._log("手动导出开始 → 目录=%s | 设备=%s | 列=%d个 | 月份=%d个"
                  % (outdir, "、".join(devices) if devices else "全部", len(cols), total))
        try:
            if not months:
                self.after(0, lambda: self.status.config(text="没有可导出的月份（数据库为空）。"))
            self.after(0, lambda: self.prog.configure(maximum=max(1, total)))
            idx = 0
            for idx, m in enumerate(months, 1):
                if self._stop_event.is_set():
                    break
                cmd = [sys.executable, script, "--outdir", outdir, "--ym", m, "--cols"] + cols
                if devices:
                    cmd += ["--device"] + devices
                self._log("手动导出 正在导出 %s … (%d/%d)" % (m, idx, total))
                self.after(0, lambda idx=idx, total=total, m=m: (
                    self.status.config(text="正在导出 %s … (%d/%d)" % (m, idx, total)),
                    self.prog.config(value=idx)))
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            msg = "导出完成：%d 个月" % (len(months) if not self._stop_event.is_set() else idx)
            if self._stop_event.is_set():
                msg = "已取消（已处理 %d 个月）" % idx
            self._log("手动导出 " + msg)
            self.after(0, lambda: self._exit_run(msg))
        except Exception as e:
            self._log("手动导出 失败：" + str(e))
            self.after(0, lambda e=e: self._exit_run("导出失败：" + str(e)))

    def _log(self, msg):
        """把操作日志写入任务中心窗口的日志区（不写入主界面）。可在工作线程调用。"""
        if not getattr(self, "_alive", False) or not hasattr(self, "log_text"):
            return
        try:
            text = ("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
            self.after(0, lambda t=text: self._append_log_text(t))
        except Exception:
            pass

    def _append_log_text(self, text):
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _clear_log(self):
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass


def _dpi_scale():
    """返回系统 DPI 缩放系数（dpi/96）。

    参照常用写法：取主屏 HDC 的 LOGPIXELSX(=88) 实际 DPI 再除以 96。
    在已声明 DPI 感知的进程里，该值即系统真实缩放（如 150% → 1.5）；
    非 Windows 或取不到时返回 1.0。
    """
    scale = 1.0
    if sys.platform == "win32":
        try:
            import ctypes
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # 88 = LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
            if dpi:
                scale = dpi / 96.0
        except Exception:
            pass
    return scale


def _enable_high_dpi():
    """在创建任何 Tk 窗口前调用：声明进程 DPI 感知，使高分屏下界面按系统缩放、不模糊。

    Tk 默认是 DPI-unaware，系统会对窗口做位图拉伸 → 控件偏小且发虚。声明感知后，
    Tk 自动按系统 DPI 缩放 point 字号与像素布局（tk scaling 随之生效）。
    """
    try:
        import ctypes
        # PROCESS_SYSTEM_DPI_AWARE = 1：按系统 DPI 统一缩放，跨多屏移动行为一致、最稳妥。
        # （PER_MONITOR=2 在 Tk 8.6 下跨屏移动不会动态重算缩放，反而易错位，故不用。）
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            # Windows 8.0 之前无 shcore，退化为 user32 API
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    _enable_high_dpi()  # 必须在 tk.Tk() 之前，否则窗口已建无法再声明 DPI 感知
    root = tk.Tk()
    app = ScannerApp(root)
    app._refresh_poll_state()  # 启动时不扫描，扫描「间隔（秒）」默认可改
    root.protocol("WM_DELETE_WINDOW",
                  lambda: (app.stop_auto_export(), app._save_config(), root.destroy()))
    root.mainloop()
    # 退出时关闭全局进程池（若有），避免残留子进程
    try:
        analysis._shutdown_proc_pool()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 启动期崩溃不要直接闪退：弹窗显示错误并写日志，方便定位
        import traceback
        tb = traceback.format_exc()
        try:
            with open(os.path.join(_HERE, "gui_crash.log"), "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        try:
            messagebox.showerror("程序启动失败", tb)
        except Exception:
            pass
        raise
