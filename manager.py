"""Local Max quota viewer and guarded default-account switcher. No gateway."""
import concurrent.futures
import argparse
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk, messagebox, simpledialog, filedialog
import urllib.error
import urllib.request
import uuid

import accounts

HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


from core import read, write, running_claude
from enhanced import Store, runner_active
from desktop import Tray, create_shortcut, restore_existing, process_in_job, launch_independent
from lifecycle import Lifecycle, InstanceMutex


def reset_text(value):
    if not value:
        return '官方未提供恢复时间'
    when = dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
    remaining = max(0, int(when.timestamp() - time.time()))
    hours, minutes = remaining // 3600, remaining % 3600 // 60
    return f'{when:%m月%d日 %H:%M} 恢复 · {hours}小时{minutes}分后'


class App:
    def __init__(self, window, lifecycle=None, store=None):
        self.window = window
        self.window_destroyed = False
        self.lifecycle = lifecycle
        self.store = store if store is not None else Store()
        self.store.recover()
        self.store.init_first()
        self.results = {}
        self.events = queue.Queue()
        self.exit_requested = False
        self.tray = Tray(self.events)
        window.bind('<Destroy>', self.destroyed, add='+')
        self.refreshing = False
        self.last_refresh = 0
        self.login_process = None
        self.login_backup = None
        self.working = False
        self.runner_process = None
        self.initial_view = True
        window.report_callback_exception = self.callback_error
        window.title('Claude Max 账号与额度')
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.geometry('1050x760')
        window.minsize(920, 650)
        window.configure(bg='#f3f5f8')
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#f3f5f8')
        style.configure('Card.TFrame', background='white')
        style.configure('TLabel', background='#f3f5f8', font=('Microsoft YaHei UI', 10))
        style.configure('Card.TLabel', background='white', font=('Microsoft YaHei UI', 10))
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'))
        style.configure('Name.TLabel', background='white', font=('Microsoft YaHei UI', 12, 'bold'))
        style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(10, 6))
        style.configure('Blue.Horizontal.TProgressbar', background='#3864d8', troughcolor='#e8edf5')
        style.configure('Red.Horizontal.TProgressbar', background='#ce4850', troughcolor='#f9e8e9')
        outer = ttk.Frame(window, padding=16)
        outer.pack(fill='both', expand=True)
        head = ttk.Frame(outer)
        head.pack(fill='x')
        ttk.Label(head, text='Claude Max 账号与额度', style='Title.TLabel').pack(side='left')
        self.refresh_button = ttk.Button(head, text='刷新额度', command=self.refresh)
        self.refresh_button.pack(side='right')
        self.add_button = ttk.Button(head, text='＋ 增加账号', command=self.add_account)
        self.add_button.pack(side='right', padx=10)
        ttk.Button(head, text='添加桌面快捷方式', command=self.add_shortcut).pack(side='right', padx=(8, 0))
        self.subtitle = ttk.Label(outer, text=self.poll_description())
        self.subtitle.pack(anchor='w', pady=(6, 8))
        self.current_label = ttk.Label(outer, text='读取默认账号…', wraplength=970)
        self.current_label.pack(anchor='w', pady=(0, 8))
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill='x', pady=(0, 6))
        self.auto_value = tk.BooleanVar(value=self.store.settings()['auto_enabled'])
        ttk.Checkbutton(toolbar, text='自动换号', variable=self.auto_value, command=self.toggle_auto).pack(side='left')
        ttk.Button(toolbar, text='选择可用账号', command=self.best_account).pack(side='left', padx=6)
        ttk.Button(toolbar, text='受管任务', command=self.managed_dialog).pack(side='left', padx=6)
        ttk.Button(toolbar, text='设置', command=self.settings_dialog).pack(side='right')
        ttk.Button(toolbar, text='日志', command=self.show_logs).pack(side='right', padx=6)
        ttk.Button(toolbar, text='恢复移除账号', command=self.restore_account).pack(side='right', padx=6)
        self.auto_label = ttk.Label(outer, text='自动模式：等待检查' if self.auto_value.get() else '自动模式已关闭', foreground='#657085', wraplength=970)
        self.auto_label.pack(anchor='w', pady=(0, 8))
        body = ttk.Frame(outer)
        body.pack(fill='both', expand=True)
        row_height = tkfont.Font(family='Microsoft YaHei UI', size=10).metrics('linespace') + 8
        style.configure('Accounts.Treeview', rowheight=row_height, font=('Microsoft YaHei UI', 10), background='white')
        style.configure('Accounts.Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'))
        columns = [('rank', '#', 35), ('account', '账号', 230), ('five', '五小时', 95),
                   ('week', '每周', 95), ('state', '状态', 140),
                   ('next', '下次检查', 145), ('checked', '数据时间', 150)]
        self.table = ttk.Treeview(body, columns=[c[0] for c in columns], show='headings',
                                  selectmode='browse', height=12, style='Accounts.Treeview')
        for key, title, width in columns:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=width, stretch=key == 'account',
                              anchor='w' if key == 'account' else 'center')
        self.table.tag_configure('current', foreground='#25715b', background='#edf7f1')
        self.table.tag_configure('exhausted', foreground='#b9434d')
        self.table.tag_configure('unknown', foreground='#657085')
        self.canvas = self.table  # Shared scrolling entry used by startup/demo.
        scroll = ttk.Scrollbar(body, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.table.pack(side='left', fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', lambda event: self.show_selected())
        selection = ttk.Frame(outer, padding=(12, 8), style='Card.TFrame')
        selection.pack(fill='x', pady=(10, 0))
        self.selected_title = ttk.Label(selection, text='请选择账号', style='Card.TLabel', wraplength=950)
        self.selected_title.pack(anchor='w')
        self.selected_details = ttk.Label(selection, text='', style='Card.TLabel', foreground='#657085', wraplength=950)
        self.selected_details.pack(anchor='w', pady=(3, 5))
        actions = ttk.Frame(selection, style='Card.TFrame')
        actions.pack(fill='x')
        self.switch_button = ttk.Button(actions, text='切换所选账号', command=lambda: self.for_selected(self.switch))
        self.switch_button.pack(side='left')
        ttk.Button(actions, text='重新授权', command=lambda: self.for_selected(self.login)).pack(side='left', padx=6)
        ttk.Button(actions, text='别名', command=lambda: self.for_selected(self.rename_account)).pack(side='left')
        self.toggle_button = ttk.Button(actions, text='停用轮换', command=lambda: self.for_selected(self.toggle_account))
        self.toggle_button.pack(side='left', padx=6)
        ttk.Button(actions, text='移除', command=lambda: self.for_selected(self.remove_account)).pack(side='right')
        self.build_cards()
        self.note = ttk.Label(outer, text='正在读取官方额度…', wraplength=970)
        self.note.pack(anchor='w', pady=(14, 4))
        ttk.Label(outer, text='运行中换号对后续请求生效；已限额的消息需重新提交，或由受管任务接续。', foreground='#657085').pack(anchor='w')
        window.update_idletasks()
        width = max(1050, window.winfo_reqwidth())
        height = min(max(760, window.winfo_reqheight()), window.winfo_screenheight() - 100)
        window.geometry(f'{width}x{height}')
        window.minsize(width, height)
        window.after(100, self.poll)
        window.after(200, lambda: self.refresh(background=True))
        window.after(1000, self.auto_refresh)
        window.after(1000, self.tick)

    def build_cards(self):
        selected = self.table.selection()
        for item in self.table.get_children():
            self.table.delete(item)
        self.cards = {slot: str(slot) for slot in self.store.slots()}
        for slot, item in self.cards.items():
            name = self.store.meta(slot)['alias'] or self.store.identity(slot)['emailAddress']
            self.table.insert('', 'end', iid=item, values=('', name, '—', '—', '等待数据', '—', '—'))
        for slot, data in self.results.items():
            if slot in self.cards:
                self.render(data)
        self.rank_rows()
        item = selected[0] if selected and self.table.exists(selected[0]) else self.table.get_children()[0] if self.cards else None
        if item:
            self.table.selection_set(item)
        self.show_selected()

    def for_selected(self, action):
        selected = self.table.selection()
        if selected:
            action(int(selected[0]))

    def rank_rows(self):
        current = self.store.current()
        def order(slot):
            level = self.store.relevant(self.results.get(slot, {}), fresh=False)
            return (self.store.identity(slot)['accountUuid'] != current,
                    not self.store.meta(slot)['enabled'], level if level is not None else 101, slot)
        for index, slot in enumerate(sorted(self.cards, key=order), 1):
            self.table.move(str(slot), '', index - 1)
            self.table.set(str(slot), 'rank', index)

    def show_selected(self):
        selected = self.table.selection()
        if not selected:
            self.selected_title.config(text='请选择账号')
            self.selected_details.config(text='')
            self.switch_button.state(['disabled'])
            return
        slot = int(selected[0])
        identity = self.store.identity(slot)
        meta = self.store.meta(slot)
        current = identity['accountUuid'] == self.store.current()
        self.selected_title.config(text=(meta['alias'] + ' · ' if meta['alias'] else '') + identity['emailAddress'])
        self.switch_button.state(['disabled'] if current else ['!disabled'])
        self.toggle_button.config(text='停用轮换' if meta['enabled'] else '启用轮换')
        data = self.results.get(slot, {})
        if data.get('error'):
            self.selected_details.config(text=data['error'] + '；旧额度不用于选择账号。')
            return
        parts = [('五小时' if key == 'five_hour' else '每周') + '：' + reset_text(data['windows'][key].get('resets_at'))
                 for key in ('five_hour', 'seven_day') if key in data.get('windows', {})]
        if data.get('auth_expires'):
            parts.append('授权至 ' + dt.datetime.fromtimestamp(data['auth_expires'] / 1000).strftime('%m/%d'))
        parts += [f'{w["name"]} 周额度 {w["utilization"]:g}%' for w in data.get('scoped', [])]
        self.selected_details.config(text=' · '.join(parts) or '尚未取得官方额度数据。')

    def poll_description(self):
        settings = self.store.settings()
        return f'官方额度（已用比例） · 当前账号随机 {settings["poll_min_seconds"] / 60:g}–{settings["poll_max_seconds"] / 60:g} 分钟查询 · 备用按重置时间查询'

    def refresh(self, *, background=False):
        if self.exit_requested or self.refreshing:
            return
        if time.time() - self.last_refresh < 30:
            self.note.config(text='请间隔至少 30 秒再查询，避免触发官方频率限制。')
            return
        slots = self.store.slots()
        self.refreshing = True
        self.last_refresh = time.time()
        self.refresh_button.state(['disabled'])
        self.note.config(text='正在检查额度；未到重置时间的耗尽账号使用缓存…')
        def work():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    tasks = {pool.submit(self.store.quota, s, background=background): s for s in slots}
                    for task in concurrent.futures.as_completed(tasks):
                        slot = tasks[task]
                        try:
                            self.events.put(('quota', task.result()))
                        except Exception as exc:
                            self.events.put(('quota', {'slot': slot, 'error': '无法读取账号配置：' + type(exc).__name__,
                                                      'next_poll': time.time() + 180}))
            except Exception as exc:
                for slot in slots:
                    self.events.put(('quota', {'slot': slot, 'error': '查询任务异常：' + type(exc).__name__,
                                              'next_poll': time.time() + 180}))
            finally:
                self.events.put(('done', None))
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self.refreshing = False
            self.refresh_button.state(['!disabled'])
            raise

    def callback_error(self, kind, value, traceback):
        # Pythonw has no visible stderr. Report only the class, never raw credentials.
        self.note.config(text=f'监控回调异常：{kind.__name__}；后续定时检查继续运行。')
        try:
            self.store.log('monitor_error', reason=kind.__name__)
            if self.lifecycle:
                self.lifecycle.error('ui', kind, traceback)
        except Exception as exc:
            self.note.config(text=f'监控异常：{kind.__name__}；日志写入失败：{type(exc).__name__}。请检查本地文件。')

    def destroyed(self, event):
        if event.widget is self.window:
            self.window_destroyed = True
            for timer in self.window.tk.call('after', 'info'):
                self.window.after_cancel(timer)
            self.tray.close()

    def render(self, data):
        slot = data['slot']
        self.results[slot] = data
        if slot not in self.cards:
            return
        current = self.store.identity(slot)['accountUuid'] == self.store.current()
        if current:
            self.current_label.config(text='默认账号：' + self.store.identity(slot)['emailAddress'] + '  ·  CC Switch 官方用量读取此账号')
        meta = self.store.meta(slot)
        name = meta['alias'] or self.store.identity(slot)['emailAddress']
        if data.get('error'):
            state = '需重新授权' if data.get('reauth_required') else '查询错误'
            five, week, tag = '未知', '未知', 'unknown'
        else:
            values = [data['windows'].get(key, {}).get('utilization') for key in ('five_hour', 'seven_day')]
            five, week = [f'{value:g}%' if isinstance(value, (int, float)) else '未知' for value in values]
            level = self.store.relevant(data, fresh=False)
            exhausted = level is not None and level >= 100
            state = '已耗尽' if exhausted else '使用中' if current else '备用缓存'
            tag = 'exhausted' if exhausted else 'current' if current else 'unknown' if level is None else ''
        checked_text = dt.datetime.fromtimestamp(data['checked']).strftime('%m/%d %H:%M') if data.get('checked') else '未知'
        deadline = self.store.refresh_deadline(data, active=current)
        blocked = self.store.blocked_until(data)
        if blocked:
            state = '当前 · 待重置' if current else '等待重置'
        if not meta['enabled']:
            state += ' · 停用轮换'
        next_text = dt.datetime.fromtimestamp(deadline).strftime('%m/%d %H:%M') if deadline else '需要时校验'
        self.table.item(str(slot), values=('', name, five, week, state, next_text, checked_text), tags=(tag,))
        self.rank_rows()
        self.show_selected()

    def poll(self):
        # Arm before processing: a malformed row or disk error cannot kill the loop.
        self.window.after(150, self.poll)
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'tray':
                    if value == 'exit':
                        self.exit_app()
                    else:
                        self.show_window()
                elif kind == 'tray_error':
                    self.show_window()
                    messagebox.showerror('托盘异常', value)
                elif kind == 'background_error':
                    self.note.config(text='后台线程异常：' + value + '；详情见本地运行诊断。')
                elif kind == 'quota':
                    try:
                        self.render(value)
                    except Exception as exc:
                        self.results[value['slot']] = {**value, 'error': '显示数据异常：' + type(exc).__name__}
                        raise
                elif kind == 'done':
                    self.refreshing = False
                    self.refresh_button.state(['!disabled'])
                    ok = sum(not d.get('error') for d in self.results.values())
                    fresh = sum(not d.get('cached') and not d.get('error') for d in self.results.values())
                    self.note.config(text=f'检查完成 {dt.datetime.now():%H:%M:%S} · {ok}/{len(self.cards)} 个账号有额度记录，本轮 {fresh} 个获得新额度 · 数据时间见列表')
                    if self.initial_view:
                        self.canvas.yview_moveto(0)
                        self.initial_view = False
                    if self.auto_value.get() and not self.login_process and not self.exit_requested:
                        self.run_job(self.check_rotation, 'auto')
                    write(self.store.root / 'last-status.json', {'checked': dt.datetime.now().astimezone().isoformat(), 'accounts': list(self.results.values())})
                    self.store.log('quota_cycle', reason=f'fresh={fresh}; valid={ok}; total={len(self.cards)}')
                elif kind == 'job':
                    label, result, error = value
                    self.working = False
                    if error:
                        self.auto_label.config(text=error)
                        if label != 'auto':
                            messagebox.showerror('操作未完成', error)
                    else:
                        self.auto_label.config(text=str(result))
                        if label == 'switch':
                            self.last_refresh = 0
                            self.refresh(background=True)
                elif kind == 'enrolled':
                    path, exitcode = value
                    self.login_process = None
                    self.add_button.state(['!disabled'])
                    try:
                        if exitcode:
                            raise RuntimeError('官方登录已取消或未完成，原账号列表保持不变。')
                        slot = self.store.finish_enrollment(path)
                        self.store.log('account_added', slot=slot)
                        self.build_cards()
                        self.note.config(text='已增加账号：' + self.store.identity(slot)['emailAddress'])
                        self.last_refresh = 0
                        # A running refresh uses its original snapshot of slots.
                        self.window.after(200, self.refresh_after_enrollment)
                    except (RuntimeError, OSError, KeyError, ValueError) as exc:
                        self.store.discard_enrollment(path)
                        messagebox.showinfo('未增加账号', str(exc))
                elif kind == 'login':
                    self.login_process = None
                    backup = self.login_backup
                    self.login_backup = None
                    if backup:
                        config_path, credential_path, config_data, credential_data, expected = backup
                        try:
                            if read(config_path)['oauthAccount']['accountUuid'] != expected:
                                write(config_path, config_data)
                                write(credential_path, credential_data)
                                messagebox.showerror('账号不一致', '登录了另一个账号，已恢复原账号配置。请重新授权并选择列表中的账号。')
                        except (OSError, KeyError, ValueError):
                            write(config_path, config_data)
                            write(credential_path, credential_data)
                            messagebox.showerror('授权未完成', '未获取有效登录身份，已恢复原账号配置。')
                    self.last_refresh = 0
                    self.store.invalidate(value)
                    self.refresh(background=True)
        except queue.Empty:
            pass
        if self.exit_requested and not (self.working or self.refreshing or self.login_process):
            self.window.destroy()
            return
        if self.window.state() == 'iconic':
            self.close()

    def auto_refresh(self):
        if self.exit_requested:
            return
        # This is a local deadline check, not an API request every second.
        # Store.quota remains authoritative for cache and Retry-After deadlines.
        self.window.after(1000, self.auto_refresh)
        if self.refreshing or self.working or self.login_process or time.time() - self.last_refresh < 30:
            return
        current = self.store.current()
        for slot in self.cards:
            active = self.store.identity(slot)['accountUuid'] == current
            if not active and not self.store.meta(slot)['enabled']:
                continue
            deadline = self.store.refresh_deadline(self.results.get(slot, {}), active=active)
            if deadline is not None and deadline <= time.time():
                self.refresh(background=True)
                break

    def refresh_after_enrollment(self):
        if self.refreshing:
            self.window.after(200, self.refresh_after_enrollment)
        else:
            self.last_refresh = 0
            self.refresh(background=True)

    def close(self):
        if not self.tray.hwnd or not self.tray.thread.is_alive():
            messagebox.showerror('无法隐藏窗口', '系统托盘不可用，请保持窗口打开。')
            return
        self.window.withdraw()

    def show_window(self):
        self.window.deiconify()
        self.window.state('normal')
        self.window.lift()
        self.window.focus_force()

    def exit_app(self):
        if self.login_process:
            self.show_window()
            messagebox.showinfo('授权进行中', '请先完成或关闭官方授权窗口，保存授权结果后再退出。')
            return
        self.exit_requested = True
        self.note.config(text='正在退出，等待当前查询或账号写入完成…')

    def add_shortcut(self):
        self.run_job(lambda: '已添加桌面快捷方式：' + create_shortcut(HERE / 'manager.py'), 'shortcut')

    def run_job(self, action, label):
        if self.exit_requested or self.working or self.refreshing or self.login_process:
            self.auto_label.config(text='正在查询或授权，完成后可继续操作。')
            return
        self.working = True
        def work():
            try:
                self.events.put(('job', (label, action(), None)))
            except Exception as exc:
                self.events.put(('job', (label, None, str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def tick(self):
        if self.window.winfo_exists():
            self.window.after(30000, self.tick)
            if self.lifecycle:
                self.lifecycle.pulse()
            for slot, data in list(self.results.items()):
                if slot in self.cards:
                    self.render(data)

    def toggle_auto(self):
        self.store.save_settings({'auto_enabled': self.auto_value.get()})
        self.auto_label.config(text='自动模式已开启；关闭窗口后在托盘继续监控' if self.auto_value.get() else '自动模式已关闭；托盘右键可退出')
        if self.auto_value.get():
            self.run_job(self.check_rotation, 'auto')

    def check_rotation(self):
        rows = list(self.results.values())
        try:
            return self.store.auto_step(rows)
        finally:
            for row in rows:
                self.events.put(('quota', row))

    def best_account(self):
        if runner_active():
            messagebox.showinfo('受管任务运行中', '请先停止受管任务，再手动选择账号。')
            return
        def work():
            rows = self.store.validate_candidates(list(self.results.values()))
            for row in rows:
                self.events.put(('quota', row))
            target = self.store.choose(rows)
            if target is None:
                raise RuntimeError('暂无查询成功、已启用且低于切换阈值的账号；未重置的耗尽账号不会重复查询。')
            return self.store.switch(target, allow_running=True)
        self.run_job(work, 'switch')

    def rename_account(self, slot):
        alias = simpledialog.askstring('账号别名', '输入便于辨认的名称（留空恢复显示邮箱）：', initialvalue=self.store.meta(slot)['alias'])
        if alias is not None:
            self.store.set_meta(slot, alias=alias.strip())
            self.build_cards()

    def toggle_account(self, slot):
        self.store.set_meta(slot, enabled=not self.store.meta(slot)['enabled'])
        self.build_cards()

    def remove_account(self, slot):
        if self.login_process or self.refreshing or self.working or runner_active():
            messagebox.showinfo('稍后操作', '请等待正在进行的查询、授权或切换结束。')
            return
        if messagebox.askyesno('移除账号', '从列表移除此账号？本地登录将归档，可用“恢复移除账号”找回。'):
            try:
                self.store.remove(slot)
                self.results.pop(slot, None)
                self.build_cards()
            except Exception as exc:
                messagebox.showerror('未移除', str(exc))

    def restore_account(self):
        try:
            self.store.restore_removed()
            self.build_cards()
            self.last_refresh = 0
            self.refresh(background=True)
        except Exception as exc:
            messagebox.showinfo('未恢复', str(exc))

    def settings_dialog(self):
        dialog = tk.Toplevel(self.window)
        dialog.title('监控设置')
        dialog.transient(self.window)
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill='both', expand=True)
        current = self.store.settings()
        fields = {}
        for row, (key, label) in enumerate([('threshold', '自动切换阈值（50–100%）'),
                ('poll_min_seconds', '最短随机间隔（秒，默认300）'),
                ('poll_max_seconds', '最长随机间隔（秒，默认480）'), ('cooldown', '切换冷却（60–3600秒）'),
                ('model', '额外检查的模型周限额（可留空）')]):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=6)
            value = tk.StringVar(value=str(current[key]))
            ttk.Entry(body, textvariable=value, width=24).grid(row=row, column=1, padx=12)
            fields[key] = value
        ttk.Label(body, text='仅当前账号定期查询；备用账号在重置或换号前校验。\n额度耗尽且未到官方重置时间时不查询。\n访问令牌按需刷新，授权失效时提示重新登录。', wraplength=520).grid(row=5, column=0, columnspan=2, pady=12)
        def save():
            try:
                values = {k: (v.get().strip() if k == 'model' else int(v.get())) for k, v in fields.items()}
                self.store.save_settings(values)
                self.subtitle.config(text=self.poll_description())
                dialog.destroy()
            except ValueError as exc:
                messagebox.showerror('设置无效', str(exc))
        ttk.Button(body, text='保存', command=save).grid(row=6, column=1, sticky='e')

    def show_logs(self):
        dialog = tk.Toplevel(self.window)
        dialog.title('操作日志（不含令牌）')
        dialog.geometry('900x500')
        box = tk.Text(dialog, wrap='word', font=('Consolas', 10))
        box.pack(fill='both', expand=True)
        path = self.store.root / 'events.jsonl'
        box.insert('end', '\n'.join(path.read_text(encoding='utf-8').splitlines()[-200:]) if path.exists() else '暂无操作记录。')
        runtime = self.store.root / 'manager-runtime.json'
        if runtime.exists():
            box.insert('end', '\n\n运行诊断：\n' + json.dumps(read(runtime), ensure_ascii=False, indent=2))
        for name in ('manager-fault.log', 'manager-fault.previous.log'):
            fault = self.store.root / name
            if fault.exists() and fault.stat().st_size:
                box.insert('end', '\n\n' + name + '：\n' + fault.read_text(encoding='utf-8', errors='replace')[-16000:])
        box.configure(state='disabled')

    def managed_dialog(self):
        dialog = tk.Toplevel(self.window)
        dialog.title('受管任务：额度中断后自动换号接续')
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill='both', expand=True)
        sessions = []
        for path in (accounts.LIVE / 'sessions').glob('*.json'):
            try:
                info = read(path)
                if info.get('sessionId') and info.get('cwd'):
                    sessions.append(info)
            except (OSError, ValueError):
                continue
        session = tk.StringVar(value=sessions[0]['sessionId'] if sessions else '')
        cwd = tk.StringVar(value=sessions[0]['cwd'] if sessions else '')
        previous_run = self.store.root / 'runner.json'
        if not sessions and previous_run.exists():
            previous = read(previous_run)
            session.set(previous['session'])
            cwd.set(previous['cwd'])
        ttk.Label(body, text='项目目录').grid(row=0, column=0, sticky='w')
        ttk.Entry(body, textvariable=cwd, width=64).grid(row=0, column=1, pady=6)
        def browse():
            selected = filedialog.askdirectory()
            if selected:
                cwd.set(selected)
        ttk.Button(body, text='选择', command=browse).grid(row=0, column=2, padx=6)
        ttk.Label(body, text='会话 ID（留空新建）').grid(row=1, column=0, sticky='w')
        ttk.Entry(body, textvariable=session, width=64).grid(row=1, column=1, pady=6)
        ttk.Label(body, text='任务 / 接续指令').grid(row=2, column=0, sticky='nw')
        prompt = tk.Text(body, height=5, width=64)
        prompt.grid(row=2, column=1, pady=6)
        prompt.insert('1.0', '继续当前会话中尚未完成的用户任务。先核对已有进度，避免重复执行已完成操作。')
        permission = tk.StringVar(value='auto')
        ttk.Label(body, text='Claude 权限模式').grid(row=3, column=0, sticky='w')
        ttk.Combobox(body, textvariable=permission, values=['auto', 'acceptEdits', 'default'], state='readonly').grid(row=3, column=1, sticky='w')
        ttk.Label(body, text='请先退出原 Claude 会话，工具会等待其他 Claude 进程退出。\n只在明确额度错误后续跑；普通错误、权限问题或本轮完成时停止。\n权限由 Claude 官方模式处理，不跳过权限检查。', wraplength=700).grid(row=4, column=0, columnspan=3, pady=12)
        state_label = ttk.Label(body, text='尚未启动', wraplength=700)
        state_label.grid(row=5, column=0, columnspan=3, sticky='w')
        def update_status():
            if not dialog.winfo_exists():
                return
            path = self.store.root / 'runner.json'
            if path.exists():
                data = read(path)
                status = data['status']
                if not runner_active() and status not in ('已停止', '已停止，可使用同一会话继续', '本轮完成，已停止自动提交', '任务出错或需要人工处理，未重复执行', '受管任务异常，已停止自动操作'):
                    status = '当前未运行；上次状态：' + status
                state_label.config(text=status + '\n会话：' + data['session'])
            dialog.after(2000, update_status)
        def launch():
            try:
                if self.login_process or self.working:
                    raise RuntimeError('请等待正在进行的账号操作完成后启动受管任务。')
                if runner_active():
                    raise RuntimeError('已有受管任务在运行，请先停止它。')
                directory = Path(cwd.get()).resolve(strict=True)
                if not directory.is_dir():
                    raise ValueError('请选择项目目录。')
                value = session.get().strip()
                if value:
                    uuid.UUID(value)
                else:
                    value = str(uuid.uuid4())
                text = prompt.get('1.0', 'end').strip()
                if not text:
                    raise ValueError('请输入任务或接续指令。')
                prompt_path = self.store.root / ('task-' + uuid.uuid4().hex + '.txt')
                prompt_path.write_text(text, encoding='utf-8')
                # sys.executable is pythonw for the UI; runner needs its own terminal.
                import sys
                python = Path(sys.executable).with_name('python.exe')
                command = [str(python), str(HERE / 'runner.py'), '--cwd', str(directory),
                           '--session', value, '--prompt-file', str(prompt_path), '--permission-mode', permission.get()]
                if session.get().strip():
                    command.append('--resume')
                self.runner_process = subprocess.Popen(command, cwd=str(HERE), creationflags=subprocess.CREATE_NEW_CONSOLE)
                session.set(value)
                start.state(['disabled'])
            except Exception as exc:
                messagebox.showerror('无法启动', str(exc))
        def stop():
            (self.store.root / 'runner.stop').write_text('stop', encoding='utf-8')
            state_label.config(text='停止请求已发送；等待 Claude 退出并保留会话。')
        start = ttk.Button(body, text='启动受管任务', command=launch)
        start.grid(row=6, column=1, sticky='w', pady=12)
        ttk.Button(body, text='停止受管任务', command=stop).grid(row=6, column=1, sticky='e', pady=12)
        update_status()

    def add_account(self):
        if self.login_process or self.refreshing or self.working:
            messagebox.showinfo('授权进行中', '请先完成已经打开的官方授权窗口。')
            return
        path = self.store.new_enrollment()
        env = accounts.environment(1)
        env['CLAUDE_CONFIG_DIR'] = str(path)
        try:
            self.login_process = subprocess.Popen([str(accounts.CLI), 'auth', 'login', '--claudeai'],
                env=env, cwd=str(HERE), creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError as exc:
            self.store.discard_enrollment(path)
            messagebox.showerror('无法打开官方登录', str(exc))
            return
        self.add_button.state(['disabled'])
        self.note.config(text='请在官方网页登录要增加的 Max 账号。授权完成后自动加入列表，当前账号保持不变。')
        proc = self.login_process
        threading.Thread(target=lambda: self.events.put(('enrolled', (path, proc.wait()))), daemon=True).start()

    def switch(self, slot):
        try:
            if runner_active():
                raise RuntimeError('受管任务正在管理账号，请先在“受管任务”中停止它，再手动切换。')
            result = self.results.get(slot, {})
            exhausted = any(w.get('utilization', 0) >= 100 for w in result.get('windows', {}).values())
            if exhausted and not messagebox.askyesno('目标账号额度已耗尽', '该账号有额度窗口已耗尽。仍要将它设为默认账号吗？'):
                return
            self.run_job(lambda: self.store.switch(slot, allow_running=True), 'switch')
        except Exception as exc:
            messagebox.showerror('未切换账号', str(exc))

    def login(self, slot):
        if self.login_process or self.refreshing or self.working or runner_active():
            messagebox.showinfo('授权进行中', '请先完成已经打开的官方授权窗口。')
            return
        current = self.store.identity(slot)['accountUuid'] == self.store.current()
        if current and self.store.process_check():
            messagebox.showinfo('当前账号正在使用', '请先退出 Claude Code，再重新授权当前账号。备用账号可以独立授权。')
            return
        env = accounts.environment(slot)
        if current:
            env.pop('CLAUDE_CONFIG_DIR', None)
        else:
            env['CLAUDE_CONFIG_DIR'] = str(self.store.profile(slot))
        email = self.store.identity(slot)['emailAddress']
        config_path = self.store.config if current else self.store.profile(slot) / '.claude.json'
        credential_path = self.store.credential_path(slot)
        self.login_backup = (config_path, credential_path, read(config_path), read(credential_path), self.store.identity(slot)['accountUuid'])
        try:
            self.login_process = subprocess.Popen([str(accounts.CLI), 'auth', 'login', '--claudeai', '--email', email],
                env=env, cwd=str(HERE), creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError as exc:
            self.login_backup = None
            messagebox.showerror('无法启动官方授权', str(exc))
            return
        proc = self.login_process
        threading.Thread(target=lambda: (proc.wait(), self.events.put(('login', slot))), daemon=True).start()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Claude Max 账号与额度')
    parser.add_argument('--desktop-launch', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--cli-executable', help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.cli_executable:
        accounts.CLI = Path(options.cli_executable).expanduser().resolve(strict=True)
    # Running from an IDE/tool job can terminate the UI when that host finishes.
    # Restoring the user's existing instance must not start a second instance.
    if restore_existing():
        return
    if process_in_job():
        if options.desktop_launch:
            raise RuntimeError('独立启动仍受宿主进程管理，请从 Windows 桌面快捷方式打开。')
        args = ['--desktop-launch']
        override = options.cli_executable or os.environ.get('CLAUDE_CODE_EXECUTABLE')
        if override:
            args += ['--cli-executable', override]
        launch_independent(HERE / 'manager.py', args)
        return
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    mutex = InstanceMutex()
    lifecycle = None
    window = None
    app = None
    old_thread_hook = threading.excepthook
    try:
        if mutex.existing:
            if not restore_existing():
                ctypes.windll.user32.MessageBoxW(0, '账号工具正在启动，请稍后查看系统托盘。', 'Claude Max', 0)
            return
        store = Store()
        lifecycle = Lifecycle(store.root, store.log)
        window = tk.Tk()
        app = App(window, lifecycle, store)
        def thread_error(args):
            lifecycle.error('thread', args.exc_type, args.exc_traceback)
            app.events.put(('background_error', args.exc_type.__name__))
        threading.excepthook = thread_error
        window.mainloop()
        lifecycle.finish('clean' if app.exit_requested else 'unexpected',
                         'tray_exit' if app.exit_requested else 'mainloop_return')
    except Exception as exc:
        if lifecycle:
            lifecycle.error('main', type(exc), exc.__traceback__)
            lifecycle.finish('failed', type(exc).__name__)
        ctypes.windll.user32.MessageBoxW(0, '程序运行异常：' + str(exc) + '\n错误类型：' + type(exc).__name__ + '\n请查看本地运行诊断。', 'Claude Max', 0x10)
    finally:
        threading.excepthook = old_thread_hook
        try:
            if window and not (app and app.window_destroyed):
                window.update_idletasks()
                window.destroy()
        finally:
            try:
                if lifecycle:
                    lifecycle.close()
            finally:
                mutex.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(0, '程序启动失败：' + str(exc) + '\n请从桌面快捷方式打开。', 'Claude Max', 0x10)
