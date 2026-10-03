"""Local Max quota viewer and guarded default-account switcher. No gateway."""
import concurrent.futures
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
from tkinter import ttk, messagebox, simpledialog, filedialog
import urllib.error
import urllib.request
import uuid

import accounts

HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


from core import read, write, running_claude
from enhanced import Store, runner_active
from desktop import Tray, create_shortcut, restore_existing


def reset_text(value):
    if not value:
        return '官方未提供恢复时间'
    when = dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
    remaining = max(0, int(when.timestamp() - time.time()))
    hours, minutes = remaining // 3600, remaining % 3600 // 60
    return f'{when:%m月%d日 %H:%M} 恢复 · {hours}小时{minutes}分后'


class App:
    def __init__(self, window):
        self.window = window
        self.store = Store()
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
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 20, 'bold'))
        style.configure('Name.TLabel', background='white', font=('Microsoft YaHei UI', 12, 'bold'))
        style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(10, 6))
        style.configure('Blue.Horizontal.TProgressbar', background='#3864d8', troughcolor='#e8edf5')
        style.configure('Red.Horizontal.TProgressbar', background='#ce4850', troughcolor='#f9e8e9')
        outer = ttk.Frame(window, padding=24)
        outer.pack(fill='both', expand=True)
        head = ttk.Frame(outer)
        head.pack(fill='x')
        ttk.Label(head, text='Claude Max 账号与额度', style='Title.TLabel').pack(side='left')
        self.refresh_button = ttk.Button(head, text='刷新额度', command=self.refresh)
        self.refresh_button.pack(side='right')
        self.add_button = ttk.Button(head, text='＋ 增加账号', command=self.add_account)
        self.add_button.pack(side='right', padx=10)
        ttk.Button(head, text='添加桌面快捷方式', command=self.add_shortcut).pack(side='right', padx=(8, 0))
        self.subtitle = ttk.Label(outer, text=f'官方额度 · 每 {self.store.settings()["poll_seconds"]} 秒查询 · 时间按 Windows 本地时区显示')
        self.subtitle.pack(anchor='w', pady=(8, 16))
        self.current_label = ttk.Label(outer, text='读取默认账号…')
        self.current_label.pack(anchor='w', pady=(0, 12))
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill='x', pady=(0, 12))
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
        self.canvas = tk.Canvas(body, bg='#f3f5f8', highlightthickness=0, height=620)
        scroll = ttk.Scrollbar(body, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.grid = ttk.Frame(self.canvas)
        self.grid.columnconfigure((0, 1), weight=1, uniform='cards')
        grid_window = self.canvas.create_window((0, 0), window=self.grid, anchor='nw')
        self.grid.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(grid_window, width=e.width))
        window.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(-int(e.delta / 120), 'units'))
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
        window.after(200, self.refresh)
        window.after(1000, self.auto_refresh)
        window.after(1000, self.tick)

    def build_cards(self):
        grid = self.grid
        for child in grid.winfo_children():
            child.destroy()
        self.cards = {}
        self.details = {}
        for index, slot in enumerate(self.store.slots()):
            frame = ttk.Frame(grid, style='Card.TFrame', padding=18)
            frame.grid(row=index//2, column=index%2, sticky='nsew', padx=6, pady=6)
            meta = self.store.meta(slot)
            name = ttk.Label(frame, text=meta['alias'] or self.store.identity(slot)['emailAddress'], style='Name.TLabel', wraplength=420)
            name.pack(anchor='w')
            if meta['alias']:
                ttk.Label(frame, text=self.store.identity(slot)['emailAddress'], style='Card.TLabel').pack(anchor='w')
            state = ttk.Label(frame, text='查询中…', style='Card.TLabel', wraplength=420)
            state.pack(anchor='w', pady=(5, 8))
            detail = ttk.Label(frame, text='', style='Card.TLabel', wraplength=420, foreground='#657085')
            detail.pack(anchor='w')
            self.details[slot] = detail
            labels, bars, resets = {}, {}, {}
            for key, title in [('five_hour', '五小时'), ('seven_day', '每周')]:
                labels[key] = ttk.Label(frame, text=title + '：—', style='Card.TLabel')
                labels[key].pack(anchor='w')
                bars[key] = ttk.Progressbar(frame, maximum=100, style='Blue.Horizontal.TProgressbar')
                bars[key].pack(fill='x', pady=4)
                resets[key] = ttk.Label(frame, text='—', style='Card.TLabel', foreground='#657085')
                resets[key].pack(anchor='w', pady=(0, 8))
            buttons = ttk.Frame(frame, style='Card.TFrame')
            buttons.pack(fill='x', side='bottom', pady=(5, 0))
            switch = ttk.Button(buttons, text='切换为默认账号', command=lambda s=slot: self.switch(s))
            switch.pack(side='left')
            ttk.Button(buttons, text='重新授权', command=lambda s=slot: self.login(s)).pack(side='right')
            management = ttk.Frame(frame, style='Card.TFrame')
            management.pack(fill='x', pady=(4, 4))
            ttk.Button(management, text='别名', command=lambda s=slot: self.rename_account(s)).pack(side='left')
            ttk.Button(management, text='移除', command=lambda s=slot: self.remove_account(s)).pack(side='right')
            ttk.Button(management, text='停用轮换' if meta['enabled'] else '启用轮换', command=lambda s=slot: self.toggle_account(s)).pack(side='left', padx=6)
            self.cards[slot] = (state, labels, bars, resets, switch)
        for slot, data in self.results.items():
            if slot in self.cards:
                self.render(data)

    def refresh(self):
        if self.exit_requested or self.refreshing:
            return
        if time.time() - self.last_refresh < 30:
            self.note.config(text='请间隔至少 30 秒再查询，避免触发官方频率限制。')
            return
        slots = self.store.slots()
        self.refreshing = True
        self.last_refresh = time.time()
        self.refresh_button.state(['disabled'])
        self.note.config(text=f'正在向官方查询 {len(slots)} 个账号的额度…')
        def work():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    tasks = {pool.submit(self.store.quota, s): s for s in slots}
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
        except Exception as exc:
            self.note.config(text=f'监控异常：{kind.__name__}；日志写入失败：{type(exc).__name__}。请检查本地文件。')

    def destroyed(self, event):
        if event.widget is self.window:
            for timer in self.window.tk.call('after', 'info'):
                self.window.after_cancel(timer)
            self.tray.close()

    def render(self, data):
        slot = data['slot']
        self.results[slot] = data
        state, labels, bars, resets, switch = self.cards[slot]
        current = self.store.identity(slot)['accountUuid'] == self.store.current()
        if current:
            self.current_label.config(text='默认账号：' + self.store.identity(slot)['emailAddress'] + '  ·  CC Switch 官方用量读取此账号')
        switch.state(['disabled'] if current else ['!disabled'])
        if data.get('error'):
            self.details[slot].config(text='需要重新授权' if data.get('reauth_required') else '暂时错误，按冷却时间自动重试')
            state.config(text=data['error'], foreground='#b9434d', wraplength=420)
            for key in labels:
                labels[key].config(text=('五小时' if key == 'five_hour' else '每周') + '：未知')
                bars[key]['value'] = 0
                previous = data.get('last_good') or {}
                prior = previous.get('windows', {}).get(key, {}).get('utilization')
                resets[key].config(text=f'上次成功查询：已用 {prior:g}%（旧数据，不用于自动切换）' if isinstance(prior, (int, float)) else '本次查询未成功，不代表额度为零')
            return
        exhausted = any(w.get('utilization', 0) >= 100 for w in data['windows'].values())
        scoped_text = '；'.join(f'{w["name"]} 周额度 {w["utilization"]:g}%' for w in data.get('scoped', []))
        checked_text = dt.datetime.fromtimestamp(data['checked']).strftime('%H:%M:%S') if data.get('checked') else '未知'
        self.details[slot].config(text='数据时间 ' + checked_text + ('（缓存）' if data.get('cached') else '') + ('\n' + scoped_text if scoped_text else ''))
        extra = ''
        expiry = data.get('auth_expires')
        if expiry:
            extra = ' · 授权至 ' + dt.datetime.fromtimestamp(expiry / 1000).strftime('%m/%d')
        if not self.store.meta(slot)['enabled']:
            extra += ' · 不参与自动轮换'
        state.config(text=('默认账号 · ' if current else '') + ('额度已耗尽' if exhausted else '有可用额度') + ' · MAX' + extra,
                     foreground='#b9434d' if exhausted else '#25715b')
        for key, label in labels.items():
            value = data['windows'][key].get('utilization')
            if not isinstance(value, (int, float)):
                label.config(text=('五小时' if key == 'five_hour' else '每周') + '：未知')
                continue
            label.config(text=f'{"五小时" if key == "five_hour" else "每周"}：已用 {value:g}% · 剩余 {max(0,100-value):g}%')
            bars[key]['value'] = value
            bars[key].config(style=('Red' if value >= 100 else 'Blue') + '.Horizontal.TProgressbar')
            resets[key].config(text=reset_text(data['windows'][key].get('resets_at')))

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
                    self.note.config(text=f'检查完成 {dt.datetime.now():%H:%M:%S} · {ok}/{len(self.cards)} 个账号有有效数据，本轮 {fresh} 个获得新额度 · 实际数据时间见卡片')
                    if self.initial_view:
                        self.canvas.yview_moveto(0)
                        self.initial_view = False
                    if self.auto_value.get() and not self.login_process and not self.exit_requested:
                        self.run_job(lambda: self.store.auto_step(list(self.results.values())), 'auto')
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
                            self.refresh()
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
                                messagebox.showerror('账号不一致', '登录了另一个账号，已恢复原账号配置。请重新授权并选择卡片上的账号。')
                        except (OSError, KeyError, ValueError):
                            write(config_path, config_data)
                            write(credential_path, credential_data)
                            messagebox.showerror('授权未完成', '未获取有效登录身份，已恢复原账号配置。')
                    self.last_refresh = 0
                    self.store.invalidate(value)
                    self.refresh()
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
        if any(self.results.get(slot, {}).get('next_poll', 0) <= time.time() for slot in self.cards):
            self.refresh()

    def refresh_after_enrollment(self):
        if self.refreshing:
            self.window.after(200, self.refresh_after_enrollment)
        else:
            self.last_refresh = 0
            self.refresh()

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
            for slot, data in list(self.results.items()):
                if slot in self.cards:
                    self.render(data)

    def toggle_auto(self):
        self.store.save_settings({'auto_enabled': self.auto_value.get()})
        self.auto_label.config(text='自动模式已开启；关闭窗口后在托盘继续监控' if self.auto_value.get() else '自动模式已关闭；托盘右键可退出')
        if self.auto_value.get():
            self.run_job(lambda: self.store.auto_step(list(self.results.values())), 'auto')

    def best_account(self):
        target = self.store.choose(list(self.results.values()))
        if target is None:
            messagebox.showinfo('暂无可用账号', '当前没有查询成功、已启用且低于切换阈值的账号。等待额度恢复后再试。')
            return
        self.switch(target)

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
            self.refresh()
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
                ('poll_seconds', '查询间隔（180–3600秒）'), ('cooldown', '切换冷却（60–3600秒）'),
                ('model', '额外检查的模型周限额（可留空）')]):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky='w', pady=6)
            value = tk.StringVar(value=str(current[key]))
            ttk.Entry(body, textvariable=value, width=24).grid(row=row, column=1, padx=12)
            fields[key] = value
        ttk.Label(body, text='访问令牌按需自动刷新；刷新授权失效时提示重新登录。\n已验证 Windows Claude 2.1.288 运行中切换，后续请求生效。\n已经发出的请求不变；受管任务可在额度中断后换号接续。', wraplength=520).grid(row=4, column=0, columnspan=2, pady=12)
        def save():
            try:
                values = {k: (v.get().strip() if k == 'model' else int(v.get())) for k, v in fields.items()}
                self.store.save_settings(values)
                self.subtitle.config(text=f'官方额度 · 每 {values["poll_seconds"]} 秒查询 · 时间按 Windows 本地时区显示')
                dialog.destroy()
            except ValueError as exc:
                messagebox.showerror('设置无效', str(exc))
        ttk.Button(body, text='保存', command=save).grid(row=5, column=1, sticky='e')

    def show_logs(self):
        dialog = tk.Toplevel(self.window)
        dialog.title('操作日志（不含令牌）')
        dialog.geometry('900x500')
        box = tk.Text(dialog, wrap='word', font=('Consolas', 10))
        box.pack(fill='both', expand=True)
        path = self.store.root / 'events.jsonl'
        box.insert('end', '\n'.join(path.read_text(encoding='utf-8').splitlines()[-200:]) if path.exists() else '暂无操作记录。')
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


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    # Prevent concurrent account switches from multiple application windows.
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, 'Local\\ClaudeMaxManager')
    if kernel.GetLastError() == 183:
        if not restore_existing():
            ctypes.windll.user32.MessageBoxW(0, '账号工具已经打开，请查看任务栏或系统托盘。', 'Claude Max', 0)
        return
    window = tk.Tk()
    try:
        App(window)
    except Exception as exc:
        window.withdraw()
        messagebox.showerror('工具未能启动', str(exc))
        window.destroy()
        return
    window.mainloop()
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
