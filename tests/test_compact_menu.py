from pathlib import Path
import sys,tkinter as tk,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from test_launch_menu import LaunchMenuTests as MenuFixtures
from launch_menu import WizardMenu


class CompactMenuTests(unittest.TestCase):
    setUp=MenuFixtures.setUp
    tearDown=MenuFixtures.tearDown
    window=MenuFixtures.window
    def buttons(self,widget):
        found=[]
        for child in widget.winfo_children():
            if child.winfo_class()=='TButton':found.append(child)
            found.extend(self.buttons(child))
        return found
    def test_three_home_entries_advanced_and_tools_remain_reachable(self):
        w=self.window();menu=WizardMenu(w,self.config)
        labels=[button.cget('text') for button in self.buttons(menu.home)]
        self.assertEqual(labels,['開始轉錄　›','查看文字稿　›','檢查漏句　›','更多工具　›'])
        menu.choose('normal');w.update();self.assertEqual(menu.transcribe_advanced.winfo_manager(),'grid')
        menu.choices(False);labels=[b.cget('text') for b in self.buttons(menu.choice_panel)]
        self.assertIn('重新轉錄',labels);self.assertIn('補回文字檔／重試校稿',labels)
        menu.choose('force');self.assertFalse(menu.all_media.get())
        menu.choices(True);labels=[b.cget('text') for b in self.buttons(menu.choice_panel)]
        self.assertIn('只校稿文字檔',labels);self.assertIn('字幕轉純文字',labels);self.assertIn('查看重轉備份',labels)
        canvas=next(child for frame in menu.choice_panel.winfo_children() if frame.winfo_class()=='TFrame' for child in frame.winfo_children() if child.winfo_class()=='Canvas')
        canvas.yview_moveto(1);w.update()
        settings=next(button for button in self.buttons(menu.choice_panel) if button.cget('text')=='目前設定與工具路徑')
        settings.invoke();self.assertEqual(menu.mode.get(),'settings');self.assertEqual(menu.page,'result')
    def test_preview_listens_to_current_selection_without_reselection(self):
        w=self.window();menu=WizardMenu(w,self.config)
        menu.choose('preview');source=self.output/'listen.srt';source.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n',encoding='utf-8')
        menu.selection={source}
        with patch.object(menu.audio_review,'load',return_value=True) as load:
            menu.listen_to_selected();load.assert_called_once_with(source)
        self.assertEqual(menu.page,'audio_review');self.assertFalse(menu.audio_review.checking)
        self.assertEqual(menu.audio_review.scan_controls.winfo_manager(),'')
        self.assertEqual(menu.audio_review.mark_panel.winfo_manager(),'')
    def test_collapsed_settings_and_marks_preserve_values_and_filter_switches(self):
        w=self.window();menu=WizardMenu(w,self.config);panel=menu.audio_review
        self.assertEqual(panel.play_settings.winfo_manager(),'');self.assertEqual(panel.mark_panel.winfo_manager(),'')
        panel.toggle_settings();panel.speed.set('0.75倍');panel.context.set('前後5秒');panel.toggle_settings()
        self.assertEqual(panel.speed.get(),'0.75倍');self.assertEqual(panel.context.get(),'前後5秒')
        panel.decision.set('仍不確定');panel.note.set('核對這段')
        panel.toggle_mark();panel.toggle_mark();self.assertEqual(panel.note.get(),'核對這段')
        panel.cues=[{'start_ms':0,'end_ms':1000,'text':'Hello'}]
        panel.show_rows([{'start_ms':2000,'end_ms':3000,'text':'review','kind':'possible_omission'}])
        panel.filter_choice.set('待核對疑點');panel.change_filter();self.assertEqual(panel.tree.get_children(),('1',))
        panel.filter_choice.set('全部字幕');panel.change_filter();self.assertEqual(panel.tree.get_children(),('0','1'))


del MenuFixtures
if __name__=='__main__':unittest.main()
