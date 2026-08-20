#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui_common.py —— 跨窗口共享的 UI 常量与组件。

1) 状态色（统一语义，替代各窗口零散的硬编码颜色）：
   COLOR_INFO 进行中/强调（蓝）、COLOR_OK 成功/运行（绿）、
   COLOR_WARN 警告/暂停（橙）、COLOR_ERR 失败/停止（红）、COLOR_DIM 次要说明（灰）。

2) LineDeviceMonthFilter：产线-设备-月份筛选组件（数据库查看器与任务中心共用）。
   形态：产线复选框行（联动设备）+ 设备/月份多选 Listbox + 全选/清空按钮。
   统一两处此前各自实现、行为易漂移的问题；产线联动逻辑只此一份。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import analysis   # classify_line：设备号 → 产线

# ---- 状态色（统一语义） ----
COLOR_INFO = "#1a73e8"   # 进行中 / 强调
COLOR_OK = "#0a7d2c"     # 成功 / 运行中
COLOR_WARN = "#e07b00"   # 警告 / 暂停
COLOR_ERR = "#d11a1a"    # 失败 / 停止
COLOR_DIM = "#666666"    # 次要说明文字

_ALL_LINES = ("E", "C", "D", "A")


class LineDeviceMonthFilter(ttk.Frame):
    """产线-设备-月份 三级筛选（查看器与任务中心共用）。

    - 产线：复选框（默认 E/C/D/A，可用 lines 参数只显示实际存在的产线）；
      勾选变化自动联动设备框（只选中对应产线设备，单向）。
    - 设备 / 月份：多选 Listbox（Ctrl/Shift 连选）；不选 = 不限。
    - on_change：任一筛选变化时回调（查看器刷新表格 / 任务中心更新提示）。
    - status_var：可选 StringVar，显示在产线行右侧（选择摘要由外部更新）。

    数据由 set_devices()/set_months() 填充（查看器传入 "(空)" 条目表示 NULL；
    产线联动时自动跳过 "(空)"）。读取用 selected_lines/selected_devices/selected_months。
    """

    def __init__(self, parent, on_change=None, status_var=None, lines=None):
        super().__init__(parent, padding=(8, 4))
        self._on_change = on_change
        shown_lines = tuple(lines) if lines else _ALL_LINES

        # 组件内自备按钮小样式（幂等；任务中心定义过则覆盖为同值）
        try:
            ttk.Style().configure("Small.TButton",
                                  font=("Microsoft YaHei", 9), padding=(2, 2))
        except Exception:
            pass

        # ---- 产线行 ----
        lf = ttk.Frame(self)
        lf.pack(fill="x", pady=(2, 4))
        ttk.Label(lf, text="产线：").pack(side="left")
        self._line_vars = {}
        for ln in shown_lines:
            v = tk.BooleanVar()
            self._line_vars[ln] = v
            ttk.Checkbutton(lf, text=ln, variable=v,
                            command=self._on_line_toggle).pack(side="left", padx=2)
        if status_var is not None:
            ttk.Label(lf, textvariable=status_var, foreground=COLOR_INFO).pack(
                side="left", padx=(14, 0))

        # ---- 设备 / 月份 双列 ----
        mid = ttk.Frame(self)
        mid.pack(fill="x", pady=2)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        dev_frame = ttk.LabelFrame(mid, text="设备（可多选）")
        dev_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.dev_listbox = tk.Listbox(dev_frame, selectmode="extended", height=5,
                                      exportselection=0, borderwidth=0, relief="flat")
        dev_sb = ttk.Scrollbar(dev_frame, orient="vertical", command=self.dev_listbox.yview)
        self.dev_listbox.pack(side="left", fill="both", expand=True, pady=2)
        dev_sb.pack(side="right", fill="y")
        self.dev_listbox.config(yscrollcommand=dev_sb.set)
        self.dev_listbox.bind("<<ListboxSelect>>", lambda e: self._changed())

        mth_frame = ttk.LabelFrame(mid, text="月份（可多选，Ctrl/Shift 连选）")
        mth_frame.grid(row=0, column=1, sticky="nsew")
        self.mth_listbox = tk.Listbox(mth_frame, selectmode="extended", height=5,
                                      exportselection=0, borderwidth=0, relief="flat")
        mth_sb = ttk.Scrollbar(mth_frame, orient="vertical", command=self.mth_listbox.yview)
        self.mth_listbox.pack(side="left", fill="both", expand=True, pady=2)
        mth_sb.pack(side="right", fill="y")
        self.mth_listbox.config(yscrollcommand=mth_sb.set)
        self.mth_listbox.bind("<<ListboxSelect>>", lambda e: self._changed())

        # ---- 批量按钮 ----
        bf = ttk.Frame(self)
        bf.pack(fill="x", pady=(2, 0))
        for text, cmd in (
                ("全选设备", lambda: self._set_all(self.dev_listbox, True)),
                ("清设备", lambda: self._set_all(self.dev_listbox, False)),
                ("全选月", lambda: self._set_all(self.mth_listbox, True)),
                ("清月", lambda: self._set_all(self.mth_listbox, False))):
            ttk.Button(bf, text=text, style="Small.TButton", command=cmd).pack(
                side="left", padx=2)

    # ---- 内部 ----
    def _changed(self):
        if self._on_change:
            self._on_change()

    def _on_line_toggle(self):
        self.apply_line()
        self._changed()

    @staticmethod
    def _set_all(lb, select):
        if select:
            lb.selection_set(0, "end")
        else:
            lb.selection_clear(0, "end")

    # ---- 数据填充 ----
    def set_devices(self, devs):
        self.dev_listbox.delete(0, "end")
        for d in devs:
            self.dev_listbox.insert("end", d)

    def set_months(self, yms):
        self.mth_listbox.delete(0, "end")
        for ym in yms:
            self.mth_listbox.insert("end", ym)

    # ---- 读取 ----
    def selected_lines(self):
        return [ln for ln, v in self._line_vars.items() if v.get()]

    def selected_devices(self):
        return [self.dev_listbox.get(i) for i in self.dev_listbox.curselection()]

    def selected_months(self):
        return [self.mth_listbox.get(i) for i in self.mth_listbox.curselection()]

    def device_items(self):
        """设备框全部条目（含 "(空)"），供产线→设备展开遍历。"""
        return [self.dev_listbox.get(i) for i in range(self.dev_listbox.size())]

    # ---- 联动 / 重置 ----
    def apply_line(self):
        """产线联动（单向）：勾了产线 → 设备框只保留对应产线设备；全不勾 → 清设备选择。
        "(空)" 设备不属于任何产线，联动时不选。"""
        lines = set(self.selected_lines())
        self.dev_listbox.selection_clear(0, "end")
        if not lines:
            return
        for i in range(self.dev_listbox.size()):
            d = self.dev_listbox.get(i)
            if d == "(空)":
                continue
            if analysis.classify_line(d) in lines:
                self.dev_listbox.selection_set(i)

    def reset(self):
        """清空全部筛选（产线/设备/月份）。"""
        for v in self._line_vars.values():
            v.set(False)
        self._set_all(self.dev_listbox, False)
        self._set_all(self.mth_listbox, False)
