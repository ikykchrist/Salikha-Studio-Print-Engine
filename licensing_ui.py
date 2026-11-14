"""License gate shown before the main application is constructed."""
import queue
import threading
import tkinter as tk
from tkinter import ttk
from licensing import LicenseError

def show_license_dialog(root, manager, required=False):
    window = tk.Toplevel(root)
    if icon_path := getattr(root, "photoman_icon_path", None):
        window.iconbitmap(icon_path)
    window.title('Salikha Photoman — Activate to continue' if required else 'Salikha Photoman — License')
    window.geometry('700x520'); window.minsize(620, 470); window.configure(bg='#F6F3EF')
    if root.state() != 'withdrawn':
        window.transient(root)
    style = ttk.Style(window); style.theme_use('clam')
    style.configure('LicensePrimary.TButton', padding=(18, 10), background='#754052', foreground='white', borderwidth=0, font=('Segoe UI', 10, 'bold'))
    style.map('LicensePrimary.TButton', background=[('active', '#4D2838'), ('disabled', '#D7C8CE')], foreground=[('disabled', '#8F7F86')])
    style.configure('LicenseSecondary.TButton', padding=(14, 9), background='#EDE5DF', foreground='#4D2838', borderwidth=0)
    style.configure('License.TEntry', padding=10, fieldbackground='white', foreground='#30282D', bordercolor='#DCCFD4')
    style.configure('License.TCheckbutton', background='#F6F3EF', foreground='#6F686B')

    brand = tk.Frame(window, bg='#4D2838', padx=32, pady=24); brand.pack(fill='x')
    tk.Label(brand, text='SALIKHA', font=('Segoe UI', 12, 'bold'), bg='#4D2838', fg='#D8B982').pack(anchor='w')
    tk.Label(brand, text='Your studio starts here.', font=('Segoe UI', 24, 'bold'), bg='#4D2838', fg='white').pack(anchor='w', pady=(5, 0))
    frame = tk.Frame(window, bg='#F6F3EF', padx=32, pady=22); frame.pack(fill='both', expand=True)
    tk.Label(frame, text='Activate your license', font=('Segoe UI', 18, 'bold'), bg='#F6F3EF', fg='#2B2528').pack(anchor='w')
    tk.Label(frame, text='Paste the complete license key supplied by Salikha.\nThis computer will be registered to one license machine slot.', bg='#F6F3EF', fg='#6F686B', justify='left', font=('Segoe UI', 10)).pack(anchor='w', pady=(5, 16))
    tk.Label(frame, text='LICENSE KEY', font=('Segoe UI', 9, 'bold'), bg='#F6F3EF', fg='#4D2838').pack(anchor='w', pady=(0, 5))
    entry = ttk.Entry(frame, show='•', style='License.TEntry', font=('Segoe UI', 11)); entry.pack(fill='x', ipady=7)
    remember = tk.BooleanVar(); ttk.Checkbutton(frame, text='Remember securely for automatic renewal', variable=remember, style='License.TCheckbutton').pack(anchor='w', pady=(10, 8))
    status = tk.StringVar(value='Activation required · this computer is locked')
    tk.Label(frame, textvariable=status, bg='#F3E5EA', fg='#4D2838', anchor='w', justify='left', wraplength=620, padx=12, pady=11, font=('Segoe UI', 9)).pack(fill='x', pady=8)
    result = {'proceed': False, 'busy': False}; messages = queue.Queue(); buttons = []
    if not required:
        try:
            manager.require()
            result['proceed'] = True
            status.set('License active on this computer.')
        except LicenseError:
            pass
    def close():
        if not result['busy']: window.destroy()
    def run(operation):
        if result['busy']: return
        key = entry.get().strip()
        if operation == 'activate' and not key: status.set('Enter your license key first.'); return
        result['busy'] = True
        for button in buttons: button.config(state='disabled')
        status.set('Validating key and confirming machine activation…' if operation == 'activate' else 'Updating license…')
        remember_value = bool(remember.get())
        def worker():
            try:
                if operation == 'activate':
                    remembered = manager.activate(key, remember_value)
                    manager.require()
                    message = ('Activation confirmed. Windows could not save the key for automatic renewal.'
                               if remember_value and not remembered else 'Activation confirmed.')
                elif operation == 'refresh':
                    manager.refresh()
                    manager.require()
                    message = 'License renewed.'
                elif operation == 'logout':
                    manager.logout()
                    message = 'Logged out.'
                else:
                    manager.deactivate(key or None)
                    message = 'Deactivated.'
                messages.put((True, operation, message))
            except Exception as exc:
                detail = str(exc).strip() or type(exc).__name__
                messages.put((False, operation, detail if isinstance(exc, LicenseError) else 'Could not confirm activation: ' + detail))
        threading.Thread(target=worker, daemon=True).start()
    def proceed():
        try: manager.require(); result['proceed'] = True; window.destroy()
        except LicenseError as exc: status.set(str(exc))
    actions = tk.Frame(frame, bg='#F6F3EF'); actions.pack(fill='x', pady=(7, 0))
    activate = ttk.Button(actions, text='Validate & Activate', style='LicensePrimary.TButton', command=lambda: run('activate')); activate.pack(side='left')
    open_button = ttk.Button(actions, text='Open Studio' if required else 'Done', style='LicensePrimary.TButton', command=proceed, state='normal' if result['proceed'] else 'disabled'); open_button.pack(side='left', padx=8); buttons.extend([activate, open_button])
    secondary_actions = [('Renew Saved License', 'refresh')]
    if not required:
        secondary_actions = [('Log out', 'logout'), ('Deactivate', 'deactivate')]
    for label, operation in secondary_actions:
        button = ttk.Button(actions, text=label, style='LicenseSecondary.TButton',
                            command=lambda action=operation: run(action))
        button.pack(side='left', padx=4)
        buttons.append(button)
    footer = tk.Frame(frame, bg='#F6F3EF'); footer.pack(fill='x', side='bottom', pady=(18, 0))
    tk.Label(footer, text='One license · one Windows computer\nYour photos stay on this computer.', bg='#F6F3EF', fg='#7C7377', justify='left', font=('Segoe UI', 9)).pack(side='left')
    ttk.Button(footer, text='Exit app' if required else 'Cancel', style='LicenseSecondary.TButton', command=close).pack(side='right')
    def poll():
        if not window.winfo_exists(): return
        try:
            success, operation, message = messages.get_nowait(); result['busy'] = False; status.set(message)
            if success and operation in ('logout', 'deactivate'):
                result['proceed'] = False
                window.destroy(); return
            try:
                manager.require()
                result['proceed'] = True
            except LicenseError:
                result['proceed'] = False
            for button in buttons:
                button.config(state='normal')
            if not result['proceed']:
                open_button.config(state='disabled')
        except queue.Empty: pass
        window.after(100, poll)
    window.protocol('WM_DELETE_WINDOW', close)
    window.after(100, poll)
    entry.bind('<Return>', lambda _event: run('activate'))
    window.deiconify(); window.lift(); window.focus_force(); entry.focus_set()
    window.wait_visibility()
    window.grab_set()
    root.wait_window(window)
    if not result['proceed']:
        return False
    try:
        manager.require()
        return True
    except LicenseError:
        return False
