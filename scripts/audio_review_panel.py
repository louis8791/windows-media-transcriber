"""Audio review controls embedded in the existing Tk wizard."""
import json
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, filedialog
import uuid
import winsound
import wave

from audio_review import ROOT, ffmpeg, matching_media, matching_subtitle, matching_timed_transcript, sha, REVIEW_DECISIONS, read_decision, write_decision, read_draft, write_draft, clear_draft, PLAYBACK_CONTEXTS, playback_bounds, duration
from recheck_audio import parse_srt_strict
from merge_whisper_chunks import format_timestamp


class AudioReviewPanel(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self.process = self.log = None
        self.play_process = None
        self.rows = []
        self.review_hashes = None
        self.decisions = {}
        self.edit_row = self.edit_hashes = self.edit_base = None
        self.loading_edit = False
        self.play_token = None
        self.end_after = None
        self.last_row = None
        self.clip_cache = {}
        self.media_snapshot = None
        self.duration_ms = None
        self.pending_clip = None
        self.play_after = None
        self.media = self.transcript = None
        self.checking = True
        self.speed = tk.StringVar(value='原速')
        self.status = tk.StringVar(value='選一段文字即可播放對應原音。')
        self.source = ttk.Label(self, wraplength=850)
        self.source.pack(anchor='w', pady=(0, 8))
        ttk.Label(self, text='點選字幕或疑點會播放原音；慢速不改變音高。漏句疑點須聽校，不會自動補字。',
                  wraplength=850).pack(anchor='w')
        controls = self.controls = ttk.Frame(self)
        controls.pack(fill='x', pady=12)
        self.scan_controls = ttk.Frame(controls)
        self.scan_controls.pack(side='left')
        self.check_button = ttk.Button(self.scan_controls, text='檢查漏句', command=self.start_check)
        self.check_button.pack(side='left')
        self.stop_scan_button = ttk.Button(self.scan_controls, text='停止檢查', command=self.stop_check)
        self.play_button = ttk.Button(controls, text='播放／重播', command=self.play_selected)
        self.play_button.pack(side='left', padx=8)
        ttk.Button(controls, text='停止播放', command=lambda: self.stop_playback(True)).pack(side='left')
        self.settings_button = ttk.Button(controls, text='播放設定　▸', command=self.toggle_settings)
        self.settings_button.pack(side='left', padx=8)
        self.force_check = tk.BooleanVar(value=False)
        options = self.play_settings = ttk.Frame(self)
        ttk.Label(options, text='播放速度').pack(side='left', padx=(0, 6))
        ttk.Combobox(options, textvariable=self.speed, values=['原速', '0.75倍', '0.5倍'], state='readonly', width=9).pack(side='left')
        ttk.Label(options, text='播放範圍').pack(side='left', padx=(20, 8))
        self.context = tk.StringVar(value='前後0.4秒')
        ttk.Combobox(options, textvariable=self.context, values=list(PLAYBACK_CONTEXTS), state='readonly', width=13).pack(side='left')
        self.force_button = ttk.Checkbutton(options, text='重新檢查，不沿用', variable=self.force_check)
        ttk.Label(self, textvariable=self.status, wraplength=850).pack(anchor='w', pady=(0, 8))
        navigation = self.navigation = ttk.Frame(self)
        navigation.pack(fill='x', pady=(0, 8))
        self.only_findings = tk.BooleanVar(value=False)
        self.only_pending = tk.BooleanVar(value=False)
        ttk.Label(navigation, text='顯示').pack(side='left', padx=(0, 8))
        self.filter_choice = tk.StringVar(value='全部字幕')
        self.filter_picker = ttk.Combobox(navigation, textvariable=self.filter_choice, values=['待核對疑點', '全部疑點', '全部字幕'], state='readonly', width=13)
        self.filter_picker.pack(side='left', padx=(0, 12))
        self.filter_picker.bind('<<ComboboxSelected>>', lambda _: self.change_filter())
        ttk.Button(navigation, text='上一個疑點', command=lambda: self.jump_finding(-1)).pack(side='left')
        ttk.Button(navigation, text='下一個疑點', command=lambda: self.jump_finding(1)).pack(side='left', padx=8)
        self.review_count = tk.StringVar()
        self.count_label = ttk.Label(self, textvariable=self.review_count)
        self.count_label.pack(anchor='w', pady=(0, 4))
        table = self.table = ttk.Frame(self)
        table.pack(fill='both', expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=('kind', 'time', 'decision', 'text'), show='headings', selectmode='browse', height=3)
        for col, title, width in [('kind', '類型', 90), ('time', '原音時間', 220), ('decision', '人工核對', 110), ('text', '文字／待核對疑點', 420)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=width if col != 'text' else 220, stretch=col == 'text')
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.selected_text = tk.Text(self, height=2, wrap='word', font=('Microsoft JhengHei', 12), state='disabled')
        self.selected_text.pack(side='bottom', fill='x', pady=(8, 0), before=table)
        self.mark_panel = ttk.Frame(self)
        self.mark_panel.pack(side='bottom', fill='x', before=self.selected_text, pady=(4, 0))
        ttk.Label(self.mark_panel, text='所選疑點').grid(row=0, column=0, sticky='w')
        self.decision = tk.StringVar(value='尚未核對')
        ttk.Combobox(self.mark_panel, textvariable=self.decision, values=REVIEW_DECISIONS, state='readonly', width=13).grid(row=0, column=1, sticky='w', padx=8)
        self.save_button = ttk.Button(self.mark_panel, text='保存核對', command=self.save_mark, state='disabled')
        self.save_button.grid(row=0, column=2, sticky='w')
        self.note = tk.StringVar()
        ttk.Label(self.mark_panel, text='備註').grid(row=1, column=0, sticky='w', pady=(4, 0))
        ttk.Entry(self.mark_panel, textvariable=self.note).grid(row=1, column=1, columnspan=2, sticky='ew', pady=(4, 0))
        self.mark_panel.columnconfigure(1, weight=1)
        self.draft_status = tk.StringVar()
        ttk.Label(self.mark_panel, textvariable=self.draft_status).grid(row=2, column=0, columnspan=3, sticky='w')
        self.note.trace_add('write', self.draft_changed)
        self.decision.trace_add('write', self.draft_changed)
        self.mark_panel.pack_forget()
        self.mark_toggle = ttk.Button(self, text='核對這一段　▸', command=self.toggle_mark, state='disabled')
        self.mark_toggle.pack(side='bottom', anchor='w', before=self.selected_text, pady=(4, 0))
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.play_selected())
        self.tree.bind('<Return>', lambda _: self.play_selected())
        self.tree.tag_configure('possible_omission', foreground='#805400')

    def toggle_settings(self):
        if self.play_settings.winfo_manager():
            self.play_settings.pack_forget()
            self.settings_button.configure(text='播放設定　▸')
        else:
            self.play_settings.pack(fill='x', after=self.controls, pady=(0, 8))
            self.settings_button.configure(text='播放設定　▾')

    def change_filter(self):
        self.only_findings.set(self.filter_choice.get() != '全部字幕')
        self.only_pending.set(self.filter_choice.get() == '待核對疑點')
        self.draw_rows()

    def toggle_mark(self):
        if self.mark_panel.winfo_manager():
            self.mark_panel.pack_forget()
            self.mark_toggle.configure(text='核對這一段　▸')
        else:
            self.mark_panel.pack(side='bottom', fill='x', before=self.mark_toggle, pady=(4, 0))
            self.mark_toggle.configure(text='核對這一段　▾（草稿自存，按保存才確認）')

    def set_edit_fields(self, decision, note):
        self.loading_edit = True
        try:
            self.decision.set(decision)
            self.note.set(note)
        finally:
            self.loading_edit = False

    def draft_changed(self, *_):
        if not self.loading_edit:
            self.flush_draft()

    def flush_draft(self):
        if self.edit_row is None:
            return True
        try:
            if (self.decision.get(), self.note.get()) == (self.edit_base['decision'], self.edit_base['note']):
                clear_draft(self.edit_hashes, self.edit_row)
                self.draft_status.set('')
            else:
                write_draft(self.edit_hashes, self.edit_row, self.decision.get(), self.note.get(), self.edit_base)
                self.draft_status.set('草稿已保存；尚未確認，不計入已判定。')
            return True
        except (OSError, ValueError) as error:
            self.draft_status.set(f'草稿未保存：{error}')
            self.status.set('草稿未保存，請處理後再切換或關閉；目前輸入仍保留。')
            return False

    def select_edit(self, row):
        if not self.flush_draft():
            return False
        self.edit_row = self.edit_hashes = self.edit_base = None
        mark = self.mark_for(row)
        self.set_edit_fields(mark['decision'], mark['note'])
        self.draft_status.set('')
        if row['kind'] == 'possible_omission' and self.review_hashes is not None:
            self.edit_row, self.edit_hashes, self.edit_base = dict(row), dict(self.review_hashes), dict(mark)
            try:
                draft = read_draft(self.edit_hashes, row)
                if draft is not None:
                    self.set_edit_fields(draft['decision'], draft['note'])
                    self.draft_status.set('已恢復草稿；尚未確認，不計入已判定。')
            except (OSError, ValueError) as error:
                self.draft_status.set(str(error))
        return True

    def set_view(self, checking):
        self.checking = checking
        if checking:
            self.scan_controls.pack(side='left', before=self.play_button)
            self.count_label.pack(anchor='w', before=self.table, pady=(0, 4))
            self.navigation.pack(fill='x', before=self.count_label, pady=(0, 8))
            self.mark_toggle.pack(side='bottom', anchor='w', before=self.selected_text, pady=(4, 0))
            self.force_button.pack(side='left', padx=12)
        else:
            self.scan_controls.pack_forget()
            self.navigation.pack_forget()
            self.count_label.pack_forget()
            self.mark_toggle.pack_forget()
            self.mark_panel.pack_forget()
            self.force_button.pack_forget()
        self.draw_rows()

    def load(self, path, media_mode=False):
        if self.close() is False:
            raise ValueError('草稿未保存，請先處理目前的核對內容。')
        path = Path(path)
        requested = path if not media_mode else None
        if media_mode:
            media, transcript = path, matching_subtitle(path, self.config)
        else:
            transcript, media = path, matching_media(path, self.config)
        if transcript is None:
            selected = filedialog.askopenfilename(parent=self.winfo_toplevel(), title='選擇此音檔對應的含時間碼 TXT／SRT',
                                                 initialdir=self.config['desktop_output_dir'], filetypes=[('含時間碼文字稿', '*.srt *.txt')])
            if not selected:
                return False
            transcript = Path(selected)
        try:
            cues = parse_srt_strict(transcript.read_text(encoding='utf-8-sig'))
        except ValueError:
            matched = matching_timed_transcript(transcript, self.config)
            if matched is None:
                selected = filedialog.askopenfilename(parent=self.winfo_toplevel(), title='純文字無可靠對應，請選同一音檔的含時間碼 TXT／SRT',
                                                     initialdir=self.config['desktop_output_dir'], filetypes=[('含時間碼文字稿', '*.srt *.txt')])
                if not selected:
                    return False
                matched = Path(selected)
            transcript = matched
            cues = parse_srt_strict(transcript.read_text(encoding='utf-8-sig'))
            media = matching_media(transcript, self.config)
        if media is None:
            selected = filedialog.askopenfilename(parent=self.winfo_toplevel(), title='請選擇這份文字稿對應的原音或影片',
                                                 initialdir=self.config['desktop_input_dir'])
            if not selected:
                return False
            media = Path(selected)
        if not media.is_file() or not transcript.is_file():
            raise ValueError('原音或文字稿已移動，請重新選擇。')
        self.media, self.transcript, self.cues = media, transcript, cues
        self.duration_ms = duration(media, self.config)
        self.media_snapshot = (media.stat().st_size, media.stat().st_mtime_ns)
        self.context.set('前後0.4秒')
        self.review_hashes, self.decisions = None, {}
        self.last_row = None
        self.folder = ROOT/'work/audio_review'/uuid.uuid4().hex
        self.folder.mkdir(parents=True)
        self.source.configure(text=f'原音：{media.name}\n文字稿：{transcript.name}')
        if requested is not None and transcript.resolve() != requested.resolve():
            self.source.configure(text=f'閱讀稿：{requested.name}\n原音：{media.name}｜定位字幕：{transcript.name}')
        self.status.set('選一段文字即可播放。漏句檢查涵蓋開場、結尾及中間至少1秒的無字幕區段。')
        self.force_check.set(False)
        self.play_settings.pack_forget()
        self.settings_button.configure(text='播放設定　▸')
        self.mark_panel.pack_forget()
        self.mark_toggle.configure(text='核對這一段　▸')
        self.only_findings.set(media_mode)
        self.only_pending.set(media_mode)
        self.show_rows([])
        self.selected_text.configure(state='normal')
        self.selected_text.delete('1.0', 'end')
        self.selected_text.configure(state='disabled')
        return True

    def show_rows(self, findings):
        if not self.flush_draft():
            return False
        self.stop_playback()
        self.rows = sorted([dict(row, kind='subtitle') for row in self.cues] + findings, key=lambda row: row['start_ms'])
        self.draw_rows()

    def mark_for(self, row):
        return self.decisions.get((row['start_ms'], row['end_ms']), {'decision': '尚未核對', 'note': ''})

    def draw_rows(self):
        if not self.flush_draft():
            return False
        self.edit_row = self.edit_hashes = self.edit_base = None
        self.filter_choice.set('待核對疑點' if self.only_pending.get() else '全部疑點' if self.only_findings.get() else '全部字幕')
        self.tree.delete(*self.tree.get_children())
        self.save_button.configure(state='disabled')
        self.mark_toggle.configure(state='disabled')
        self.set_edit_fields('尚未核對', '')
        self.draft_status.set('')
        findings = [row for row in self.rows if row['kind'] == 'possible_omission']
        done = sum(self.mark_for(row)['decision'] in {'確實漏句', '掌聲／音樂', '沒有漏句'} for row in findings)
        self.review_count.set(f'疑點 {len(findings)} 處｜已判定 {done} 處｜待核對 {len(findings)-done} 處（含仍不確定）')
        if self.checking and self.review_hashes is None:
            self.review_count.set('尚未檢查：按「檢查漏句」讀取結果，再核對疑點。')
        for index, row in enumerate(self.rows):
            finding = row['kind'] == 'possible_omission'
            decision = self.mark_for(row)['decision'] if finding else ''
            if (self.only_findings.get() and not finding) or (self.only_pending.get() and (not finding or decision in {'確實漏句', '掌聲／音樂', '沒有漏句'})):
                continue
            time = f'{format_timestamp(row["start_ms"])} – {format_timestamp(row["end_ms"])}'
            self.tree.insert('', 'end', iid=str(index), tags=(row['kind'],),
                             values=('漏句疑點' if finding else '字幕', time, decision, row['text']))

    def jump_finding(self, direction):
        candidates = [int(item) for item in self.tree.get_children() if self.rows[int(item)]['kind'] == 'possible_omission']
        if not candidates:
            self.status.set('目前沒有符合篩選的疑點。可取消「只看未完成」或先執行漏句檢查。')
            return
        selected = self.tree.selection()
        current = int(selected[0]) if selected else self.last_row if self.last_row is not None else (-1 if direction > 0 else len(self.rows))
        targets = [index for index in candidates if index > current] if direction > 0 else [index for index in candidates if index < current]
        target = targets[0] if direction > 0 and targets else targets[-1] if targets else candidates[0 if direction > 0 else -1]
        self.last_row = target
        self.tree.selection_set(str(target))
        self.tree.focus(str(target))
        self.tree.see(str(target))

    def save_mark(self):
        selected = self.tree.selection()
        if not selected or self.review_hashes is None:
            return
        row = self.rows[int(selected[0])]
        if self.edit_row is not None and (row['start_ms'], row['end_ms']) != (self.edit_row['start_ms'], self.edit_row['end_ms']):
            self.status.set('請先處理未保存的草稿，核對紀錄未寫入。')
            return
        self.last_row = int(selected[0])
        try:
            if self.source_stats != self.file_stats() or sha(self.transcript) != self.review_hashes['transcript']:
                raise ValueError('原音或字幕已改變，請重新載入並檢查，核對紀錄未寫入。')
            if not self.flush_draft():
                return
            mark = write_decision(self.review_hashes, row, self.decision.get(), self.note.get())
            self.decisions[(row['start_ms'], row['end_ms'])] = mark
            self.edit_row = self.edit_hashes = self.edit_base = None
            cleanup_error = None
            try:
                clear_draft(self.review_hashes, row)
            except (OSError, ValueError) as error:
                cleanup_error = error
            self.draw_rows()
            self.status.set(f'人工核對已保存；草稿清理失敗：{cleanup_error}' if cleanup_error else
                            '人工核對與備註已保存；原稿保持不變。可跳到下一個疑點。')
        except (OSError, ValueError) as error:
            self.status.set(f'核對未保存：{error}')

    def file_stats(self):
        return tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in (self.media, self.transcript))

    def play_selected(self):
        selected = self.tree.selection()
        if not selected or self.media is None:
            return
        self.stop_playback()
        row = self.rows[int(selected[0])]
        self.last_row = int(selected[0])
        if not self.select_edit(row):
            return
        self.save_button.configure(state='normal' if row['kind'] == 'possible_omission' and self.review_hashes is not None else 'disabled')
        self.mark_toggle.configure(state=self.save_button.cget('state'))
        self.selected_text.configure(state='normal')
        self.selected_text.delete('1.0', 'end')
        self.selected_text.insert('end', row['text'])
        self.selected_text.configure(state='disabled')
        try:
            current_stat = (self.media.stat().st_size, self.media.stat().st_mtime_ns)
            if self.media_snapshot is not None and current_stat != self.media_snapshot:
                raise ValueError('原音已更動，請重新載入，避免播放舊片段。')
            a, b = playback_bounds(row, self.duration_ms, self.context.get())
        except (ValueError, OSError) as error:
            self.status.set(f'無法播放：{error}')
            return
        speed_label = self.speed.get()
        speed = {'原速': 1.0, '0.75倍': .75, '0.5倍': .5}[speed_label]
        cache_key = (str(self.media.resolve()), *current_stat, a, b, speed)
        cached = self.clip_cache.get(cache_key)
        if cached is not None:
            clip, expected_hash = cached
            try:
                if sha(clip) == expected_hash:
                    self.clip_cache.pop(cache_key)
                    self.clip_cache[cache_key] = cached
                    self.play_clip(clip, a, b, speed_label, True)
                    return
            except (OSError, ValueError, RuntimeError, wave.Error):
                pass
            self.clip_cache.pop(cache_key, None)
            self.remove_clip(clip)
        clip = self.folder/(uuid.uuid4().hex+'.wav')
        self.pending_clip = clip
        try:
            self.play_log = (self.folder/(clip.stem+'.log')).open('wb')
            self.play_process = subprocess.Popen([ffmpeg(self.config), '-nostdin', '-v', 'error', '-n',
                '-ss', str(a/1000), '-t', str((b-a)/1000), '-i', str(self.media), '-vn',
                '-af', f'atempo={speed}', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(clip)],
                stdout=subprocess.DEVNULL, stderr=self.play_log, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, ValueError) as error:
            self.stop_playback()
            self.status.set(f'無法播放：{error}')
            return
        process = self.play_process
        self.status.set(f'正在準備播放：{format_timestamp(a)}，{speed_label}')
        def poll():
            self.play_after = None
            if process is not self.play_process:
                return
            if process.poll() is None:
                self.play_after = self.after(100, poll)
                return
            self.play_log.close()
            self.play_log = None
            self.play_process = None
            self.pending_clip = None
            if process.returncode != 0 or not clip.is_file():
                self.remove_clip(clip)
                self.status.set('音訊擷取失敗，請確認原音與工具；檢查紀錄保存在專案工作資料夾。')
                return
            try:
                if current_stat != (self.media.stat().st_size, self.media.stat().st_mtime_ns):
                    raise ValueError('擷取期間原音已更動，請重新載入。')
                self.play_clip(clip, a, b, speed_label)
                self.clip_cache[cache_key] = (clip, sha(clip))
                while len(self.clip_cache) > 24:
                    oldest = next(iter(self.clip_cache))
                    old_clip, _ = self.clip_cache.pop(oldest)
                    self.remove_clip(old_clip)
            except (RuntimeError, OSError, ValueError, wave.Error) as error:
                self.remove_clip(clip)
                self.status.set(f'播放失敗：{error}')
        self.play_after = self.after(100, poll)

    def play_clip(self, clip, a, b, speed_label, reused=False):
        with wave.open(str(clip)) as audio:
            if audio.getnchannels() != 1 or audio.getframerate() != 16000 or audio.getsampwidth() != 2 or audio.getnframes() == 0:
                raise ValueError('播放片段格式無效，請重新擷取。')
            milliseconds = round(audio.getnframes()/audio.getframerate()*1000)
        winsound.PlaySound(str(clip), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        token = self.play_token = uuid.uuid4().hex
        reused_label = '沿用已擷取片段；' if reused else ''
        self.status.set(f'播放：{format_timestamp(a)} – {format_timestamp(b)}，{speed_label}；{reused_label}可重播或停止。')
        def ended():
            self.end_after = None
            if self.play_token == token:
                self.play_token = None
                self.status.set(f'這段播放已結束（{speed_label}）。可重播或跳到下一個疑點。')
        self.end_after = self.after(milliseconds, ended)

    def remove_clip(self, clip):
        # Only remove UUID-named playback files created inside this session folder.
        if not clip.resolve().is_relative_to(self.folder.resolve()) or len(clip.stem) != 32 or any(char not in '0123456789abcdef' for char in clip.stem):
            return
        for path in (clip, clip.with_suffix('.log')):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def stop_playback(self, announce=False):
        self.play_token = None
        if self.play_after is not None:
            self.after_cancel(self.play_after)
            self.play_after = None
        if self.end_after is not None:
            self.after_cancel(self.end_after)
            self.end_after = None
        winsound.PlaySound(None, 0)
        if self.play_process is not None:
            if self.play_process.poll() is None:
                self.play_process.terminate()
                self.play_process.wait(timeout=5)
            self.play_process = None
        if getattr(self, 'play_log', None):
            self.play_log.close()
            self.play_log = None
        if self.pending_clip is not None:
            self.remove_clip(self.pending_clip)
            self.pending_clip = None
        if announce:
            self.status.set('播放已停止。')

    def start_check(self):
        if self.process is not None or self.media is None:
            return
        if not self.flush_draft():
            return
        self.report = self.folder/(uuid.uuid4().hex+'.json')
        self.check_source_stats = self.file_stats()
        self.log_path = self.report.with_suffix('.log')
        self.log = self.log_path.open('wb')
        try:
            self.process = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT/'scripts/audio_review.py'),
                                            str(self.media), str(self.transcript), str(self.report)] + (['--force'] if self.force_check.get() else []),
                                           cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as error:
            self.log.close()
            self.log = None
            self.status.set(f'無法開始：{error}')
            return
        self.check_button.configure(state='disabled')
        self.stop_scan_button.pack(side='left', padx=8)
        self.status.set('正在檢查，長音檔需要等待。可停止；原稿保持不變。')
        self.after(200, self.poll_check)

    def poll_check(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            with self.log_path.open('rb') as stream:
                stream.seek(max(0, self.log_path.stat().st_size-3000))
                lines = stream.read().decode('utf-8', errors='replace').splitlines()
            progress = next((line for line in reversed(lines) if line.startswith('檢查音訊')), None)
            if progress:
                self.status.set(progress+'；可停止，原稿保持不變。')
            self.after(200, self.poll_check)
            return
        code = self.process.returncode
        self.log.close()
        self.log = self.process = None
        self.check_button.configure(state='normal')
        self.stop_scan_button.pack_forget()
        try:
            if code or not self.report.is_file():
                raise ValueError(self.log_path.read_text(encoding='utf-8', errors='replace')[-700:])
            report = json.loads(self.report.read_text(encoding='utf-8'))
            if sha(self.transcript) != report['source_hashes']['transcript']:
                raise ValueError('字幕在檢查後已改動，請重新檢查。')
            self.cues = parse_srt_strict(self.transcript.read_text(encoding='utf-8-sig'))
            if self.check_source_stats != self.file_stats():
                raise ValueError('來源在檢查期間已改變，請重新檢查。')
            decisions = {(row['start_ms'], row['end_ms']): read_decision(report['source_hashes'], row) for row in report['findings']}
            if not self.flush_draft():
                return
            self.review_hashes, self.source_stats, self.decisions = report['source_hashes'], self.check_source_stats, decisions
            self.show_rows(report['findings'])
            reused = '已沿用上次結果。' if report.get('reused') else ''
            if not reused and report.get('resumed_chunks'):
                reused = f'已接續檢查，略過{report["resumed_chunks"]}段完成區段。'
            self.status.set(f'檢查完成：{len(report["findings"])}處待聽校。'+reused+report['limitations'])
            first = next((str(i) for i, row in enumerate(self.rows) if row['kind'] == 'possible_omission'), None)
            if first is not None:
                if self.tree.exists(first):
                    self.tree.see(first)
        except (OSError, ValueError, KeyError) as error:
            self.status.set(f'檢查未完成：{error}')

    def stop_check(self):
        if self.process is not None:
            if self.process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                self.process.wait(timeout=10)
            self.process = None
            if self.log:
                self.log.close()
                self.log = None
            self.check_button.configure(state='normal')
            self.stop_scan_button.pack_forget()
            self.status.set('檢查已停止；完成區段已保存，下次按「檢查漏句」可接續。原稿保留。')

    def close(self):
        if not self.flush_draft():
            return False
        self.stop_check()
        self.stop_playback()
        for clip, _ in self.clip_cache.values():
            self.remove_clip(clip)
        self.clip_cache.clear()
        self.edit_row = self.edit_hashes = self.edit_base = None
        return True
