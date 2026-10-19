"""Human audio review for training candidates, separate from everyday transcription."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import uuid
from whisper_learning import ROOT, preflight, save_new

def main():
    window = tk.Tk(); window.title('Whisper 教材核對與訓練檢查'); window.geometry('960x700')
    data = {'schema_version':1, 'records':[]}
    folder = ROOT/'work/whisper_learning'/uuid.uuid4().hex
    ttk.Label(window, text='先播放原音、修正逐字稿，再勾選已聽校。不同錄音分別用於訓練、驗證及考試。').pack(pady=10)
    ttk.Label(window, text='train＝教學；validation＝選版本；test＝獨立考試。同一支錄音只能選一組。').pack(pady=4)
    listing = tk.Listbox(window, height=9); listing.pack(fill='x', padx=20)
    editor = tk.Text(window, height=10, wrap='word'); editor.pack(fill='both', expand=True, padx=20)
    controls = ttk.Frame(window); controls.pack(fill='x', padx=20, pady=8)
    split = tk.StringVar(value='train'); approved = tk.BooleanVar(); reviewer = tk.StringVar()
    ttk.Combobox(controls, values=['train','validation','test'], textvariable=split, state='readonly', width=12).pack(side='left')
    ttk.Checkbutton(controls, text='我已聽校，文字符合原音', variable=approved).pack(side='left', padx=12)
    ttk.Label(controls, text='核對者').pack(side='left'); ttk.Entry(controls, textvariable=reviewer, width=15).pack(side='left')
    status = tk.StringVar(value='尚未匯入教材；目前不會訓練或更換正式模型。')
    ttk.Label(window, textvariable=status, wraplength=900).pack(padx=20, pady=8)
    selected = [None]
    def show(_=None):
        indexes = listing.curselection()
        if not indexes:return
        selected[0] = indexes[0]; row = data['records'][indexes[0]]
        editor.delete('1.0','end'); editor.insert('1.0',row['text'])
        split.set(row['split']); approved.set(row['approved']); reviewer.set(row['verified_by'])
    listing.bind('<<ListboxSelect>>', show)
    def import_data():
        path = filedialog.askopenfilename(filetypes=[('教材 JSON','*.json')])
        if not path:return
        try:
            incoming = json.loads(Path(path).read_text(encoding='utf-8-sig'))['records']
            ids = {r['id'] for r in data['records']}
            for row in incoming:
                if row['id'] not in ids:data['records'].append(row);ids.add(row['id']);listing.insert('end', row['id'])
            status.set(f'已匯入 {len(data["records"])} 個片段；按播放原音逐筆核對。')
        except Exception as error:messagebox.showerror('匯入失敗',str(error))
    def play():
        if selected[0] is not None:os.startfile(data['records'][selected[0]]['audio_path'])
    def save():
        if selected[0] is None:return
        if approved.get() and not reviewer.get().strip():
            messagebox.showinfo('請填核對者','勾選已聽校前請填核對者。');return
        row = data['records'][selected[0]]
        row.update(text=editor.get('1.0','end').strip(), split=split.get(), approved=approved.get(), verified_by=reviewer.get().strip(), verified_at=datetime.now(timezone.utc).isoformat() if approved.get() else '')
        path = folder/(uuid.uuid4().hex+'.json');save_new(path,data)
        status.set('已另存教材版本：'+str(path));last[0]=path
    last = [None]
    running = [False]
    def check():
        result = preflight(data, ROOT/'models/whisper_training/base')
        status.set('可開始有界訓練實驗，仍不更換正式模型。' if result['ready'] else '尚未訓練：'+'；'.join(result['reasons']))
    buttons = ttk.Frame(window);buttons.pack(pady=10)
    for label,command in [('匯入教材',import_data),('播放原音',play),('保存本筆與教材版本',save),('檢查能否訓練',check)]:ttk.Button(buttons,text=label,command=command).pack(side='left',padx=5)
    def start():
        if last[0] is None:status.set('請先保存教材版本。');return
        checked = preflight(json.loads(last[0].read_text(encoding='utf-8')),ROOT/'models/whisper_training/base')
        if not checked['ready']:status.set('尚未訓練：'+'；'.join(checked['reasons']));return
        running[0]=True;button.configure(state='disabled');status.set('訓練候選中；正式转錄模型保持原狀。')
        def worker():
            try:
                from train_whisper import train
                train(last[0],ROOT/'models/whisper_training/base',folder/'run')
                text='實驗完成，請核對比較報告；未替換正式模型。'
            except Exception as error:text='實驗未完成：'+str(error)
            def done():
                running[0]=False;button.configure(state='normal');status.set(text)
            window.after(0,done)
        threading.Thread(target=worker,daemon=False).start()
    button = ttk.Button(window,text='開始有界訓練實驗（最多200步）',command=start);button.pack(pady=5)
    def close():
        if running[0]:status.set('實驗仍在執行，請等待有界訓練結束；目前不會替換正式模型。')
        else:window.destroy()
    window.protocol('WM_DELETE_WINDOW',close)
    window.mainloop()

if __name__ == '__main__':main()
