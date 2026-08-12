#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用标准库(tkinter + sqlite3)直接读取 scan_state.db 并以表格展示文件记录。
  - 只读连接，不修改数据库（可与扫描器 GUI 并发查看）。
  - 表格列：固定列 `# / 设备 / 月份 / 路径 / 处理时间` + `analysis.METRICS` 的全部指标列（品名/原品名/LOT/Block/日期/总枚数/外层枚数/内外层各报警代码/MISS总回数·奇数/剥离值方差/**大小**）。
  - 「大小」(`metrics.size`) 在「计算指标」阶段由 os.path.getsize 取一次、写入 metrics 表；扫描发现阶段不取 size（只存 path/ts）以保性能，故大小属于 metrics 指标宽表、是 METRICS 注册表最后一项，不在固定列。
  - 筛选条件（实时生效）：
      设备：多选列表框（可全选/清空，支持 (空) 表示 device 为 NULL）
      月份：多选列表框（同上）
      路径：支持通配符 * ? （自动转 SQL 的 % _），多条件用 | , ; 或空格分隔（OR 关系）
      状态：全部 / ok / simulated / 无结果
  - 底部状态栏显示当前过滤下的文件数。
  - 「导出 CSV」按钮：将当前筛选结果导出为 CSV（UTF-8-SIG，Excel 可直接打开）。

既可作为独立程序运行（python db_view.py），
也可由扫描器主 UI 通过 open_db_view(parent) 以内嵌窗口打开。
"""
from __future__ import annotations
import os
import re
import csv
import json
import sqlite3
import datetime
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import analysis
import db_maintain   # 复用 run_maintain 做归档/瘦身（GUI 按钮触发，不再需命令行）
from analysis import column_defs, join_clause, classify_line   # 指标列定义 / SQL JOIN 均来自 analysis

# 分页设置（仅影响界面渲染，不影响 CSV 导出）
MAX_ROWS = 3000      # 界面最多加载/浏览的总行数上限（超出部分提示用筛选缩小范围）
PAGE_SIZE = 500      # 每页最多显示的行数

# 允许点击表头排序的列（设备、月份本身即 processed 表字段，排序无需额外索引）。
# 其余列点击表头不再排序。
_SORTABLE_COLS = {"设备", "月份"}

# 固定列（顺序即表格显示顺序）；全部可在「显示列」对话框中勾选隐藏
_FIXED_COLS = ["#", "设备", "月份", "路径", "处理时间"]

# 导出 CSV 时用户所选列的持久化文件（下次默认沿用）
_EXPORT_COLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csv_export_cols.json")

_HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(_HERE, "scan_state.db")


class DBViewApp:
    def __init__(self, root: tk.Tk | tk.Toplevel):
        self.root = root
        self.root.title("数据库文件查看器 (scan_state.db)")
        self.root.geometry("1440x860")
        self._apply_style()
        devs, yms = self._load_filters()
        self._visible_cols, self._visible_fixed = self._load_visible_cols()
        # 分页状态（新建分页栏前需先存在）
        self._page = 0          # 当前页（0-based）
        self._total_pages = 1   # 总页数（由 _refresh 依总数/每页大小计算）
        self._page_size = PAGE_SIZE
        self._build_ui(devs, yms)
        self._refresh()
        # 延迟到布局完成再测宽（避免同帧内 Treeview 几何未就绪导致测量偏小）
        self.root.after_idle(self._autosize_columns)

    def _apply_style(self):
        """统一字体 + 让表格行高随字体自适应（高分辨率下随 DPI 放大、不被裁切）。"""
        import tkinter.font as tkfont
        style = ttk.Style(self.root)
        # 主题：内嵌时沿用主程序已设主题；独立运行时优先原生 vista
        try:
            cur = style.theme_use()
        except Exception:
            cur = ""
        if cur not in ("vista", "xpnative", "clam"):
            for pref in ("vista", "xpnative", "clam"):
                if pref in style.theme_names():
                    try:
                        style.theme_use(pref)
                        break
                    except Exception:
                        continue
        # 表格字体沿用全局默认（ttk 主题决定的 Treeview 内容字体，
        # 经实测 style.configure("Treeview", font=...) 在本主题下不生效，
        # 故不在此硬性更换字体，避免"改了等于没改"的错觉）。
        style.configure(".", font=("Microsoft YaHei", 8))
        # 行高随字体行距自适应（高分辨率下不被裁切）；加一点余量让行更宽松
        row_font = tkfont.Font(font=("Microsoft YaHei", 8))
        style.configure("Treeview", rowheight=row_font.metrics("linespace") + 8)
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 8, "bold"))

    # 只读连接；mode=ro 失败则回退普通连接（我们只会 SELECT）
    def _conn(self):
        try:
            return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        except sqlite3.Error:
            return sqlite3.connect(DB)

    # ---------- 筛选数据加载 ----------
    def _load_filters(self):
        try:
            c = self._conn()
            devs = [r[0] or "(空)" for r in
                    c.execute("SELECT DISTINCT device FROM processed ORDER BY device")]
            yms = [r[0] or "(空)" for r in
                   c.execute("SELECT DISTINCT ym FROM processed ORDER BY ym")]
            c.close()
        except sqlite3.Error as e:
            messagebox.showerror("打开数据库失败", str(e), parent=self.root)
            devs, yms = [], []
        return devs, yms

    def _all_metric_cols(self):
        return list(analysis.METRICS.keys())

    def _make_col_index(self):
        """列名 -> 数据行元组下标（与列可见性无关，供排序取值用）。
        row = (device, ym, path, size, ts, 指标1, 指标2, ...)。"""
        idx = {"设备": 0, "月份": 1, "路径": 2, "处理时间": 3}
        # 固定列现占 4 个下标（device/ym/path/ts）；指标列从下标 4 起
        for j, name in enumerate(self._all_metric_cols_list):
            idx[name] = 4 + j
        return idx

    # ---------- 列宽 / 对齐 辅助 ----------
    def _metric_type(self, name):
        """把列 label 映射回 METRICS key，并返回其 SQL 类型（TEXT/INTEGER/REAL）。
        固定列或未在 METRICS 中的列返回 None。"""
        key = getattr(self, "_label2key", {}).get(name)
        if key is None:
            return None
        m = analysis.METRICS.get(key)
        return (m or {}).get("type")

    def _col_width(self, name):
        """按指标 SQL 类型给合理的默认列宽（像素），完全自适应：
        - TEXT    -> 文本类，给宽（品名类 140，其余 100）
        - INTEGER -> 计数类，窄（75）
        - REAL    -> 实数类，中等（100）
        新增指标只要填对 type，宽窄自动正确，无需维护 key 白名单。"""
        t = self._metric_type(name)
        if t == "TEXT":
            key = getattr(self, "_label2key", {}).get(name)
            return 140 if key in ("hinmei", "hinmei_fmt") else 100
        if t == "REAL":
            return 100
        return 75   # INTEGER / 未知类型统一窄

    def _col_anchor(self, name):
        """文本列左对齐，数值列右对齐。name 为表格列 label，按 type 自动推断。"""
        t = self._metric_type(name)
        if t == "TEXT":
            return "w"
        return "e"

    def _load_visible_cols(self):
        """读取查看器可见列的持久化选择。
        返回 (指标key集合, 固定列集合)。兼容旧版纯列表格式（仅指标 key）。"""
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "visible_cols.json")
        all_cols = self._all_metric_cols()
        metrics, fixed = set(all_cols), set(_FIXED_COLS)
        try:
            with open(p, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, list):        # 旧格式：仅指标 key 列表
                valid = [c for c in saved if c in all_cols]
                metrics = set(valid) if valid else set(all_cols)
            elif isinstance(saved, dict):      # 新格式：{"metrics": [...], "fixed": [...]}
                metrics = {c for c in saved.get("metrics", []) if c in all_cols}
                fixed = {c for c in saved.get("fixed", []) if c in _FIXED_COLS}
        except Exception:
            pass
        return metrics, fixed

    def _save_visible_cols(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "visible_cols.json")
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"metrics": sorted(self._visible_cols),
                           "fixed": [c for c in _FIXED_COLS if c in self._visible_fixed]},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 多选列表框辅助 ----------
    @staticmethod
    def _mk_multi(parent, title, values, on_change):
        """构造一个『多选列表框』分组，返回 (group_frame, listbox)。"""
        g = ttk.LabelFrame(parent, text=title, padding=4)
        lb = tk.Listbox(g, selectmode="extended", height=5, width=10,
                        exportselection=0)  # exportselection=0：失去焦点仍保留选中
        for v in values:
            lb.insert("end", v)
        sb = ttk.Scrollbar(g, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.grid(row=0, column=0, sticky="ns")
        sb.grid(row=0, column=1, sticky="ns")
        bf = ttk.Frame(g)
        bf.grid(row=1, column=0, columnspan=2, pady=(3, 0))
        ttk.Button(bf, text="全选", width=5,
                   command=lambda: (lb.selection_set(0, "end"), on_change())).grid(row=0, column=0, padx=2)
        ttk.Button(bf, text="清空", width=5,
                   command=lambda: (lb.selection_clear(0, "end"), on_change())).grid(row=0, column=1, padx=2)
        lb.bind("<<ListboxSelect>>", lambda e: on_change())
        return g, lb

    @staticmethod
    def _sel(lb: tk.Listbox):
        return [lb.get(i) for i in lb.curselection()]

    # ---------- UI 构建 ----------
    def _build_ui(self, devs, yms):
        # 用于『产线→设备』展开的全部设备（不含 (空)）
        self._devices = [d for d in devs if d != "(空)"]
        # 数据中实际存在的产线，固定顺序 E / C / D / A
        self._lines = [ln for ln in ("E", "C", "D", "A")
                       if any(classify_line(d) == ln for d in self._devices)]

        # ---- 筛选区：第一行（产线多选 / 设备多选 / 月份多选 / 路径通配） ----
        frm = ttk.Frame(self.root, padding=8)
        frm.grid(row=0, column=0, sticky="ew")
        frm.columnconfigure(3, weight=1)

        g_line, self.line_lb = self._mk_multi(frm, "产线(多选)", self._lines, self._on_line_change)
        g_line.grid(row=0, column=0, padx=(0, 8), sticky="ns")
        g_dev, self.dev_lb = self._mk_multi(frm, "设备(多选)", devs, self._on_filter_change)
        g_dev.grid(row=0, column=1, padx=(0, 8), sticky="ns")
        g_ym, self.ym_lb = self._mk_multi(frm, "月份(多选)", yms, self._on_filter_change)
        g_ym.grid(row=0, column=2, padx=(0, 8), sticky="ns")

        p_grp = ttk.LabelFrame(frm, text="路径（通配符 * ?；多条件用 | 分隔，OR）", padding=4)
        p_grp.grid(row=0, column=3, sticky="nsew")
        self.q_var = tk.StringVar()
        ttk.Entry(p_grp, textvariable=self.q_var).pack(fill="x", padx=2, pady=2)
        self.q_var.trace_add("write", lambda *a: self._on_filter_change())

        # ---- 筛选区：第二行 ----
        frm2 = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        frm2.grid(row=1, column=0, sticky="ew")

        ttk.Button(frm2, text="重置筛选", command=self._reset_filters).grid(row=0, column=0, padx=(0, 0))
        ttk.Button(frm2, text="显示列", command=self._pick_visible_columns).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(frm2, text="归档旧数据", command=self._open_archive_dialog).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(frm2, text="复制选中", command=self._copy_selected).grid(row=0, column=3, padx=(6, 0))
        ttk.Button(frm2, text="导出 CSV", command=self._export_csv).grid(row=0, column=4, padx=(6, 0))

        # ---- 状态栏 ----
        self.stat_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.stat_var, foreground="#1a73e8").grid(
            row=2, column=0, sticky="w", padx=8)

        # ---- 表格（列定义来自 analysis，新增指标自动出现） ----
        metric_defs = column_defs()
        all_metric_cols = [d[1] for d in metric_defs]   # 全量指标列 label（顺序 = 数据库列序）
        metric_keys = list(analysis.METRICS.keys())     # 与列序一一对应的指标 key
        # label -> key 映射：_visible_cols 存的是 key，而表格列/SQL 结果序用的是 label
        self._label2key = dict(zip(all_metric_cols, metric_keys))
        # 仅显示「可见列」选择中的指标列：按 key 过滤后映射回 label
        visible_metrics = [label for key, label in zip(metric_keys, all_metric_cols)
                           if key in self._visible_cols]
        fixed_vis = [c for c in _FIXED_COLS if c in self._visible_fixed]
        cols = fixed_vis + visible_metrics
        self.cols = cols
        self._all_metric_cols_list = all_metric_cols
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings")
        # 隔行灰色 tag：必须等 self.tree 创建后再配置（在 _apply_style 里会因 tree 未创建而失效）
        self.tree.tag_configure("oddrow", background="#F2F2F2")
        # 固定列宽度
        base_widths = {"#": 45, "设备": 70, "月份": 75, "路径": 260,
                       "处理时间": 140}
        for c in fixed_vis:
            if c in _SORTABLE_COLS:
                self.tree.heading(c, text=c, command=lambda c=c: self._on_heading_click(c))
            else:
                self.tree.heading(c, text=c)
            # 路径列 minwidth 仅防空路径塌陷；真实宽度由 _autosize_columns 按内容测量，
            # 不再设大下限，否则 width<minwidth 时被强制撑开产生空白
            min_w = 120 if c == "路径" else 40
            self.tree.column(c, width=base_widths[c], minwidth=min_w, anchor="w",
                             stretch=False)
        if "#" in cols:
            self.tree.column("#", anchor="center")
        if "路径" in cols:
            # 路径列不拉伸：宽度由 _autosize_columns 按最长路径测量，
            # stretch=False 使超长路径时总列宽超出窗口、启用水平滚动条查看完整路径
            self.tree.column("路径", stretch=False, minwidth=120)
        # 指标列宽度：按类型/名称给合理默认，允许拖拽调整
        for c in visible_metrics:
            if c in _SORTABLE_COLS:
                self.tree.heading(c, text=c, command=lambda c=c: self._on_heading_click(c))
            else:
                self.tree.heading(c, text=c)
            self.tree.column(c, width=self._col_width(c), minwidth=40,
                             anchor=self._col_anchor(c), stretch=False)

        # 排序状态：当前排序列名、方向；以及裸数据行（原始查询结果，用于重排）
        self._raw_rows = []
        self._sort_col = None
        self._sort_dir = "asc"
        self._col_index = self._make_col_index()
        # 记录用户手动拖拽过宽度的列，避免刷新时被自适应覆盖
        self._manual_widths = set()
        # 监听列宽拖拽：拖动后标记该列，刷新时不再自动调整它
        self.tree.bind("<ButtonRelease-1>",
                       lambda e: self._on_col_resize())
        self.tree.bind("<B1-Leave>",
                       lambda e: self._on_col_resize())

        vsb = ttk.Scrollbar(self.root, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self.root, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        vsb.grid(row=3, column=1, sticky="ns", pady=4)
        hsb.grid(row=4, column=0, sticky="ew", padx=8)
        self.root.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)

        # ---- 分页栏（表格下方独立一行，左对齐，窄窗口也始终可见） ----
        pf = ttk.Frame(self.root, padding=(4, 4))
        pf.grid(row=5, column=0, sticky="w", padx=8, pady=(0, 4))
        ttk.Label(pf, text="分页：").grid(row=0, column=0, padx=(0, 4))
        self._pg_first = ttk.Button(pf, text="首页", width=5, command=lambda: self._goto_page(0))
        self._pg_prev = ttk.Button(pf, text="上一页", width=6, command=lambda: self._goto_page(self._page - 1))
        self._pg_label = ttk.Label(pf, text="第 1 / 1 页")
        self._pg_next = ttk.Button(pf, text="下一页", width=6, command=lambda: self._goto_page(self._page + 1))
        self._pg_last = ttk.Button(pf, text="末页", width=5, command=lambda: self._goto_page(self._total_pages - 1))
        self._pg_first.grid(row=0, column=1, padx=2)
        self._pg_prev.grid(row=0, column=2, padx=2)
        self._pg_label.grid(row=0, column=3, padx=4)
        self._pg_next.grid(row=0, column=4, padx=2)
        self._pg_last.grid(row=0, column=5, padx=2)
        ttk.Label(pf, text="每页").grid(row=0, column=6, padx=(6, 1))
        self._pg_size = ttk.Combobox(pf, values=[200, 500, 1000, 2000, 3000],
                                     width=5, state="readonly")
        self._pg_size.set(str(self._page_size))
        self._pg_size.bind("<<ComboboxSelected>>", self._on_page_size_change)
        self._pg_size.grid(row=0, column=7, padx=1)
        ttk.Label(pf, text="行").grid(row=0, column=8, padx=(1, 2))

        # ---- 选中复制（整行）：右键菜单 + Ctrl+C ----
        self._ctx_menu = tk.Menu(self.tree, tearoff=0)
        self._ctx_menu.add_command(label="复制选中行", command=self._copy_selected)
        self.tree.bind("<Button-3>", lambda e: self._ctx_menu.post(e.x_root, e.y_root))
        self.tree.bind("<Control-c>", lambda e: self._copy_selected())

    def _rebuild_tree_columns(self):
        """根据当前可见列集合，重建 Treeview 的列定义（heading / width / anchor）。
        在「显示列」选择变化后调用，使新勾选的列真正出现在表格里。"""
        all_metric = self._all_metric_cols_list
        label2key = self._label2key
        # _visible_cols 存 key，列名为 label，需映射后再过滤
        visible_metrics = [c for c in all_metric
                           if label2key.get(c) in self._visible_cols]
        fixed_vis = [c for c in _FIXED_COLS if c in self._visible_fixed]
        cols = fixed_vis + visible_metrics
        self.cols = cols
        self.tree.configure(columns=cols)
        base_widths = {"#": 45, "设备": 70, "月份": 75, "路径": 260,
                       "处理时间": 140}
        for c in fixed_vis:
            if c in _SORTABLE_COLS:
                self.tree.heading(c, text=c, command=lambda c=c: self._on_heading_click(c))
            else:
                self.tree.heading(c, text=c)
            # 路径列 minwidth 仅防空路径塌陷；真实宽度由 _autosize_columns 按内容测量，
            # 不再设大下限，否则 width<minwidth 时被强制撑开产生空白
            min_w = 120 if c == "路径" else 40
            self.tree.column(c, width=base_widths[c], minwidth=min_w, anchor="w",
                             stretch=False)
        if "#" in cols:
            self.tree.column("#", anchor="center")
        if "大小" in cols:
            self.tree.column("大小", anchor="e")
        if "路径" in cols:
            self.tree.column("路径", stretch=False)
        for c in visible_metrics:
            if c in _SORTABLE_COLS:
                self.tree.heading(c, text=c, command=lambda c=c: self._on_heading_click(c))
            else:
                self.tree.heading(c, text=c)
            self.tree.column(c, width=self._col_width(c), minwidth=40,
                             anchor=self._col_anchor(c), stretch=False)
        # 列索引重建（排序用，与可见性无关的固定映射）
        self._col_index = self._make_col_index()
        # 清掉手动宽度记录（列结构已变，旧记录无意义）
        self._manual_widths = set()
        # 注意：此处仅重建列结构，不测列宽；列宽由调用方在 _refresh() 之后
        # 再调 _autosize_columns() 测量（必须基于新结构下的数据行，否则宽度错位）。

    def _on_filter_change(self):
        """筛选条件变化：回到第 0 页再刷新（避免停留在已无数据的旧页）。"""
        self._page = 0
        self._refresh()

    def _reset_filters(self):
        self.line_lb.selection_clear(0, "end")
        self.dev_lb.selection_clear(0, "end")
        self.ym_lb.selection_clear(0, "end")
        self.q_var.set("")
        self.stat_var.set("(全部)")
        self._on_filter_change()

    # ---------- 产线 → 设备 联动（单向） ----------
    def _on_line_change(self):
        """产线选择变化时，自动勾选/清空设备列表框中匹配的设备号（单向联动）。
        选中产线 -> 设备框只保留这些产线下的设备；(空) 设备不属于任何产线，故不选。
        """
        lines = set(self._sel(self.line_lb))
        self.dev_lb.selection_clear(0, "end")
        if lines:
            for i in range(self.dev_lb.size()):
                d = self.dev_lb.get(i)
                if d == "(空)":
                    continue
                if classify_line(d) in lines:
                    self.dev_lb.selection_set(i)
        self._on_filter_change()

    # ---------- 数值解析 ----------
    @staticmethod
    def _int(val: str):
        val = (val or "").strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            return "ERR"

    # ---------- 构建查询（筛选 + 通配符多选） ----------
    def _query_sql(self, limit=None, offset=0):
        dev_sel = self._sel(self.dev_lb)
        ym_sel = self._sel(self.ym_lb)
        q = self.q_var.get().strip()

        wheres, params = [], []
        # 设备：多选 + 支持 (空)
        if dev_sel:
            sub = []
            for d in dev_sel:
                if d == "(空)":
                    sub.append("p.device IS NULL")
                else:
                    sub.append("p.device=?")
                    params.append(d)
            wheres.append("(" + " OR ".join(sub) + ")")
        # 月份：多选 + 支持 (空)
        if ym_sel:
            sub = []
            for y in ym_sel:
                if y == "(空)":
                    sub.append("p.ym IS NULL")
                else:
                    sub.append("p.ym=?")
                    params.append(y)
            wheres.append("(" + " OR ".join(sub) + ")")
        # 路径：通配符 + 多条件(OR)
        if q:
            pats = [p.replace("*", "%").replace("?", "_")
                    for p in re.split(r"[|,;\s]+", q) if p]
            if pats:
                wheres.append("(" + " OR ".join(["p.path LIKE ?"] * len(pats)) + ")")
                params.extend(pats)

        where = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        base_exprs = ["p.device", "p.ym", "p.path", "p.ts"]
        metric_exprs = [d[0] for d in column_defs()]
        sel = base_exprs + metric_exprs
        sql = (f"SELECT {', '.join(sel)} FROM processed p {join_clause()} "
               f"{where} ORDER BY p.ym DESC, p.path")
        # 计数查询：复用同一 WHERE，但不要带上展示用的 LIMIT 占位符
        count_sql = f"SELECT COUNT(*) FROM processed p {join_clause()} {where}"
        count_params = list(params)
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params.append(limit)
            params.append(offset)
        return sql, params, count_sql, count_params

    # ---------- 刷新表格 ----------
    def _refresh(self):
        # 当前页偏移（受总上限 MAX_ROWS 约束，避免翻到不存在的页）
        offset = self._page * self._page_size
        # 本页实际取行数：不超过每页大小，且不超过总上限剩余
        limit = min(self._page_size, MAX_ROWS - offset)
        if limit <= 0:
            limit = 0
        sql, params, count_sql, count_params = self._query_sql(limit=limit, offset=offset)
        if sql is None:
            return
        try:
            c = self._conn()
            rows = c.execute(sql, params).fetchall() if limit else []
            # 真实总数（不受 LIMIT 影响），用于分页与状态栏提示
            total = c.execute(count_sql, count_params).fetchone()[0]
            c.close()
        except sqlite3.Error as e:
            messagebox.showerror("查询失败", str(e), parent=self.root)
            return

        self._raw_rows = rows
        self._total_count = total
        # 总页数：按「界面可浏览上限」与每页大小计算（超出 MAX_ROWS 的部分不可翻页浏览）
        _browse_total = min(total, MAX_ROWS)
        self._total_pages = max(1, (min(total, MAX_ROWS) + self._page_size - 1) // self._page_size)
        if self._page >= self._total_pages:
            self._page = self._total_pages - 1
            offset = self._page * self._page_size
            limit = min(self._page_size, MAX_ROWS - offset)
            sql2, params2, _, _ = self._query_sql(limit=limit, offset=offset)
            try:
                c = self._conn()
                rows = c.execute(sql2, params2).fetchall() if limit else []
                c.close()
            except sqlite3.Error:
                pass
            self._raw_rows = rows
        self._update_pager()
        self._render_rows()

    # ---------- 排序与渲染 ----------
    def _on_heading_click(self, name):
        """点击表头：同列则切换升/降序，异列则按该列升序。"""
        if self._sort_col == name:
            self._sort_dir = "desc" if self._sort_dir == "asc" else "asc"
        else:
            self._sort_col, self._sort_dir = name, "asc"
        self._render_rows()

    @staticmethod
    def _sort_key(v):
        """按类型排序：空值最前；数值按大小；其余按字符串。"""
        if v is None or v == "":
            return (0, 0)
        if isinstance(v, (int, float)):
            return (1, v)
        s = str(v)
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return (1, float(s))
        return (2, s)

    def _sorted_rows(self, rows):
        # 仅允许「有索引的列」排序；非可排序列一律按原顺序（数据库默认序）
        if not self._sort_col or self._sort_col not in _SORTABLE_COLS:
            return rows
        if self._sort_col == "#":
            indexed = list(enumerate(rows))
            indexed.sort(key=lambda iv: iv[0], reverse=(self._sort_dir == "desc"))
            return [iv[1] for iv in indexed]
        idx = self._col_index[self._sort_col]
        return sorted(rows, key=lambda r: self._sort_key(r[idx]),
                      reverse=(self._sort_dir == "desc"))

    def _render_rows(self):
        rows = self._raw_rows
        col_index = self._col_index   # 列名 -> row 元组下标（与可见性无关）
        ordered = self._sorted_rows(rows)
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(ordered, 1):
            vals = []
            for c in self.cols:       # 按当前可见列逐一从 row 取值
                if c == "#":
                    vals.append(i)
                elif c in ("设备", "月份", "处理时间"):
                    vals.append(row[col_index[c]] or "")
                elif c == "路径":
                    vals.append(row[col_index[c]])
                else:                 # 指标列（含"大小"）
                    vals.append(row[col_index[c]])
            self.tree.insert("", "end", values=tuple(vals),
                              tags=("oddrow",) if i % 2 == 0 else ())
        total = getattr(self, "_total_count", len(rows))
        shown = len(rows)
        # 分页信息：当前页显示的行区间（相对全量）
        if total == 0:
            self.stat_var.set("共 0 个文件")
        else:
            start = self._page * self._page_size + 1
            end = start + shown - 1
            if total > MAX_ROWS:
                self.stat_var.set(
                    f"显示第 {start}–{end} 条（界面上限 {MAX_ROWS} / 共 {total} 个文件，"
                    f"请用筛选缩小范围翻看后续）")
            else:
                self.stat_var.set(f"显示第 {start}–{end} 条（共 {total} 个文件）")
        self._update_headings()

    def _autosize_columns(self):
        """按「表头文字 + 当前可见行内容」取最大值撑开列宽，列宽严格贴合内容、不留多余空白。
        - 以 50px 为绝对下限，仅防「该列当前页几乎无内容」时塌陷；
        - 表头 / 内容均为实测宽度，长中文表头与长值自然撑开；
        - 路径列随窗口拉伸，不自动收缩；用户手动拖过的列不再自动调整。
        仅在列结构变化 / 首次打开时调用一次，避免每次筛选刷新都卡顿。"""
        import tkinter.font as tkfont
        # 缓存测量字体对象（避免每次重算都 new，进一步加速）
        if not hasattr(self, "_measure_font") or self._measure_font is None:
            # 测量字体与 Treeview 实际渲染字体一致（8 号）
            self._measure_font = tkfont.Font(font=("Microsoft YaHei", 8))
            self._measure_head_font = tkfont.Font(font=("Microsoft YaHei", 8, "bold"))
        font = self._measure_font
        head_font = self._measure_head_font
        PAD = 10          # 内容左右边距（贴合内容，避免右侧多余空白）
        HEAD_PAD = 14     # 表头额外余量（给排序箭头 ▲/▼ 及点击留位，保证标题不被截断）
        MIN_W = 50        # 绝对下限：仅防止该列当前页几乎无内容时列过窄
        SAMPLE = 200      # 采样当前显示的前 200 行测宽度（更全面，仍毫秒级）
        for col in self.cols:
            # 表头宽度（中文 bold 测量偏保守，加排序箭头占位与余量）
            head_w = head_font.measure(col) + head_font.measure("▲") + PAD + HEAD_PAD
            # 内容宽度：采样前若干行取最大值
            content_w = 0
            for iid in self.tree.get_children()[:SAMPLE]:
                v = self.tree.set(iid, col)
                if not v:
                    continue
                w = font.measure(v) + PAD
                if w > content_w:
                    content_w = w
            if col == "路径":
                # 路径列不设上限，按最长路径完整显示（stretch=False，超长时靠水平滚动条查看）
                best = max(MIN_W, head_w, content_w)
            else:
                # 列宽取「表头标题」与「最长内容」的较大者，确保标题完整显示、
                # 内容也不截断。不设 500 上限，否则长标题/长内容列会被压窄显示不全，
                # 超长时靠底部水平滚动条查看完整内容。
                best = max(head_w, content_w, MIN_W)
            self.tree.column(col, width=best)
        # 注意：路径列保持 stretch=False，宽度严格贴合内容（由上面按最长路径测量）。
        # 切不可把路径列设 stretch=True —— 那会把整列拉伸到填满窗口剩余宽度，
        # 造成"路径文字短但列很宽、文字右边一大片空白"的观感。
        # 长路径时总列宽会超出窗口，靠底部水平滚动条查看完整路径。

    def _on_col_resize(self):
        """用户拖动表头分隔线（separator）改列宽后，记录该列，刷新时不再自动调整它。

        只认 `separator` 区域（真正的拖宽边界）。不认 `heading`——
        否则点击表头排序也会把该列误加进 _manual_widths，导致它从此不再自适应
        （字符多的路径/品名/原品名等被点过表头后即固定宽度、显示不符）。
        """
        try:
            x = self.tree.winfo_pointerx() - self.tree.winfo_rootx()
            y = self.tree.winfo_pointery() - self.tree.winfo_rooty()
            if self.tree.identify("region", x, y) != "separator":
                return
            col = self.tree.identify_column(x)
            # col 形如 "#3"，转成列名
            idx = int(col[1:]) - 1
            if 0 <= idx < len(self.cols):
                self._manual_widths.add(self.cols[idx])
        except Exception:
            pass

    def _update_headings(self):
        """在表头标题后追加排序箭头 ▲/▼。"""
        for name in self.cols:
            arrow = " ▲" if self._sort_col == name and self._sort_dir == "asc" \
                else " ▼" if self._sort_col == name and self._sort_dir == "desc" \
                else ""
            self.tree.heading(name, text=name + arrow)

    # ---------- 分页 ----------
    def _update_pager(self):
        """刷新分页栏文字与按钮可用状态（由 _refresh 调用）。"""
        self._pg_label.config(text=f"第 {self._page + 1} / {self._total_pages} 页")
        at_first = self._page <= 0
        at_last = self._page >= self._total_pages - 1
        self._pg_first.state(["disabled"] if at_first else ["!disabled"])
        self._pg_prev.state(["disabled"] if at_first else ["!disabled"])
        self._pg_next.state(["disabled"] if at_last else ["!disabled"])
        self._pg_last.state(["disabled"] if at_last else ["!disabled"])

    def _goto_page(self, page):
        """跳转到指定页（0-based），越界自动夹紧，并刷新。"""
        if page < 0:
            page = 0
        if page > self._total_pages - 1:
            page = self._total_pages - 1
        if page == self._page:
            return
        self._page = page
        self._refresh()

    def _on_page_size_change(self, _ev=None):
        """修改每页大小：重置到第 0 页并重算总页数。"""
        try:
            size = int(self._pg_size.get())
        except ValueError:
            return
        if size <= 0:
            return
        self._page_size = size
        self._page = 0
        self._refresh()

    # ---------- 选中复制（整行） ----------
    def _copy_selected(self):
        """把当前选中的整行（支持多选）复制为 Tab 分隔文本，便于直接粘贴到 Excel。
        按表格显示顺序（从上到下）拼接；单元格值为 None 时输出空串。"""
        sel = set(self.tree.selection())
        if not sel:
            self.stat_var.set("未选中任何行")
            return
        lines = ["\t".join(self.cols)]            # 首行输出表头（列名）
        for iid in self.tree.get_children():      # 按显示顺序遍历
            if iid in sel:
                vals = self.tree.item(iid, "values")
                lines.append("\t".join("" if v is None else str(v) for v in vals))
        if not lines:
            return
        text = "\n".join(lines)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.stat_var.set(f"已复制 {len(lines)} 行到剪贴板")
        # 1.5 秒后恢复原有计数显示
        self.root.after(1500, lambda: self.stat_var.set(f"共 {len(self._raw_rows)} 个文件"))

    # ---------- 导出 CSV ----------
    def _load_export_cols(self):
        """读取上次导出的列选择（JSON）。返回选中列名集合；无/损坏则返回 None。"""
        try:
            with open(_EXPORT_COLS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(data)
        except Exception:
            pass
        return None

    def _save_export_cols(self, cols):
        """持久化本次导出的列选择。"""
        try:
            with open(_EXPORT_COLS_PATH, "w", encoding="utf-8") as f:
                json.dump(list(cols), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _pick_export_columns(self):
        """弹出列选择对话框，返回按原顺序选中的列名列表；取消或未选返回 None。
        选中的列会被持久化（下次默认沿用）。"""
        result = []
        saved = self._load_export_cols()

        dlg = tk.Toplevel(self.root)
        dlg.title("选择导出列")
        dlg.grab_set()          # 模态（不调用 transient，以保留最大/最小化按钮）
        dlg.resizable(True, True)
        dlg.geometry("820x680")  # 整体放大，避免勾选区被压缩、底部按钮被挤出
        dlg.withdraw()   # 先隐藏，避免默认位置闪现
        # 导出始终提供「全部列」选择（固定列 + 全量指标列），不受查看器可见列影响
        metric_cols = self._all_metric_cols_list
        frm = ttk.Frame(dlg, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="选择要导出的列（默认沿用上次选择）：").pack(anchor="w", pady=(0, 6))

        # ---- 固定列分组（横排，与「显示列」布局一致） ----
        fixed_frm = ttk.Frame(frm, borderwidth=1, relief="solid")
        fixed_frm.pack(fill="x", pady=(0, 8))
        fvars = {}
        _fixed_names = {"#": "序号", "设备": "设备", "月份": "月份",
                        "路径": "路径", "处理时间": "处理时间"}
        for i, col in enumerate(_FIXED_COLS):
            init = col in saved if saved is not None else True
            v = tk.BooleanVar(value=init)
            fvars[col] = v
            ttk.Checkbutton(fixed_frm, text=_fixed_names[col],
                            variable=v).grid(row=0, column=i, sticky="w",
                                             padx=8, pady=2)

        ttk.Label(frm, text="指标列：").pack(anchor="w", pady=(0, 6))

        # 可滚动的勾选区：自适应列数（参照主程序手动导出列），窗口拉伸时自动重排
        canv_frame = ttk.Frame(frm, borderwidth=1, relief="solid")
        canv_frame.pack(fill="both", expand=True, pady=(0, 8))
        canv = tk.Canvas(canv_frame, highlightthickness=0, width=760, height=420)
        sb = ttk.Scrollbar(canv_frame, orient="vertical", command=canv.yview)
        inner = ttk.Frame(canv)
        inner.bind("<Configure>",
                   lambda e: canv.configure(scrollregion=canv.bbox("all")))
        _win = canv.create_window((0, 0), window=inner, anchor="nw")
        canv.bind("<Configure>",
                  lambda e: canv.itemconfigure(_win, width=e.width))
        canv.configure(yscrollcommand=sb.set)
        canv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # 鼠标滚轮（中键/触控板）滚动
        canv.bind("<MouseWheel>",
                  lambda e: canv.yview_scroll(int(-e.delta / 120), "units"))
        canv.bind("<Button-4>",
                  lambda e: canv.yview_scroll(-1, "units"))
        canv.bind("<Button-5>",
                  lambda e: canv.yview_scroll(1, "units"))

        vars_ = {}
        # metric_cols 里是 label（中文显示名），需经 _label2key 反查回 METRICS 的 key
        l2k = getattr(self, "_label2key", {}) or {}
        _exp_widgets = []
        for label in metric_cols:
            key = l2k.get(label, label)
            init = label in saved if saved is not None else True
            v = tk.BooleanVar(value=init)
            vars_[label] = v
            wd = ttk.Checkbutton(inner, text="%s  (%s)" % (label, key), variable=v)
            _exp_widgets.append(wd)

        # 自适应列数：按实际测量的每列宽度估算每行列数，窗口/面板拉伸时自动重排
        # 不再用固定估算值(150)直接算，因为中文"名称 (key)"实际更宽，固定估算会溢出裁切最右列
        _GAP = 12   # 列间距
        def _measure_col_w():
            # 取所有复选框实际请求宽度中的最大值作为单列宽，保证该行每一列都完整放得下
            dlg.update_idletasks()
            _ws = [w.winfo_reqwidth() for w in _exp_widgets]
            return max(_ws) if _ws else 150
        def _relayout_exp_cols():
            _w = canv.winfo_width()
            _col_w = _measure_col_w()
            _n = max(1, _w // (_col_w + _GAP))
            for _i, _wd in enumerate(_exp_widgets):
                _wd.grid(row=_i // _n, column=_i % _n, sticky="w",
                         padx=(0, 12), pady=2)
            canv.configure(scrollregion=canv.bbox("all"))

        canv.bind("<Configure>", lambda e: _relayout_exp_cols())

        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill="x", pady=(8, 0))

        def select_all(val):
            for v in vars_.values():
                v.set(val)
            for v in fvars.values():
                v.set(val)

        def on_ok():
            sel = ([c for c in _FIXED_COLS if fvars[c].get()]
                   + [n for n in metric_cols if vars_[n].get()])
            result.extend(sel)
            self._save_export_cols(sel)   # 持久化
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        ttk.Button(btn_frm, text="全选", command=lambda: select_all(True)).pack(
            side="left", padx=(0, 6))
        ttk.Button(btn_frm, text="全不选", command=lambda: select_all(False)).pack(
            side="left", padx=(0, 6))
        ttk.Button(btn_frm, text="取消", command=on_cancel).pack(
            side="right", padx=(6, 0))
        ttk.Button(btn_frm, text="确定", command=on_ok, default="active").pack(side="right")
        # 居中于父窗口后再显示（消除闪烁）
        dlg.update_idletasks()
        _w, _h = dlg.winfo_width(), dlg.winfo_height()
        _x = self.root.winfo_rootx() + (self.root.winfo_width() - _w) // 2
        _y = self.root.winfo_rooty() + (self.root.winfo_height() - _h) // 2
        dlg.geometry("+%d+%d" % (max(0, _x), max(0, _y)))
        dlg.deiconify()
        dlg.lift()
        dlg.wait_window(dlg)
        return result or None

    def _pick_visible_columns(self):
        """『显示列』对话框：勾选查看器表格要显示的列（固定列 + 指标列均可勾选隐藏）。
        固定列横排在顶部，指标列可滚动区自适应列数（窗口拉伸自动重排）。"""
        all_cols = self._all_metric_cols()
        labels = {k: analysis.METRICS[k]["label"] for k in all_cols}
        dlg = tk.Toplevel(self.root)
        dlg.title("选择显示列")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(True, True)
        dlg.geometry("820x680")  # 整体放大，避免勾选区被压缩、底部按钮被挤出

        frm = ttk.Frame(dlg, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")
        dlg.rowconfigure(0, weight=1)
        dlg.columnconfigure(0, weight=1)

        # ---- 固定列分组（序号/设备/月份/路径/大小/处理时间，可勾选隐藏） ----
        ttk.Label(frm, text="固定列：").grid(row=0, column=0, sticky="w", pady=(0, 2))
        fixed_frm = ttk.Frame(frm, borderwidth=1, relief="solid")
        fixed_frm.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        fvars = {}
        _fixed_names = {"#": "序号", "设备": "设备", "月份": "月份",
                        "路径": "路径", "处理时间": "处理时间"}
        for i, col in enumerate(_FIXED_COLS):
            v = tk.BooleanVar(value=col in self._visible_fixed)
            fvars[col] = v
            ttk.Checkbutton(fixed_frm, text=_fixed_names[col],
                            variable=v).grid(row=0, column=i, sticky="w",
                                             padx=8, pady=2)

        ttk.Label(frm, text="指标列：").grid(row=2, column=0, sticky="w", pady=(0, 6))

        # 可滚动的勾选区（带边框）：自适应列数，窗口拉伸时自动重排；
        # 高度封顶 300px，再高则走滚动条——既紧凑（列少不留大片空白）又不撑高对话框。
        canv_frame = ttk.Frame(frm, borderwidth=1, relief="solid")
        canv_frame.grid(row=3, column=0, sticky="nsew")
        canv = tk.Canvas(canv_frame, highlightthickness=0, width=760, height=420)
        sb = ttk.Scrollbar(canv_frame, orient="vertical", command=canv.yview)
        inner = ttk.Frame(canv)
        inner.bind("<Configure>",
                   lambda e: canv.configure(scrollregion=canv.bbox("all")))
        _win = canv.create_window((0, 0), window=inner, anchor="nw")
        # 让内部 Frame 宽度随 Canvas 自适应，避免右侧内容被裁掉
        canv.bind("<Configure>",
                  lambda e: canv.itemconfigure(_win, width=e.width))
        canv.configure(yscrollcommand=sb.set)
        canv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        frm.rowconfigure(3, weight=1)
        frm.columnconfigure(0, weight=1)
        # 鼠标滚轮（中键/触控板）滚动
        canv.bind("<MouseWheel>",
                  lambda e: canv.yview_scroll(int(-e.delta / 120), "units"))
        canv.bind("<Button-4>",
                  lambda e: canv.yview_scroll(-1, "units"))
        canv.bind("<Button-5>",
                  lambda e: canv.yview_scroll(1, "units"))

        vars_ = {}
        _vis_widgets = []
        for col in all_cols:
            v = tk.BooleanVar(value=col in self._visible_cols)
            vars_[col] = v
            wd = ttk.Checkbutton(inner, text="%s  (%s)" % (labels[col], col),
                                 variable=v)
            _vis_widgets.append(wd)

        # 自适应列数：按实际测量的每列宽度估算每行列数，窗口/面板拉伸时自动重排
        # 不再用固定估算值(150)直接算，因为中文"名称 (key)"实际更宽，固定估算会溢出裁切最右列
        _GAP = 12   # 列间距
        def _measure_col_w():
            # 取所有复选框实际请求宽度中的最大值作为单列宽，保证该行每一列都完整放得下
            dlg.update_idletasks()
            _ws = [w.winfo_reqwidth() for w in _vis_widgets]
            return max(_ws) if _ws else 150
        def _relayout_vis_cols():
            _w = canv.winfo_width()
            _col_w = _measure_col_w()
            _n = max(1, _w // (_col_w + _GAP))
            for _i, _wd in enumerate(_vis_widgets):
                _wd.grid(row=_i // _n, column=_i % _n, sticky="w",
                         padx=(0, 12), pady=2)
            canv.configure(scrollregion=canv.bbox("all"))

        canv.bind("<Configure>", lambda e: _relayout_vis_cols())

        bf = ttk.Frame(frm)
        bf.grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="ew")

        def _set_all(val):
            for v in vars_.values():
                v.set(val)
            for v in fvars.values():
                v.set(val)

        def _apply():
            sel = [c for c in all_cols if vars_[c].get()]
            self._visible_cols = set(sel)
            self._visible_fixed = {c for c in _FIXED_COLS if fvars[c].get()}
            self._save_visible_cols()
            dlg.destroy()
            self._rebuild_tree_columns()
            self._refresh()
            # 延迟到布局完成后再测宽：对话框关闭后同帧内 Treeview 尚未完成几何布局，
            # 此时 font.measure 测不准（尤其中文表头），会导致列宽未按真实渲染撑开。
            self.root.after_idle(self._autosize_columns)

        def _cancel():
            dlg.destroy()

        ttk.Button(bf, text="全选", command=lambda: _set_all(True)).grid(row=0, column=0, padx=3)
        ttk.Button(bf, text="全不选", command=lambda: _set_all(False)).grid(row=0, column=1, padx=3)
        ttk.Button(bf, text="取消", command=_cancel).grid(row=0, column=2, padx=(20, 3))
        ttk.Button(bf, text="确定", command=_apply, default="active").grid(row=0, column=3, padx=3)

        dlg.bind("<Return>", lambda e: _apply())
        dlg.bind("<Escape>", lambda e: _cancel())

        dlg.update_idletasks()
        _w, _h = dlg.winfo_width(), dlg.winfo_height()
        _x = self.root.winfo_rootx() + (self.root.winfo_width() - _w) // 2
        _y = self.root.winfo_rooty() + (self.root.winfo_height() - _h) // 2
        dlg.geometry("%dx%d+%d+%d" % (_w, _h, max(0, _x), max(0, _y)))
        dlg.deiconify()
        dlg.lift()
        dlg.wait_window(dlg)

    def _open_archive_dialog(self):
        """打开归档设置对话框（可设保留月份、预览、手动/自动归档）。"""
        ArchiveDialog(self.root, self)

    def _export_csv(self):
        sql, params, _count_sql, _count_params = self._query_sql()   # 导出不加 LIMIT，导出全部匹配行
        if sql is None:
            return
        try:
            c = self._conn()
            rows = c.execute(sql, params).fetchall()
            c.close()
        except sqlite3.Error as e:
            messagebox.showerror("导出失败", str(e), parent=self.root)
            return
        if not rows:
            messagebox.showinfo("导出", "当前筛选无数据，未导出。", parent=self.root)
            return

        # 先让用户选择要导出的列
        sel = self._pick_export_columns()
        if sel is None:
            return

        fn = filedialog.asksaveasfilename(
            title="导出 CSV",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile="scan_state_export_%s.csv"
                       % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        if not fn:
            return
        try:
            metric_labels = [d[1] for d in column_defs()]

            def header_of(name):
                return "序号" if name == "#" else name

            def value_of(name, i, row):
                if name == "#":
                    return i
                if name == "设备":
                    return row[0] or ""
                if name == "月份":
                    return row[1] or ""
                if name == "路径":
                    return row[2]
                if name == "大小":
                    return row[3] if row[3] is not None else ""
                if name == "处理时间":
                    return row[4] or ""
                k = metric_labels.index(name)  # 指标列
                v = row[5 + k]
                return v if v is not None else ""

            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow([header_of(n) for n in sel])
                for i, row in enumerate(rows, 1):
                    w.writerow([value_of(n, i, row) for n in sel])
            messagebox.showinfo("导出成功", f"已导出 {len(rows)} 行、{len(sel)} 列到：\n{fn}", parent=self.root)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self.root)


def open_db_view(parent=None):
    """打开查看器。parent 为 None 时独立运行(自建 Tk + mainloop)；
    传入主窗口则作为内嵌 Toplevel 打开（共享主程序事件循环）。"""
    win = tk.Toplevel(parent) if parent is not None else tk.Tk()
    # 不调用 transient：Windows 上 transient 会把窗口变成「拥有的工具窗口」，
    # 导致标题栏只剩关闭、没有最大/最小化按钮。改用 resizable + lift 保留按钮且可缩放。
    win.resizable(True, True)
    win.withdraw()   # 先隐藏，避免默认位置闪现后再跳到居中
    DBViewApp(win)
    # 居中：有 parent 则居中于父窗口，否则居中于屏幕
    win.update_idletasks()
    _w, _h = win.winfo_width(), win.winfo_height()
    if parent is not None:
        _x = parent.winfo_rootx() + (parent.winfo_width() - _w) // 2
        _y = parent.winfo_rooty() + (parent.winfo_height() - _h) // 2
    else:
        _x = (win.winfo_screenwidth() - _w) // 2
        _y = (win.winfo_screenheight() - _h) // 2
    win.geometry("+%d+%d" % (max(0, _x), max(0, _y)))
    win.lift()
    win.deiconify()   # 定位完毕后再显示，消除闪烁
    if parent is None:
        win.mainloop()
    return win


def _enable_high_dpi():
    """独立运行时声明 DPI 感知（内嵌由主程序已声明），使高分屏不模糊。"""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    _enable_high_dpi()  # 必须在创建 Tk 之前
    open_db_view(None)  # parent=None 时自建 Tk 并 mainloop


if __name__ == "__main__":
    main()


class ArchiveDialog(tk.Toplevel):
    """归档设置对话框：设定保留月份、预览、手动/自动归档。"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.title("归档旧数据设置")
        self.resizable(False, False)
        self.transient(parent)   # 置顶于父窗口并相对父窗口定位
        self._auto_after_id = None
        self._last_auto_run = 0.0
        self._popup = True
        self._build()
        # 居中于父窗口（控件布局完成后再取尺寸，避免偏出主界面）
        self.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + (ph - h) // 2)
        self.geometry("+%d+%d" % (x, y))

    def _build(self):
        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")

        # 保留月份
        ttk.Label(frm, text="保留最近月份数：").grid(row=0, column=0, sticky="w", pady=2)
        self.km_var = tk.StringVar(value=str(db_maintain.KEEP_MONTHS))
        ttk.Entry(frm, textvariable=self.km_var, width=8).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(frm, text="（数据月份 < 当前月 - 此值 的记录将被归档）").grid(row=0, column=2, sticky="w")

        # 预览
        ttk.Button(frm, text="预览将归档数量", command=self._preview).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=6)
        self.prev_var = tk.StringVar(value="（点上方预览）")
        ttk.Label(frm, textvariable=self.prev_var, foreground="#1a73e8").grid(
            row=2, column=0, columnspan=3, sticky="w")

        # 手动归档
        ttk.Button(frm, text="立即归档（手动）", command=self._manual).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=6)

        # 自动归档
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="启用自动归档", variable=self.auto_var,
                        command=self._on_auto_toggle).grid(row=4, column=0, sticky="w")
        ttk.Label(frm, text="检查频率(天)：").grid(row=4, column=1, sticky="w")
        self.interval_var = tk.StringVar(value="1")
        ttk.Entry(frm, textvariable=self.interval_var, width=6).grid(
            row=4, column=2, sticky="w", padx=4)
        ttk.Label(frm, text="（多久查一次是否超期；归档本身按「保留月份」条件触发，非定时）").grid(
            row=5, column=0, columnspan=3, sticky="w")

        self.auto_stat_var = tk.StringVar(value="自动归档：未启用")
        ttk.Label(frm, textvariable=self.auto_stat_var, foreground="#666").grid(
            row=6, column=0, columnspan=3, sticky="w")

        ttk.Button(frm, text="关闭", command=self.destroy).grid(
            row=7, column=2, sticky="e", pady=10)

    def _valid_km(self):
        try:
            v = int(self.km_var.get())
            if v < 1:
                raise ValueError
            return v
        except Exception:
            messagebox.showerror("参数错误", "保留月份数必须为正整数。", parent=self)
            return None

    def _valid_interval(self):
        try:
            v = float(self.interval_var.get())
            if v < 1:
                raise ValueError
            return v
        except Exception:
            return 1.0

    def _check_ms(self):
        # 界面按「天」设置，转成毫秒（1 天 = 86400 秒）
        return int(self._valid_interval() * 86400 * 1000)

    def _preview(self):
        km = self._valid_km()
        if km is None:
            return
        old, keep, cut, no_ym = db_maintain.preview_old(km)
        if cut is None:
            self.prev_var.set("未找到数据库文件。")
            return
        self.prev_var.set("数据月份 < %s 将被归档：旧 %d 条，保留 %d 条%s"
                          % (cut, old, keep,
                             ("（其中 %d 条无月份保留）" % no_ym) if no_ym else ""))

    def _manual(self):
        km = self._valid_km()
        if km is None:
            return
        if not messagebox.askyesno(
                "确认归档",
                "将归档数据月份 < 当前月 - %d 的记录并删除主库旧数据（先整库备份）。继续？"
                % km, parent=self):
            return
        self._run_archive(km, popup=True)

    def _run_archive(self, km, popup):
        self._popup = popup
        self.config(cursor="watch")
        logs = []

        def _worker():
            try:
                summary = db_maintain.run_maintain(log_cb=logs.append, keep_months=km)
            except Exception as e:
                summary = "归档出错：%s" % e
                logs.append(summary)
            self.after(0, self._on_done, summary, logs)

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _on_done(self, summary, logs):
        self.config(cursor="")
        try:
            self.app._refresh()
        except Exception:
            pass
        tail = "\n".join(logs[-6:])
        if self._popup:
            messagebox.showinfo("归档结果", "%s\n\n%s" % (summary, tail), parent=self)
        else:
            self.auto_stat_var.set("上次自动归档：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    def _on_auto_toggle(self):
        if self.auto_var.get():
            self._schedule_auto()
        else:
            self._cancel_auto()
            self.auto_stat_var.set("自动归档：未启用")

    def _schedule_auto(self):
        self._cancel_auto()
        self._auto_tick()

    def _cancel_auto(self):
        if self._auto_after_id is not None:
            self.after_cancel(self._auto_after_id)
            self._auto_after_id = None

    def _auto_tick(self):
        km = self._valid_km()
        if km is None:
            self.auto_var.set(False)
            self.auto_stat_var.set("自动归档：参数错误已停用")
            return
        ms = self._check_ms()
        try:
            old_cnt, _keep, _cut, _no_ym = db_maintain.preview_old(km)
        except sqlite3.Error as e:
            self.auto_stat_var.set("自动检查出错：%s" % e)
            self._auto_after_id = self.after(ms, self._auto_tick)
            return
        if old_cnt > 0:
            # 存在超过保存月数的数据 → 归档（而非按固定周期无脑执行）
            self.auto_stat_var.set("发现 %d 条超期数据，已触发归档…" % old_cnt)
            self._run_archive(km, popup=False)
        else:
            self.auto_stat_var.set("当前无超期数据，无需归档（检查频率 %d 天）" % int(self._valid_interval()))
        self._auto_after_id = self.after(ms, self._auto_tick)

    def destroy(self):
        self._cancel_auto()
        super().destroy()
