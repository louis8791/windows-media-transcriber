"""Local click-to-start menu. Session options never change config.local.json."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid
import queue
import threading
import re

ROOT = Path(__file__).resolve().parents[1]
MEDIA_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.mp4', '.aac', '.flac', '.mov', '.mkv',
                    '.avi', '.wmv', '.webm', '.ogg', '.wma', '.ts', '.mts', '.m2ts', '.3gp'}
MODES = {
    'normal': ('一般轉錄', '只處理新音檔。\n已完成的音檔自動略過。'),
    'force': ('重新轉錄', '重新辨識勾選的音檔。\n開始前先備份舊稿。'),
    'retry': ('補回文字檔／重試校稿', '沿用已存的辨識結果。\n不重跑音訊辨識。'),
    'proofread': ('只校稿文字檔', '選擇 TXT／SRT 文字檔。\n另存校稿候選，保留原稿。'),
    'preview': ('預覽文字稿', '勾選文字稿，在本視窗閱讀內容；不修改檔案。'),
    'listen': ('點文字聽原音', '選含時間碼 TXT／SRT，點一句播放原音，可慢速與重播。'),
    'gapcheck': ('漏句檢查', '選音檔，檢查有語音卻沒有字幕的區段，點疑點聽原音；不自動補字。'),
    'plain': ('字幕轉純文字', '移除 SRT 序號與時間碼，另存純文字；原字幕保留。'),
    'completed': ('已完成音檔紀錄', '查看已登記的來源、完成時間與證據；文字稿改名仍可略過。'),
    'status': ('工作狀態', '查看各音檔最近的處理狀態與錯誤，方便確認尚未完成的工作。'),
    'reports': ('閱讀審查報告', '在本視窗閱讀已產生的審查報告；報告不代表人工聽校完成。'),
    'backups': ('查看重轉備份', '查看重新轉錄前保留的舊稿與內容；不覆寫目前稿件。'),
    'logs': ('閱讀執行紀錄', '在本視窗查看執行紀錄，排查中斷或錯誤原因。'),
    'settings': ('目前設定與工具路徑', '查看目前模型、輸入輸出位置與工具是否存在；不變更永久設定。'),
}
CATEGORIES = {'音檔轉錄': ('normal', 'force', 'retry'),
              '文字稿處理': ('proofread', 'preview', 'plain', 'listen', 'gapcheck'),
              '完成與備份': ('completed', 'status', 'reports', 'backups'),
              '設定與紀錄': ('logs', 'settings')}
TEXT_MODES = {'proofread', 'preview', 'plain', 'reports', 'backups', 'logs', 'completed', 'status', 'listen'}
UTILITY_MODES = set(MODES) - {'normal', 'force', 'retry', 'proofread'}
OUTPUTS = {'timeline': '含時間碼 TXT', 'txt': '純文字 TXT', 'srt': 'SRT 字幕',
           'review': '審查報告', 'cleaned': 'AI 校稿文字'}
OUTPUT_HELP = {
    'timeline': '文字附上每段的開始與結束時間，方便回到音檔核對內容。',
    'txt': '只保留文字，不附時間碼，適合閱讀、複製與後續編輯。',
    'srt': 'SRT 是字幕檔，包含文字與顯示時間；可匯入支援 SRT 的播放器或剪輯軟體，搭配影音顯示字幕。',
    'review': '列出程式偵測到的重複內容、時間碼或其他品質疑點，方便檢查；不代表已完成人工聽校。',
    'cleaned': '由 AI 修正文字並另存校稿候選，保留原稿；人名、數字與專有名詞仍需對照音檔確認。',
}
COLORS = dict(background='#f6f5f1', panel='#ffffff', text='#25343b', muted='#566870',
              accent='#0e7490', hover='#155e75', border='#d3dce0', warning='#805400')
LANGUAGES = {'中文': 'zh', '英文（保留英文原文）': 'en'}


def scan_files(folder: Path, text_only=False) -> list[Path]:
    if not folder.is_dir():
        return []
    if not text_only:
        return sorted((p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS),
                      key=lambda p: str(p).casefold())
    excluded = ('.cleaned.txt', '校稿進度.txt', '轉錄進度.txt', '審查報告.txt', '.partial.txt')
    return sorted((p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in {'.txt', '.srt'}
                   and not p.name.endswith(excluded) and '.轉錄中' not in p.name),
                  key=lambda p: str(p).casefold())


def make_session(config: dict, mode: str, paths: list[Path], all_media: bool,
                 watch: bool, outputs: list[str], language: str | None = None,
                 recheck: bool = False) -> tuple[dict, list[str]]:
    """Validate before writing a session or starting the engine."""
    if mode not in {'normal', 'force', 'retry'}:
        raise ValueError('請選擇轉錄工作。')
    if not outputs:
        raise ValueError('請至少選一種輸出格式。')
    if mode != 'normal' and all_media:
        raise ValueError('重新轉錄與補回稿件必須明確勾選音檔。')
    if not all_media and not paths:
        raise ValueError('請勾選至少一支音檔。')
    input_root = Path(config['desktop_input_dir']).resolve()
    for path in paths:
        if not path.is_file() or not path.resolve().is_relative_to(input_root):
            raise ValueError(f'音檔已移動或不在音檔資料夾內：{path.name}，請重新整理清單。')
        if mode == 'retry':
            base = path.resolve().relative_to(input_root).with_suffix('')
            final = Path(config['workspace_dir']) / 'output_final' / base
            if not all(Path(str(final) + ext).is_file() and Path(str(final) + ext).stat().st_size
                       for ext in ('.txt', '.srt')):
                raise ValueError(f'「{path.name}」沒有可補回的完整稿，請改選一般轉錄。')
    session = dict(config)
    if language is not None:
        if language not in LANGUAGES.values():
            raise ValueError('請選擇中文或英文。')
        session['language'] = language
    session['asr_recheck'] = bool(recheck) and mode != 'retry'
    session.update(desktop_outputs=outputs, selected_input_paths=[] if all_media else [str(p.resolve()) for p in paths],
                   use_ollama_cleanup='auto' if 'cleaned' in outputs else 'false', reuse_only=mode == 'retry',
                   backup_before_delivery=mode == 'retry')
    arguments = [] if mode == 'normal' and all_media and watch else ['-Once', '-StableMinutes', '0']
    if mode == 'force':
        arguments.append('-Force')
    elif mode == 'retry':
        arguments.append('-RetryCleanup')
    return session, arguments


class LaunchMenu:
    def __init__(self, window: tk.Tk, config: dict):
        self.window, self.config, self.request = window, config, None
        self.events = queue.Queue()
        self.process = None
        self.running = False
        self.stopping = False
        self.mode = tk.StringVar(value='normal')
        self.all_media = tk.BooleanVar(value=True)
        self.watch = tk.BooleanVar(value=True)
        self.search = tk.StringVar()
        self.summary = tk.StringVar()
        self.language = tk.StringVar(value=next((k for k, v in LANGUAGES.items() if v == config.get('language')), '中文'))
        self.recheck = tk.BooleanVar(value=True)
        self.selection: set[Path] = set()
        self.files: list[Path] = []
        tokens = config.get('desktop_outputs', ['timeline'])
        self.outputs = {token: tk.BooleanVar(value=token in tokens) for token in OUTPUTS}
        self.input = Path(config['desktop_input_dir'])
        self.output = Path(config['desktop_output_dir'])
        self._style()
        window.title('影音轉文字｜開始工作')
        width, height = min(1120, window.winfo_screenwidth() - 80), min(800, window.winfo_screenheight() - 80)
        window.geometry(f'{width}x{height}+{(window.winfo_screenwidth()-width)//2}+{(window.winfo_screenheight()-height)//2}')
        window.minsize(min(880, width), min(700, height))
        window.configure(background=COLORS['background'])
        window.protocol('WM_DELETE_WINDOW', self.cancel)
        outer = ttk.Frame(window, padding=24)
        self.outer = outer
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        header = ttk.Frame(outer)
        self.header = header
        header.grid(row=0, column=0, sticky='ew')
        ttk.Label(header, text='這次想做什麼？', style='Title.TLabel').pack(side='left')
        ttk.Button(header, text='文字檔資料夾', command=lambda: self.open_folder(self.output)).pack(side='right')
        ttk.Button(header, text='音檔資料夾', command=lambda: self.open_folder(self.input)).pack(side='right', padx=8)
        ttk.Label(outer, text='選擇工作與檔案，再按「開始」。設定只套用這一次。', style='Muted.TLabel').grid(row=1, column=0, sticky='w', pady=(8, 20))
        body = ttk.Frame(outer)
        self.body = body
        body.grid(row=2, column=0, sticky='nsew')
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        tasks = ttk.LabelFrame(body, text='① 選擇工作', padding=16)
        self.tasks = tasks
        tasks.grid(row=0, column=0, sticky='ns', padx=(0, 20))
        self.mode_buttons = {}
        categories = ttk.Notebook(tasks)
        categories.pack(fill='both', expand=True)
        pages = {}
        for category, modes in CATEGORIES.items():
            page = ttk.Frame(categories, padding=8)
            pages[category] = page
            categories.add(page, text=category)
            for mode in modes:
                title, description = MODES[mode]
                button = ttk.Radiobutton(page, text=title, variable=self.mode, value=mode, command=self.change_mode)
                button.pack(anchor='w', pady=(8, 2))
                ttk.Label(page, text=description, style='Muted.TLabel', wraplength=300).pack(anchor='w', pady=(0, 8))
                self.mode_buttons[mode] = button
        def select_category(_):
            if not categories.winfo_ismapped():
                return
            category = categories.tab(categories.select(), 'text')
            self.mode.set(CATEGORIES[category][0])
            self.change_mode()
        categories.bind('<<NotebookTabChanged>>', select_category)
        self.description = ttk.Label(tasks, style='Muted.TLabel', wraplength=250)
        selection = ttk.LabelFrame(body, text='② 選擇檔案', padding=16)
        self.file_panel = selection
        selection.grid(row=0, column=1, sticky='nsew')
        selection.columnconfigure(0, weight=1)
        selection.rowconfigure(3, weight=1)
        scope = ttk.Frame(selection)
        self.scope_panel = scope
        scope.grid(row=0, column=0, sticky='ew')
        self.all_button = ttk.Radiobutton(scope, text='全部音檔', variable=self.all_media, value=True, command=self.update_state)
        self.all_button.pack(side='left', padx=(0, 16))
        self.selected_button = ttk.Radiobutton(scope, text='只處理勾選檔案', variable=self.all_media, value=False, command=self.update_state)
        self.selected_button.pack(side='left')
        self.watch_button = ttk.Checkbutton(pages['音檔轉錄'], text='持續監控新音檔', variable=self.watch, command=self.update_state)
        self.watch_button.pack(anchor='w', pady=(10, 4))
        ttk.Label(pages['音檔轉錄'], text='按「停止工作」或關閉視窗即停止。', style='Muted.TLabel').pack(anchor='w')
        tools = ttk.Frame(selection)
        tools.grid(row=2, column=0, sticky='ew', pady=(0, 12))
        tools.columnconfigure(1, weight=1)
        ttk.Label(tools, text='搜尋').grid(row=0, column=0, padx=(0, 8))
        self.search_entry = ttk.Entry(tools, textvariable=self.search)
        self.search_entry.grid(row=0, column=1, sticky='ew', padx=(0, 8))
        ttk.Button(tools, text='重新整理', command=self.refresh).grid(row=0, column=2)
        self.search.trace_add('write', lambda *_: self.draw_files())
        table = ttk.Frame(selection)
        table.grid(row=3, column=0, sticky='nsew')
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=('check', 'name', 'size'), show='headings', selectmode='none')
        for column, text, size in (('check', '選擇', 48), ('name', '檔名（含子資料夾）', 640), ('size', '大小', 80)):
            self.tree.heading(column, text=text)
            self.tree.column(column, width=size, minwidth=size if column != 'name' else 240, stretch=column == 'name')
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.bind('<Button-1>', self.toggle_file)
        self.tree.bind('<space>', self.toggle_file)
        actions = ttk.Frame(selection)
        actions.grid(row=4, column=0, sticky='ew', pady=(12, 0))
        ttk.Button(actions, text='勾選清單', command=self.select_visible).pack(side='left')
        ttk.Button(actions, text='清除勾選', command=self.clear_selection).pack(side='left', padx=8)
        self.browse_button = ttk.Button(actions, text='另外選文字檔…', command=self.browse_text)
        self.browse_button.pack(side='right')
        self.empty_label = ttk.Label(selection, style='Muted.TLabel', wraplength=520)
        self.empty_label.grid(row=5, column=0, sticky='w', pady=(8, 0))
        result_frame = ttk.Frame(selection)
        self.inline_result = result_frame
        result_frame.grid(row=6, column=0, sticky='ew', pady=(10, 0))
        self.result = tk.Text(result_frame, height=5, wrap='word', font=('Microsoft JhengHei', 11), state='disabled')
        self.result.pack(side='left', fill='both', expand=True)
        result_scroll = ttk.Scrollbar(result_frame, command=self.result.yview)
        result_scroll.pack(side='right', fill='y')
        self.result.configure(yscrollcommand=result_scroll.set)
        formats = ttk.LabelFrame(outer, text='③ 這次要輸出哪些檔案？', padding=16)
        self.formats_panel = formats
        formats.grid(row=3, column=0, sticky='ew', pady=(20, 16))
        self.output_buttons = []
        for token, text in OUTPUTS.items():
            button = ttk.Checkbutton(formats, text=text, variable=self.outputs[token], command=self.update_state)
            button.pack(side='left', padx=(0, 20))
            self.output_buttons.append(button)
        self.asr_options = ttk.Frame(formats)
        ttk.Label(self.asr_options, text='音檔語言').pack(side='left', padx=(0, 10))
        ttk.Combobox(self.asr_options, textvariable=self.language, values=list(LANGUAGES),
                     state='readonly', width=27).pack(side='left')
        ttk.Checkbutton(self.asr_options, text='自動重查疑點', variable=self.recheck).pack(side='left', padx=20)
        self.asr_help = ttk.Label(formats, text='英文保留原文，不自動翻中文。重查會增加等待時間，另存比對報告，不覆寫原始辨識稿。',
                                  style='Muted.TLabel', wraplength=780)
        self.summary_label = ttk.Label(outer, textvariable=self.summary, wraplength=1020, style='Summary.TLabel')
        self.summary_label.grid(row=4, column=0, sticky='ew', pady=(0, 16))
        footer = ttk.Frame(outer)
        self.footer = footer
        footer.grid(row=5, column=0, sticky='ew')
        ttk.Label(footer, text='AI 校稿會另存候選，原文字稿保持不變。', style='Muted.TLabel').pack(side='left')
        self.start_button = ttk.Button(footer, text='開始', style='Primary.TButton', command=self.begin)
        self.start_button.pack(side='right')
        ttk.Button(footer, text='取消', command=self.cancel).pack(side='right', padx=12)
        self.stop_button = ttk.Button(footer, text='停止工作', command=self.stop, state='disabled')
        self.stop_button.pack(side='right', padx=8)
        window.bind('<Escape>', lambda _: self.cancel())
        self.refresh()

    def _style(self):
        style = ttk.Style(self.window)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft JhengHei', 12), background=COLORS['background'], foreground=COLORS['text'])
        style.configure('Title.TLabel', font=('Microsoft JhengHei', 22, 'bold'))
        style.configure('Muted.TLabel', foreground=COLORS['muted'])
        style.configure('Summary.TLabel', foreground=COLORS['accent'], font=('Microsoft JhengHei', 12, 'bold'))
        style.configure('TLabelframe', bordercolor=COLORS['border'], relief='solid')
        style.configure('TLabelframe.Label', font=('Microsoft JhengHei', 12, 'bold'))
        style.configure('TButton', padding=(12, 8))
        style.configure('Primary.TButton', padding=(32, 12), background=COLORS['accent'], foreground='white')
        style.map('Primary.TButton', background=[('disabled', COLORS['border']), ('active', COLORS['hover'])], foreground=[('disabled', COLORS['muted'])])
        style.configure('Treeview', rowheight=36, background=COLORS['panel'], fieldbackground=COLORS['panel'])
        style.configure('Treeview.Heading', padding=(8, 8))
        style.map('TButton', background=[('active', '#e3ecee')])

    def change_mode(self):
        self.all_media.set(self.mode.get() == 'normal')
        self.watch.set(self.mode.get() == 'normal')
        self.selection.clear()
        self.search.set('')
        self.refresh()

    def refresh(self):
        mode = self.mode.get()
        workspace = Path(self.config['workspace_dir'])
        folders = {'completed': workspace / 'logs/completed_media', 'status': workspace / 'logs/status',
                   'backups': workspace / 'work/retranscribe_backups', 'logs': workspace / 'logs'}
        if mode in folders:
            folder = folders[mode]
            extensions = {'.json'} if mode in {'completed', 'status'} else {'.txt', '.srt', '.log'}
            self.files = sorted((p for p in folder.rglob('*') if p.is_file() and p.suffix in extensions),
                                key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
        elif mode == 'reports':
            self.files = sorted(self.output.rglob('*.審查報告.txt'))
        elif mode == 'plain':
            self.files = sorted(self.output.rglob('*.srt'))
        elif mode == 'settings':
            self.files = []
        elif mode == 'preview':
            self.files = sorted(p for p in self.output.rglob('*') if p.is_file() and p.suffix.lower() in {'.txt', '.srt'}
                                and not any(token in p.name for token in ('進度', '轉錄中', '審查報告')))
        else:
            self.files = scan_files(self.output if mode in TEXT_MODES else self.input, mode in TEXT_MODES)
        self.selection.intersection_update(self.files)
        self.draw_files()

    def draw_files(self):
        self.tree.delete(*self.tree.get_children())
        term = self.search.get().casefold()
        folder = self.output if self.mode.get() in TEXT_MODES else self.input
        for index, path in enumerate(self.files):
            label = str(path.relative_to(folder)) if path.is_relative_to(folder) else str(path)
            if term and term not in label.casefold():
                continue
            try:
                size = f'{path.stat().st_size / 1024 / 1024:.1f} MB'
            except OSError:
                size = '請重新整理'
            self.tree.insert('', 'end', iid=str(index), values=('☑' if path in self.selection else '☐', label, size))
        if self.tree.get_children():
            self.empty_label.grid_remove()
        else:
            self.empty_label.configure(text='找不到符合條件的檔案。請放入檔案後按「重新整理」，或清除搜尋文字。')
            self.empty_label.grid()
        self.update_state()

    def toggle_file(self, event):
        row = self.tree.focus() if event.keysym == 'space' else self.tree.identify_row(event.y)
        if not row:
            return
        path = self.files[int(row)]
        self.selection.symmetric_difference_update({path})
        self.all_media.set(False)
        self.tree.focus(row)
        self.draw_files()
        if self.tree.exists(row):
            self.tree.focus(row)
        return 'break'

    def select_visible(self):
        self.selection.update(self.files[int(row)] for row in self.tree.get_children())
        self.all_media.set(False)
        self.draw_files()

    def clear_selection(self):
        self.selection.clear()
        self.draw_files()

    def browse_text(self):
        paths = filedialog.askopenfilenames(parent=self.window, title='選擇要校稿的文字檔',
                                           initialdir=self.output, filetypes=[('文字稿與字幕', '*.txt *.srt')])
        for name in paths:
            path = Path(name).resolve()
            if path not in self.files:
                self.files.append(path)
            self.selection.add(path)
        self.draw_files()

    def open_folder(self, folder):
        if folder.is_dir():
            os.startfile(folder)
        else:
            messagebox.showinfo('資料夾尚未建立', '請先執行一般轉錄，程式會建立需要的資料夾。', parent=self.window)

    def update_state(self):
        mode = self.mode.get()
        self.description.configure(text=MODES[mode][1])
        self.all_button.configure(state='normal' if mode == 'normal' else 'disabled')
        self.watch_button.configure(state='normal' if mode == 'normal' and self.all_media.get() else 'disabled')
        self.browse_button.configure(state='normal' if mode == 'proofread' else 'disabled')
        for button in self.output_buttons:
            button.configure(state='normal' if mode in {'normal', 'force', 'retry'} else 'disabled')
        count = len(self.files) if mode == 'normal' and self.all_media.get() else len(self.selection)
        if mode in UTILITY_MODES:
            text = f'{MODES[mode][0]}：{count} 個已勾選檔案。結果顯示在本視窗。'
            valid = mode == 'settings' or count > 0
        elif mode == 'proofread':
            text = f'只校稿 {count} 個文字檔；原稿保留，已有校稿候選不覆寫。'
            valid = count > 0
        else:
            labels = '、'.join(OUTPUTS[token] for token, variable in self.outputs.items() if variable.get())
            if mode == 'normal':
                scope = '全部音檔，持續監控' if self.all_media.get() and self.watch.get() else f'本次處理 {count} 支音檔'
                text = f'{scope}；已完成的略過。輸出：{labels or "請選擇格式"}。'
            elif mode == 'force':
                text = f'重新辨識 {count} 支音檔；會先備份同名舊稿，再更新輸出。'
            else:
                text = f'補回／重試 {count} 支音檔的稿件；沒有完整已存結果的音檔不會重新辨識。'
            valid = bool(labels) and (count > 0 or mode == 'normal' and self.all_media.get() and self.watch.get())
        self.summary.set(text)
        self.summary_label.configure(foreground=COLORS['warning'] if mode == 'force' else COLORS['accent'])
        self.start_button.configure(state='normal' if valid and not self.running else 'disabled', text='開始校稿' if mode == 'proofread' else '開始')

    def begin(self):
        if self.running:
            return
        paths = sorted(self.selection, key=str)
        try:
            if self.mode.get() in UTILITY_MODES:
                if self.mode.get() == 'plain' and any(p.suffix.lower() != '.srt' for p in paths):
                    raise ValueError('字幕轉純文字請選擇 SRT 字幕檔。')
                self.show_result(utility_result(self.mode.get(), paths, self.config))
                return
            elif self.mode.get() == 'proofread':
                if not paths or any(not p.is_file() or p.suffix.lower() not in {'.txt', '.srt'} for p in paths):
                    raise ValueError('請選擇仍存在的 TXT／SRT 文字檔。')
                self.request = ('proofread', paths)
            else:
                session, arguments = make_session(self.config, self.mode.get(), paths, self.all_media.get(),
                                                  self.watch.get(), [t for t, v in self.outputs.items() if v.get()],
                                                  LANGUAGES[self.language.get()], self.recheck.get())
                workspace = Path(session['workspace_dir']).resolve()
                if workspace != ROOT and ROOT not in workspace.parents:
                    raise ValueError('工作資料夾必須位於本專案內。')
                directory = workspace / 'logs' / 'launch_sessions'
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f'{uuid.uuid4().hex}.json'
                path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
                self.request = ('runner', path, arguments)
        except (ValueError, OSError) as error:
            messagebox.showerror('還不能開始', str(error), parent=self.window)
            return
        self.execute()

    def show_result(self, text):
        self.result.configure(state='normal')
        self.result.delete('1.0', 'end')
        self.result.insert('end', text)
        self.result.configure(state='disabled')

    def execute(self):
        self.running, self.stopping = True, False
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.show_result('工作開始。進度與結果會留在這個視窗。\n')
        request = self.request
        self.cancel_path = Path(str(request[1]) + '.cancel') if request[0] == 'runner' else None
        if self.cancel_path is not None:
            self.cancel_path.unlink(missing_ok=True)
        def worker():
            code = 0
            try:
                for command in request_commands(request):
                    if self.stopping:
                        break
                    self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                    text=True, encoding='utf-8', errors='replace',
                                                    creationflags=subprocess.CREATE_NO_WINDOW)
                    for line in self.process.stdout:
                        self.events.put(line)
                    self.process.stdout.close()
                    code = self.process.wait()
                    if code or self.stopping:
                        break
            except Exception as error:
                self.events.put(f'無法執行：{error}\n')
                code = 1
            self.events.put(('done', code))
        threading.Thread(target=worker, daemon=True).start()
        self.window.after(100, self.poll)

    def poll(self):
        while not self.events.empty():
            item = self.events.get_nowait()
            self.result.configure(state='normal')
            if isinstance(item, tuple):
                self.result_code = item[1]
                self.running = False
                self.process = None
                self.result.insert('end', '\n工作已停止。\n' if self.stopping else f'\n工作結束，退出碼 {item[1]}。\n')
                self.stop_button.configure(state='disabled')
                self.update_state()
            else:
                self.result.insert('end', item)
            self.result.see('end')
            self.result.configure(state='disabled')
        if self.running:
            self.window.after(100, self.poll)

    def stop(self):
        self.stopping = True
        process = self.process
        if process and process.poll() is None:
            if getattr(self, 'cancel_path', None) is not None:
                self.cancel_path.write_text('cancel', encoding='ascii')
                def fallback():
                    if process.poll() is None:
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                self.window.after(2000, fallback)
            else:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)

    def cancel(self):
        if hasattr(self, 'audio_review'):
            if self.audio_review.close() is False:
                return
        self.stop()
        self.request = None
        self.window.destroy()


class WizardMenu(LaunchMenu):
    """Progressive disclosure, retaining the tested engine/session contract."""
    def __init__(self, window, config):
        super().__init__(window, config)
        self.tasks.grid_remove()
        self.body.columnconfigure(0, weight=0)
        self.inline_result.grid_remove()
        self.header.grid_remove()
        for widget in self.outer.grid_slaves(row=1):
            widget.grid_remove()
        self.footer.grid_remove()
        self.page_title = ttk.Label(self.outer, style='Title.TLabel')
        self.page_title.grid(row=0, column=0, sticky='w', pady=(0, 18))
        self.nav = ttk.Frame(self.outer)
        self.nav.grid(row=5, column=0, sticky='ew')
        self.back_button = ttk.Button(self.nav, text='上一步', command=self.back)
        self.back_button.pack(side='left')
        self.next_button = ttk.Button(self.nav, text='下一步', style='Primary.TButton', command=self.next)
        self.next_button.pack(side='right')
        self.confirm_panel = ttk.Frame(self.outer, padding=24)
        self.output_help_labels = []
        for token, button in zip(OUTPUTS, self.output_buttons):
            self.output_help_labels.append(ttk.Label(self.formats_panel, text=OUTPUT_HELP[token],
                                                     style='Muted.TLabel', wraplength=720))
        self.confirm_text = ttk.Label(self.confirm_panel, wraplength=780, font=('Microsoft JhengHei', 15))
        self.confirm_text.pack(anchor='w')
        self.progress_panel = ttk.Frame(self.outer)
        self.progress_label = ttk.Label(self.progress_panel, text='準備開始', style='Summary.TLabel')
        self.progress_label.pack(anchor='w', pady=(0, 12))
        self.progress_bar = ttk.Progressbar(self.progress_panel, mode='indeterminate')
        self.progress_bar.pack(fill='x', pady=(0, 12))
        self.result_actions = ttk.Frame(self.progress_panel)
        self.result_actions.pack(side='bottom', fill='x', pady=12)
        ttk.Button(self.result_actions, text='預覽結果', command=lambda: self.choose('preview')).pack(side='left')
        ttk.Button(self.result_actions, text='聽原音', command=self.listen_to_selected).pack(side='left', padx=8)
        ttk.Button(self.result_actions, text='更多工具', command=lambda: self.choices(True)).pack(side='left', padx=12)
        self.result = tk.Text(self.progress_panel, wrap='word', font=('Microsoft JhengHei', 12), state='disabled')
        scroll = ttk.Scrollbar(self.progress_panel, command=self.result.yview)
        scroll.pack(side='right', fill='y')
        self.result.pack(fill='both', expand=True)
        self.result.configure(yscrollcommand=scroll.set)
        self.home = ttk.Frame(window, padding=(70, 40))
        ttk.Label(self.home, text='這次想做什麼？', style='Title.TLabel').pack(anchor='w', pady=(0, 12))
        ttk.Label(self.home, text='先選擇工作，再設定檔案與輸出。', style='Muted.TLabel').pack(anchor='w', pady=(0, 28))
        for title, description, action in (
            ('開始轉錄', '處理新音檔，已完成的自動略過。', lambda: self.choose('normal')),
            ('查看文字稿', '選一份稿閱讀，需要時再聽原音。', lambda: self.choose('preview')),
            ('檢查漏句', '集中核對疑點，保存判斷與備註。', lambda: self.choose('gapcheck'))):
            card = ttk.Frame(self.home, padding=16, relief='solid', borderwidth=1)
            card.pack(fill='x', pady=8)
            ttk.Button(card, text=title + '　›', command=action).pack(fill='x')
            ttk.Label(card, text=description, style='Muted.TLabel').pack(anchor='w', pady=(8, 0))
        ttk.Button(self.home, text='更多工具　›', command=lambda: self.choices(True)).pack(anchor='w', pady=16)
        ttk.Label(self.home, text='所有操作都在這個視窗完成。', style='Muted.TLabel').pack(anchor='w', pady=12)
        self.choice_panel = ttk.Frame(window, padding=40)
        self.transcribe_advanced = ttk.Button(self.file_panel, text='進階轉錄：重新轉錄／補回稿件　›', command=lambda: self.choices(False))
        from audio_review_panel import AudioReviewPanel
        self.audio_review = AudioReviewPanel(self.outer, config)
        self.page = 'home'
        self.show_page('home')

    def choices(self, more):
        self.outer.pack_forget()
        self.home.pack_forget()
        for child in self.choice_panel.winfo_children():
            child.destroy()
        self.choice_panel.pack(fill='both', expand=True)
        ttk.Label(self.choice_panel, text='更多工具' if more else '轉錄進階選項', style='Title.TLabel').pack(anchor='w', pady=(0, 20))
        modes = ('proofread', 'plain', 'listen', 'completed', 'status', 'reports', 'backups', 'logs', 'settings') if more else ('normal', 'force', 'retry')
        tools = ttk.Frame(self.choice_panel)
        tools.pack(fill='both', expand=True)
        canvas = tk.Canvas(tools, highlightthickness=0, background=COLORS['background'], height=240)
        canvas.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(tools, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.configure(yscrollcommand=scroll.set)
        cards = ttk.Frame(canvas)
        item = canvas.create_window((0, 0), window=cards, anchor='nw')
        cards.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(item, width=event.width))
        canvas.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-int(event.delta/120), 'units'))
        cards.columnconfigure(0, weight=1)
        cards.columnconfigure(1, weight=1)
        for index, mode in enumerate(modes):
            title, description = MODES[mode]
            card = ttk.Frame(cards, padding=12)
            card.grid(row=index//2, column=index%2, sticky='new', padx=(0, 16), pady=(0, 8))
            ttk.Button(card, text=title, command=lambda m=mode: self.choose(m)).pack(anchor='w')
            ttk.Label(card, text=description.replace('\n', ' '), style='Muted.TLabel', wraplength=340).pack(anchor='w', pady=(4, 0))
        ttk.Button(self.choice_panel, text='回首頁', command=lambda: self.show_page('home')).pack(anchor='w', pady=16)
        self.page = 'choices'

    def listen_to_selected(self):
        if self.mode.get() == 'preview' and len(self.selection) == 1:
            try:
                if self.audio_review.load(next(iter(self.selection))):
                    self.mode.set('listen')
                    self.show_page('audio_review')
            except (ValueError, OSError) as error:
                messagebox.showerror('無法開啟聽校', str(error), parent=self.window)
        else:
            self.choose('listen')

    def choose(self, mode):
        self.mode.set(mode)
        self.change_mode()
        self.watch.set(False)
        self.show_page('files' if mode != 'settings' else 'result')
        if mode == 'settings':
            self.show_result(utility_result(mode, [], self.config))

    def show_page(self, page):
        if getattr(self, 'page', None) == 'audio_review' and page != 'audio_review':
            if self.audio_review.close() is False:
                return
        self.page = page
        self.choice_panel.pack_forget()
        self.home.pack_forget()
        self.outer.pack_forget()
        if page == 'home':
            self.home.pack(fill='both', expand=True)
            return
        self.outer.pack(fill='both', expand=True)
        for panel in (self.body, self.formats_panel, self.confirm_panel, self.progress_panel, self.summary_label, self.audio_review):
            panel.grid_remove()
        self.back_button.configure(state='disabled' if self.running else 'normal')
        mode = self.mode.get()
        titles = {'files': '選擇檔案', 'outputs': '這次要輸出什麼？', 'confirm': '確認本次工作',
                  'result': '進度與結果', 'audio_review': '聽原音核對'}
        self.page_title.configure(text=MODES[mode][0] + '｜' + titles[page])
        self.next_button.configure(state='normal', text='下一步', command=self.next)
        if page == 'files':
            self.body.grid()
            self.file_panel.configure(text=MODES[mode][1].replace('\n', ' '))
            self.scope_panel.grid() if mode == 'normal' else self.scope_panel.grid_remove()
            self.transcribe_advanced.grid(row=7, column=0, sticky='w', pady=(8, 0)) if mode in {'normal', 'force', 'retry'} else self.transcribe_advanced.grid_remove()
            self.browse_button.configure(state='normal' if mode in {'proofread', 'preview', 'plain', 'listen'} else 'disabled')
            self.update_state()
        elif page == 'outputs':
            self.formats_panel.grid(row=2, column=0, sticky='new')
            self.formats_panel.configure(text='選擇要產生的檔案')
            for button, help_label in zip(self.output_buttons, self.output_help_labels):
                button.pack_forget()
                help_label.pack_forget()
                button.pack(anchor='w', pady=(6, 2))
                help_label.pack(anchor='w', padx=26, pady=(0, 4))
            self.asr_options.pack_forget()
            self.asr_help.pack_forget()
            if mode in {'normal', 'force'}:
                self.asr_options.pack(anchor='w', pady=(8, 4))
                self.asr_help.pack(anchor='w', pady=(0, 4))
            if not hasattr(self, 'wizard_watch'):
                self.wizard_watch = ttk.Checkbutton(self.confirm_panel, text='持續監控新音檔', variable=self.watch)
            self.confirm_panel.grid(row=3, column=0, sticky='ew')
            self.confirm_text.configure(text='設定只套用這一次。AI 校稿另存候選，原稿保留。')
            self.wizard_watch.pack(anchor='w', pady=20) if mode == 'normal' and self.all_media.get() else self.wizard_watch.pack_forget()
        elif page == 'confirm':
            if hasattr(self, 'wizard_watch'):
                self.wizard_watch.pack_forget()
            self.confirm_panel.grid(row=2, column=0, sticky='nsew')
            selected = self.files if mode == 'normal' and self.all_media.get() else sorted(self.selection, key=str)
            labels = '、'.join(OUTPUTS[t] for t, v in self.outputs.items() if v.get())
            text = MODES[mode][1].replace('\n', ' ') + f'\n\n選擇 {len(selected)} 個檔案：\n' + '\n'.join(p.name for p in selected[:8])
            if len(selected) > 8:
                text += f'\n另有 {len(selected)-8} 個檔案（可按上一步查看完整清單）'
            if mode in {'normal', 'force', 'retry'}:
                text += f'\n\n輸出：{labels}\n持續監控：{"開啟" if mode == "normal" and self.all_media.get() and self.watch.get() else "關閉"}'
                if mode != 'retry':
                    text += f'\n音檔語言：{self.language.get()}\n自動重查疑點：{"開啟" if self.recheck.get() else "關閉"}'
            self.confirm_text.configure(text=text)
            self.next_button.configure(text='確認並開始', command=self.begin)
        elif page == 'audio_review':
            self.audio_review.set_view(mode == 'gapcheck')
            self.audio_review.grid(row=2, column=0, sticky='nsew')
            self.next_button.configure(text='回首頁', command=lambda: self.show_page('home'))
        else:
            self.progress_panel.grid(row=2, column=0, sticky='nsew')
            self.result_actions.pack_forget() if self.running else self.result_actions.pack(side='bottom', fill='x', pady=12)
            self.next_button.configure(text='停止工作' if self.running else '回首頁', command=self.stop if self.running else lambda: self.show_page('home'))

    def next(self):
        mode = self.mode.get()
        if self.page == 'files':
            if not self.selection and not (mode == 'normal' and self.all_media.get()):
                messagebox.showinfo('請先選檔案', '請勾選要處理的檔案。', parent=self.window)
                return
            if mode in {'listen', 'gapcheck'}:
                if len(self.selection) != 1:
                    messagebox.showinfo('請選一個檔案', '聽校一次開啟一份稿，請只勾選一個檔案。', parent=self.window)
                    return
                try:
                    if self.audio_review.load(next(iter(self.selection)), mode == 'gapcheck'):
                        self.show_page('audio_review')
                except (OSError, ValueError) as error:
                    messagebox.showerror('無法開啟聽校', str(error), parent=self.window)
                return
            if mode in {'preview', 'completed', 'status', 'reports', 'backups', 'logs'}:
                self.begin()
            else:
                self.show_page('outputs' if mode in {'normal', 'force', 'retry'} else 'confirm')
        elif self.page == 'outputs':
            if not any(v.get() for v in self.outputs.values()):
                messagebox.showinfo('請選輸出', '請至少選一種輸出格式。', parent=self.window)
                return
            try:
                make_session(self.config, mode, sorted(self.selection, key=str), self.all_media.get(),
                             self.watch.get(), [t for t, v in self.outputs.items() if v.get()],
                             LANGUAGES[self.language.get()], self.recheck.get())
            except (ValueError, OSError) as error:
                messagebox.showinfo('請調整選擇', str(error), parent=self.window)
                return
            self.show_page('confirm')

    def back(self):
        if self.running:
            return
        if self.page == 'outputs':
            self.show_page('files')
        elif self.page == 'confirm':
            self.show_page('outputs' if self.mode.get() in {'normal', 'force', 'retry'} else 'files')
        else:
            self.show_page('home')

    def show_result(self, text):
        super().show_result(text)
        self.progress_label.configure(text='處理中，可隨時停止' if self.running else '結果／內容預覽')
        if self.running:
            self.progress_bar.start(12)
        else:
            self.progress_bar.stop()
        self.show_page('result')

    def poll(self):
        super().poll()
        if not self.running:
            self.progress_bar.stop()
            self.progress_label.configure(text='工作已停止' if self.stopping else ('處理完成，內容仍待核對' if getattr(self, 'result_code', 1) == 0 else '工作未完成，請查看錯誤紀錄'))
            self.show_page('result')


def utility_result(mode, paths, config):
    if mode == 'settings':
        labels = {'desktop_input_dir': '音檔資料夾', 'desktop_output_dir': '文字稿資料夾',
                  'workspace_dir': '工作資料夾', 'model_path': '語音模型', 'vad_model_path': '語音活動模型',
                  'whisper_exe': '語音辨識工具', 'ffmpeg_exe': '音訊轉換工具', 'ollama_model': '校稿模型',
                  'ollama_api_url': '校稿服務位置'}
        lines = []
        for key, label in labels.items():
            if key in config:
                value = config[key]
                state = f'（{"存在" if Path(value).exists() else "找不到檔案或資料夾"}）' if key.endswith(('_dir', '_exe', '_path')) else ''
                lines.append(f'{label}：{value}{state}')
        return '\n'.join(lines)
    results = []
    for path in paths:
        content = path.read_text(encoding='utf-8-sig', errors='replace')
        if mode == 'plain':
            plain = re.sub(r'(?m)^\s*\d+\s*\n(?=\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->)', '', content)
            plain = re.sub(r'(?m)^\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->[^\n]*\n?', '', plain)
            target = path.with_name(f'{path.stem}.純文字匯出.{uuid.uuid4().hex[:8]}.txt')
            with target.open('x', encoding='utf-8') as stream:
                stream.write(plain.strip() + '\n')
            results.append(f'已另存：{target}\n原字幕保留。')
        else:
            if path.suffix == '.json':
                try:
                    content = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
                except ValueError:
                    pass
            results.append(f'【{path}】\n{content[:100000]}' + ('\n（顯示前十萬字）' if len(content) > 100000 else ''))
    return '\n\n'.join(results)


def request_commands(request):
    prefix = [sys.executable, '-X', 'utf8']
    if request[0] == 'runner':
        return [prefix + [str(ROOT / 'scripts/run_transcribe.py'), '-ConfigPath', str(request[1]), '--cancel', str(request[1]) + '.cancel', *request[2]]]
    return [prefix + [str(ROOT / 'scripts/proofread_only.py'), str(source)] for source in request[1]]


def run_request(request) -> int:
    result = 0
    for command in request_commands(request):
        code = subprocess.run(command).returncode
        if code:
            result = code
            if code == 2:
                break
    return result


def main() -> int:
    config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
    window = tk.Tk()
    menu = WizardMenu(window, config)
    window.mainloop()
    return 20


if __name__ == '__main__':
    raise SystemExit(main())
