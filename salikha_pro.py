import os, time, threading, json, queue, stat, shutil, uuid, subprocess, hashlib, sqlite3
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk, filedialog, simpledialog
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PIL import Image, ImageOps, ImageTk, ImageWin, ImageFilter, UnidentifiedImageError
import win32con, win32gui, win32print, win32ui
from ctypes import windll, c_wchar_p, byref, c_void_p, c_ulong, create_unicode_buffer, WinError

# --- PILLOW COMPATIBILITY ---
try:
    from PIL import ImageResampling
    RESAMPLE_METHOD = ImageResampling.LANCZOS
except ImportError:
    RESAMPLE_METHOD = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', 1))

CONFIG_FILE = "salikha_layout.json"
COUNTER_FILE = "salikha_counter.json"
COPIED_FILES_FILE = "salikha_copied_files.json"

APP_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SalikhaStudioPro")
IMPORT_DB_FILE = os.path.join(APP_DATA_DIR, "photo_imports.sqlite3")
THUMBNAIL_CACHE_DIR = os.path.join(APP_DATA_DIR, "thumbnail_cache")
CARD_MANIFEST_FILE = os.path.join(APP_DATA_DIR, "card_manifests.json")

def load_card_manifests():
    try:
        with open(CARD_MANIFEST_FILE, "r") as manifest_file:
            return json.load(manifest_file)
    except (OSError, ValueError):
        return {}

def save_card_manifests(manifests):
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    temporary = f"{CARD_MANIFEST_FILE}.tmp"
    with open(temporary, "w") as manifest_file:
        json.dump(manifests, manifest_file)
    os.replace(temporary, CARD_MANIFEST_FILE)

def open_import_db():
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(IMPORT_DB_FILE, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY,
            content_hash TEXT UNIQUE,
            source_filename TEXT NOT NULL,
            source_relative_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            modified_time REAL NOT NULL,
            created_time REAL NOT NULL,
            imported_at TEXT,
            destination_raw TEXT,
            destination_protected TEXT,
            status TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_imports_size ON imports(file_size)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_imports_source ON imports(source_relative_path)")
    conn.commit()
    migrate_legacy_tracking(conn)
    return conn

def migrate_legacy_tracking(conn):
    """Import old path/size/ctime records without treating paths as new identity."""
    if conn.execute("SELECT 1 FROM imports LIMIT 1").fetchone():
        return
    legacy = load_copied_tracking()
    for source_path, entry in legacy.items():
        try:
            source_path = os.path.normcase(os.path.normpath(source_path))
            size = int(entry.get("size", 0))
            ctime = float(entry.get("ctime", 0))
            content_hash = sha256_file(source_path) if os.path.isfile(source_path) else None
            conn.execute("""
                INSERT OR IGNORE INTO imports
                (content_hash, source_filename, source_relative_path, file_size,
                 modified_time, created_time, imported_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'IMPORTED')
            """, (content_hash, os.path.basename(source_path), source_path, size,
                   0, ctime, entry.get("copied_at")))
        except Exception:
            continue
    conn.commit()

def sha256_file(filepath):
    digest = hashlib.sha256()
    with open(filepath, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def find_import_by_hash(conn, content_hash):
    return conn.execute("""
        SELECT id, destination_raw, status FROM imports WHERE content_hash = ?
    """, (content_hash,)).fetchone()

def find_legacy_import(conn, source_path, file_size, created_time):
    source_path = os.path.normcase(os.path.normpath(source_path))
    return conn.execute("""
        SELECT id, content_hash, destination_raw, status FROM imports
        WHERE source_relative_path = ? AND file_size = ? AND created_time = ?
    """, (source_path, file_size, created_time)).fetchone()

def load_copied_tracking():
    try:
        if os.path.exists(COPIED_FILES_FILE):
            with open(COPIED_FILES_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_copied_tracking(data):
    try:
        with open(COPIED_FILES_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

def is_already_copied(source_path, tracking):
    entry = tracking.get(source_path)
    if not entry:
        return False
    try:
        current_size = os.path.getsize(source_path)
        current_ctime = os.path.getctime(source_path)
        return entry.get("size") == current_size and entry.get("ctime") == current_ctime
    except:
        return False

def get_next_sd_counter():
    instance_id = str(uuid.uuid4())[:8]

    try:
        if os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE, 'r') as f:
                data = json.load(f)
        else:
            data = {"sd_counter": 0, "instances": {}}
    except:
        data = {"sd_counter": 0, "instances": {}}

    counter = data.get("sd_counter", 0) + 1
    data["sd_counter"] = counter
    data["instances"][instance_id] = counter

    try:
        with open(COUNTER_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

    return counter, instance_id

def generate_sd_filename(original_path, dest_folder):
    camera_name = os.path.splitext(os.path.basename(original_path))[0]
    counter, _ = get_next_sd_counter()
    filename = f"{camera_name}_{counter:04d}.jpg"

    while os.path.exists(os.path.join(dest_folder, filename)):
        counter += 1
        filename = f"{camera_name}_{counter:04d}.jpg"

        try:
            if os.path.exists(COUNTER_FILE):
                with open(COUNTER_FILE, 'r') as f:
                    data = json.load(f)
            else:
                data = {"sd_counter": 0, "instances": {}}
            data["sd_counter"] = counter
            with open(COUNTER_FILE, 'w') as f:
                json.dump(data, f)
        except:
            pass

    return filename

class TransferNotification:
    def __init__(self, parent, count, dest):
        self.count = count
        self.dest = dest
        self.window = tk.Toplevel(parent)
        if icon_path := getattr(parent, "photoman_icon_path", None):
            self.window.iconbitmap(icon_path)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)

        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        win_w, win_h = 350, 120
        x = screen_w - win_w - 20
        y = screen_h - win_h - 60

        self.window.geometry(f"{win_w}x{win_h}+{x}+{y}")

        self.canvas = tk.Canvas(self.window, width=win_w, height=win_h, bg="#1a1a2e", highlightthickness=0)
        self.canvas.pack()

        self.alpha = 0.0
        self.fade_dir = 1

        self.canvas.create_oval(15, 25, 55, 65, fill="#00d4aa", tags="check")
        self.canvas.create_text(35, 45, text="✓", fill="white", font=("Arial", 24, "bold"), tags="check_sym")

        self.canvas.create_text(70, 35, text="TRANSFER COMPLETE!", fill="#00d4aa", font=("Arial", 13, "bold"), anchor="w")
        self.canvas.create_text(70, 58, text=f"{count} files copied", fill="#ffffff", font=("Arial", 10), anchor="w")
        self.canvas.create_text(70, 78, text=f"→ {os.path.basename(dest)}", fill="#888888", font=("Arial", 9), anchor="w")

        self.animate()
        self.window.after(3000, self.fade_out)

    def animate(self):
        if self.fade_dir == 1:
            self.alpha = min(1.0, self.alpha + 0.1)
            self.window.attributes("-alpha", self.alpha)
            if self.alpha < 1.0:
                self.window.after(30, self.animate)
        else:
            self.alpha = max(0, self.alpha - 0.1)
            self.window.attributes("-alpha", self.alpha)
            if self.alpha > 0:
                self.window.after(30, self.animate)
            else:
                self.window.destroy()

    def fade_out(self):
        self.fade_dir = -1
        self.animate()

class SalikhaStudioPro:
    def __init__(self, root, license_manager=None):
        if license_manager is not None:
            license_manager.require()
        self.license_manager = license_manager
        self.root = root
        self.root.title("Salikha Studio - PRO")
        self.root.geometry("1280x860")
        self.root.minsize(1050, 700)

        self.style = ttk.Style(self.root)
        self.style.theme_use('clam')
        self.setup_theme()

        # --- TRAFFIC CONTROL (Engine) ---
        self.input_queue = queue.Queue()
        self.gui_queue = queue.Queue()
        self.processed_files = set()
        self.queue_lock = threading.Lock()

        self.boxes = []
        self.box_items = []
        self.box_labels = []
        self.template_path = ""
        self.template_orientation = "Landscape"
        self.template_profiles = {"Landscape": {}, "Portrait": {}}
        self.print_count = 0
        self.print_queue = queue.Queue()
        self.print_queue_pending = 0
        self.print_queue_total = 0
        self.print_queue_completed = 0
        self.print_queue_lock = threading.Lock()
        self.is_running = False
        self.engine_start_time = 0
        self.last_printed_file = None # FAILSAFE: Memory for the last print

        # --- TRAFFIC CONTROL (Sorter) ---
        self.is_sorting = False
        self.sorter_thread = None
        self.sd_destination_lock = threading.Lock()
        self.sorter_start_time = 0

        # Defaults Engine
        self.source_folder = os.path.abspath("./hot_input")
        self.output_folder = os.path.abspath("./prints_archive")

        # Defaults Sorter
        self.sd_source = "F:\\DCIM"
        self.sd_dest_prot = ""
        self.sd_dest_raw = ""
        self.event_name = ""
        self.mother_folder = ""
        self.event_folder = ""

        # Sharpen (0-100%)
        self.sharpen_value = 0

        # Instance ID for counter
        self.instance_id = str(uuid.uuid4())[:8]

        for f in [self.source_folder, self.output_folder]:
            if not os.path.exists(f): os.makedirs(f)

        # --- APPLICATION SHELL ---
        self.sidebar_collapsed = False
        self.views = {}
        self.nav_buttons = {}
        self.app_shell = tk.Frame(root, bg=self.colors["soft"])
        self.app_shell.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(self.app_shell, bg="#24151d", width=230)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.sidebar_header = tk.Frame(self.sidebar, bg="#24151d", height=76)
        self.sidebar_header.pack(fill="x")
        self.sidebar_header.pack_propagate(False)
        self.brand_label = tk.Label(self.sidebar_header, text="SALIKHA\nSTUDIO PRO", bg="#24151d", fg="white",
                                    font=("Segoe UI", 13, "bold"), justify="left")
        self.brand_label.pack(side="left", padx=20, pady=14)
        self.collapse_btn = tk.Button(self.sidebar_header, text="‹", command=self.toggle_sidebar,
                                      bg="#24151d", fg="#d9b9c5", activebackground="#3b222e",
                                      activeforeground="white", relief="flat", bd=0, font=("Segoe UI", 18), width=2)
        self.collapse_btn.pack(side="right", padx=8)
        self.nav_area = tk.Frame(self.sidebar, bg="#24151d")
        self.nav_area.pack(fill="both", expand=True, padx=10, pady=12)

        self.content_shell = tk.Frame(self.app_shell, bg=self.colors["soft"])
        self.content_shell.pack(side="left", fill="both", expand=True)
        self.header = tk.Frame(self.content_shell, bg="white", height=68, bd=0)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.page_title = tk.Label(self.header, text="Dashboard", bg="white", fg=self.colors["text"],
                                   font=("Segoe UI", 18, "bold"))
        self.page_title.pack(side="left", padx=28)
        self.header_status = tk.Label(self.header, text="● READY", bg="white", fg=self.colors["success"],
                                      font=("Segoe UI", 10, "bold"))
        self.header_status.pack(side="right", padx=28)
        self.page_area = tk.Frame(self.content_shell, bg=self.colors["soft"])
        self.page_area.pack(fill="both", expand=True)

        self.dashboard_view = tk.Frame(self.page_area, bg=self.colors["soft"])
        self.views["Dashboard"] = self.dashboard_view
        self.setup_dashboard()

        self.event_tab = ttk.Frame(self.page_area, padding=18)
        self.views["Event Setup"] = self.event_tab
        self.setup_event()

        self.design_tab = ttk.Frame(self.page_area, padding=8)
        self.views["Template Designer"] = self.design_tab
        self.setup_designer()

        self.engine_tab = ttk.Frame(self.page_area, padding=8)
        self.views["Print Monitor"] = self.engine_tab
        self.setup_engine()

        self.gallery_view = ttk.Frame(self.page_area, padding=18)
        self.views["Photo Gallery"] = self.gallery_view
        self.setup_gallery()

        self.sorter_tab = ttk.Frame(self.page_area, padding=8)
        self.setup_sorter()

        self.settings_tab = ttk.Frame(self.page_area, padding=18)
        self.views["Settings"] = self.settings_tab
        self.setup_settings()

        self.logs_view = ttk.Frame(self.page_area, padding=18)
        self.views["Logs & Diagnostics"] = self.logs_view
        ttk.Label(self.logs_view, text="Logs & Diagnostics", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self.logs_view, text="Live engine and importer logs are available from their respective workspaces.",
                  foreground=self.colors["muted"]).pack(anchor="w", pady=(4, 18))
        self.import_log_box = scrolledtext.ScrolledText(self.logs_view, height=18, font=("Consolas", 9), bg="#fbfafb")
        self.import_log_box.pack(fill="both", expand=True, pady=(0, 14))
        ttk.Button(self.logs_view, text="Open Print Monitor", command=lambda: self.show_view("Print Monitor")).pack(anchor="w")
        ttk.Button(self.logs_view, text="Open Import Settings", command=lambda: self.show_view("Settings")).pack(anchor="w", pady=8)

        nav_items = [("⌂", "Dashboard"), ("▣", "Event Setup"), ("⇩", "Print Monitor"),
                     ("▤", "Photo Gallery"), ("◇", "Template Designer"), ("⚙", "Settings"), ("≡", "Logs & Diagnostics")]
        for icon, name in nav_items:
            self.add_nav_button(icon, name)
        self.show_view("Dashboard")

        self.load_config()
        self.process_gui_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)
        if self.license_manager is not None:
            self.root.after(2000, self.check_license_session)

    def check_license_session(self):
        if getattr(self, "closing", False):
            return
        if getattr(self, "license_dialog_open", False):
            self.root.after(2000, self.check_license_session)
            return
        from licensing import LicenseError
        try:
            self.license_manager.require()
        except LicenseError:
            self.stop_engine()
            if not self.open_license_dialog(required=True):
                self.close_app()
                return
        else:
            self.license_manager.refresh_in_background()
        self.root.after(2000, self.check_license_session)

    def open_license_dialog(self, required=False):
        from licensing_ui import show_license_dialog
        self.license_dialog_open = True
        try:
            return show_license_dialog(self.root, self.license_manager, required=required)
        finally:
            self.license_dialog_open = False

    def manage_license(self):
        if getattr(self, "license_dialog_open", False):
            return
        if self.is_running or self.is_sorting or self.print_queue_pending:
            return messagebox.showinfo("License", "Stop the system and finish pending prints before managing the license.")
        if not self.open_license_dialog() and not self.open_license_dialog(required=True):
            self.close_app()

    def require_license(self, notify=True):
        from licensing import LicenseError
        try:
            if self.license_manager is None:
                raise LicenseError("License service is unavailable. Restart the app using its launcher.")
            self.license_manager.require()
            self.license_manager.refresh_in_background()
            return True
        except LicenseError as exc:
            if notify:
                messagebox.showerror("License required", str(exc))
            else:
                self.queue_log(f"License required. {exc}")
            return False

    def close_app(self):
        if getattr(self, "closing", False):
            return
        self.closing = True
        self.save_config(silent=True)
        self.stop_engine()
        self.root.destroy()

    def setup_theme(self):
        self.colors = {
            "wine": "#7A1F3D", "wine_dark": "#54152A", "wine_light": "#F3E5EA",
            "surface": "#FFFFFF", "soft": "#F8F5F6", "text": "#2B2528",
            "muted": "#6F686B", "border": "#DDD5D8", "success": "#287A52",
            "warning": "#B7791F", "error": "#B42318", "info": "#2563EB"
        }
        self.root.configure(bg=self.colors["soft"])
        self.root.option_add("*Button.Cursor", "hand2")
        self.style.configure("TFrame", background=self.colors["surface"])
        self.style.configure("TLabel", background=self.colors["surface"], foreground=self.colors["text"], font=("Segoe UI", 10))
        self.style.configure("Heading.TLabel", font=("Segoe UI Semibold", 18), foreground=self.colors["wine"])
        self.style.configure("Subheading.TLabel", font=("Segoe UI Semibold", 11), foreground=self.colors["text"])
        self.style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))
        self.style.configure("Secondary.TButton", font=("Segoe UI", 9), padding=(9, 6),
                             background="#f1ecef", foreground=self.colors["text"])
        self.style.map("Secondary.TButton", background=[("active", self.colors["wine_light"])])
        self.style.configure("Primary.TButton", background=self.colors["wine"], foreground="white", padding=(14, 9))
        self.style.map("Primary.TButton", background=[("active", self.colors["wine_dark"]), ("disabled", "#BBA5AD")])
        self.style.configure("TNotebook", background=self.colors["soft"], borderwidth=0)
        self.style.configure("TNotebook.Tab", padding=(18, 9), font=("Segoe UI Semibold", 10))
        self.style.map("TNotebook.Tab", background=[("selected", self.colors["wine"]), ("active", self.colors["wine_light"])],
                       foreground=[("selected", "white"), ("!selected", self.colors["muted"])])
        self.style.configure("TLabelframe", background=self.colors["surface"], bordercolor=self.colors["border"])
        self.style.configure("TLabelframe.Label", background=self.colors["surface"], foreground=self.colors["wine"], font=("Segoe UI Semibold", 10))
        self.style.configure("TEntry", padding=7, fieldbackground="white")
        self.style.configure("TCombobox", padding=5)

    def add_nav_button(self, icon, name):
        button = tk.Button(self.nav_area, text=f"  {icon}   {name}", command=lambda: self.show_view(name),
                           anchor="w", bg="#24151d", fg="#d9b9c5", activebackground="#7A1F3D",
                           activeforeground="white", relief="flat", bd=0, font=("Segoe UI", 10),
                           padx=12, pady=11)
        button.pack(fill="x", pady=2)
        self.nav_buttons[name] = (button, icon, name)

    def show_view(self, name):
        self.current_view = name
        for view in self.views.values():
            view.pack_forget()
        view = self.views.get(name)
        if view:
            view.pack(fill="both", expand=True)
        if name == "Photo Gallery" and getattr(self, "gallery_stale", False):
            self.refresh_gallery()
            self.gallery_stale = False
        self.page_title.config(text=name)
        for item_name, (button, icon, label) in self.nav_buttons.items():
            selected = item_name == name
            button.config(bg="#7A1F3D" if selected else "#24151d",
                          fg="white" if selected else "#d9b9c5")

    def toggle_sidebar(self):
        self.sidebar_collapsed = not self.sidebar_collapsed
        width = 76 if self.sidebar_collapsed else 230
        self.sidebar.config(width=width)
        self.brand_label.config(text="S\nP" if self.sidebar_collapsed else "SALIKHA\nSTUDIO PRO")
        self.collapse_btn.config(text="›" if self.sidebar_collapsed else "‹")
        for button, icon, label in self.nav_buttons.values():
            button.config(text=f"  {icon}" if self.sidebar_collapsed else f"  {icon}   {label}")

    def setup_dashboard(self):
        self.dashboard_view.columnconfigure(0, weight=1)
        self.dashboard_view.rowconfigure(2, weight=1)
        tk.Label(self.dashboard_view, text="Good day, Salikha team", bg=self.colors["soft"],
                 fg=self.colors["text"], font=("Segoe UI", 22, "bold")).grid(row=0, column=0, sticky="w", padx=28, pady=(28, 2))
        tk.Label(self.dashboard_view, text="Your fast-paced photo workflow at a glance.", bg=self.colors["soft"],
                 fg=self.colors["muted"], font=("Segoe UI", 10)).grid(row=1, column=0, sticky="w", padx=28, pady=(0, 22))
        cards = tk.Frame(self.dashboard_view, bg=self.colors["soft"])
        cards.grid(row=2, column=0, sticky="new", padx=28)
        for col in range(4):
            cards.columnconfigure(col, weight=1)
        dashboard_cards = [("SYSTEM STATUS", "READY", self.colors["success"]),
                           ("ACTIVE EVENT", "No event selected", self.colors["wine"]),
                           ("PRINTS COMPLETED", "0", self.colors["info"]),
                           ("IMPORT STATUS", "Not running", self.colors["muted"])]
        for col, (title, value, color) in enumerate(dashboard_cards):
            card = tk.Frame(cards, bg="white", highlightbackground="#e4dce0", highlightthickness=1)
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0), pady=2)
            tk.Label(card, text=title, bg="white", fg=self.colors["muted"], font=("Segoe UI", 9, "bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(16, 7))
            tk.Label(card, text=value, bg="white", fg=color, font=("Segoe UI", 15, "bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(0, 18))
        activity = tk.Frame(self.dashboard_view, bg="white", highlightbackground="#e4dce0", highlightthickness=1)
        activity.grid(row=3, column=0, sticky="nsew", padx=28, pady=(22, 28))
        tk.Label(activity, text="Quick Start", bg="white", fg=self.colors["text"],
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=18, pady=(18, 4))
        tk.Label(activity, text="Create or select an event, configure your templates, then start both importing and printing with one button.",
                 bg="white", fg=self.colors["muted"], wraplength=760, justify="left").pack(anchor="w", padx=18, pady=(0, 16))
        ttk.Button(activity, text="Open Event Setup", command=lambda: self.show_view("Event Setup")).pack(anchor="w", padx=18, pady=(0, 18))

    def setup_event(self):
        self.event_tab.columnconfigure(0, weight=1)
        self.event_tab.rowconfigure(3, weight=1)
        ttk.Label(self.event_tab, text="Event Setup", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.event_tab, text="Create a complete PhotoMan workspace before importing photos.", foreground=self.colors["muted"]).grid(row=1, column=0, sticky="w", pady=(2, 18))

        form = ttk.LabelFrame(self.event_tab, text="Event Workspace", padding=16)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.event_name_var = tk.StringVar(value=self.event_name)
        self.mother_folder_var = tk.StringVar(value=self.mother_folder)
        self.event_preview_var = tk.StringVar(value="Select a mother folder and enter an event name.")
        ttk.Label(form, text="Event name").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=7)
        event_entry = ttk.Entry(form, textvariable=self.event_name_var)
        event_entry.grid(row=0, column=1, sticky="ew", pady=7)
        event_entry.bind("<KeyRelease>", lambda event: self.update_event_preview())
        ttk.Label(form, text="Mother folder").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=7)
        ttk.Entry(form, textvariable=self.mother_folder_var).grid(row=1, column=1, sticky="ew", pady=7)
        ttk.Button(form, text="Browse", command=self.select_mother_folder).grid(row=1, column=2, padx=(8, 0), pady=7)
        ttk.Label(form, textvariable=self.event_preview_var, foreground=self.colors["muted"], wraplength=900).grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 4))
        ttk.Button(form, text="Create Event Workspace", style="Primary.TButton", command=self.create_event_workspace).grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))

        self.event_status = ttk.Label(self.event_tab, text="No event workspace selected.", foreground=self.colors["muted"])
        self.event_status.grid(row=3, column=0, sticky="nw", pady=(20, 0))

    def setup_settings(self):
        self.settings_tab.columnconfigure(0, weight=1)
        ttk.Label(self.settings_tab, text="Settings", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.settings_tab, text="Keep the common controls here so the working screens stay simple.", foreground=self.colors["muted"]).grid(row=1, column=0, sticky="w", pady=(2, 18))
        settings = ttk.Frame(self.settings_tab)
        settings.grid(row=2, column=0, sticky="nw")
        self.auto_print_var = tk.BooleanVar(value=False)
        self.landscape_enabled_var = tk.BooleanVar(value=True)
        self.portrait_enabled_var = tk.BooleanVar(value=True)
        self.verify_copy_var = tk.BooleanVar(value=True)
        self.import_scan_interval_var = tk.DoubleVar(value=0.5)
        self.import_workers_var = tk.IntVar(value=10)
        self.sound_notifications_var = tk.BooleanVar(value=True)
        orientation = ttk.LabelFrame(settings, text="Print Sources", padding=14)
        orientation.grid(row=0, column=0, sticky="nw", padx=(0, 16))
        ttk.Checkbutton(orientation, text="Enable Landscape queue", variable=self.landscape_enabled_var).pack(anchor="w", pady=4)
        ttk.Checkbutton(orientation, text="Enable Portrait queue", variable=self.portrait_enabled_var).pack(anchor="w", pady=4)
        ttk.Label(orientation, text="Only locked photos are sent to these queues.", foreground=self.colors["muted"]).pack(anchor="w", pady=(8, 0))
        behavior = ttk.LabelFrame(settings, text="Workflow", padding=14)
        behavior.grid(row=0, column=1, sticky="nw")
        ttk.Checkbutton(behavior, text="Automatically print new queued photos", variable=self.auto_print_var).pack(anchor="w", pady=4)
        ttk.Checkbutton(behavior, text="Verify every copied photo", variable=self.verify_copy_var).pack(anchor="w", pady=4)
        ttk.Checkbutton(behavior, text="Transfer complete notifications", variable=self.sound_notifications_var).pack(anchor="w", pady=4)
        ttk.Label(behavior, text="Automatic printing is off by default.", foreground=self.colors["muted"]).pack(anchor="w", pady=(8, 0))
        importer = ttk.LabelFrame(self.settings_tab, text="Photo Import Configuration", padding=14)
        importer.grid(row=3, column=0, sticky="ew", pady=(22, 0))
        importer.columnconfigure(1, weight=1)
        ttk.Label(importer, text="SD card source").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        tk.Button(importer, textvariable=self.src_path_var, command=self.select_sorter_source, anchor="w",
                  relief="groove", bg="white").grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(importer, text="Locked destination").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=5)
        tk.Button(importer, textvariable=self.dest_prot_var, command=self.select_sorter_dest_prot, anchor="w",
                  relief="groove", bg="white").grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(importer, text="RAW destination").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=5)
        tk.Button(importer, textvariable=self.dest_raw_var, command=self.select_sorter_dest_raw, anchor="w",
                  relief="groove", bg="white").grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(importer, text="Scan interval (seconds)").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=5)
        ttk.Spinbox(importer, from_=0.1, to=30, increment=0.1, format="%0.1f",
                    textvariable=self.import_scan_interval_var, width=8).grid(row=3, column=1, sticky="w", pady=5)
        ttk.Label(importer, text="Copy workers (parallel)").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=5)
        ttk.Spinbox(importer, from_=1, to=32, textvariable=self.import_workers_var, width=8).grid(row=4, column=1, sticky="w", pady=5)
        ttk.Label(importer, text="Locked and RAW destinations follow the active event workspace.",
                  foreground=self.colors["muted"]).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(importer, text="You can start the system before inserting the SD card; it will resume automatically.",
                  foreground=self.colors["muted"]).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Button(self.settings_tab, text="Save Settings", style="Primary.TButton", command=self.save_config).grid(row=4, column=0, sticky="w", pady=24)
        ttk.Button(self.settings_tab, text="Manage License", command=self.manage_license).grid(row=5, column=0, sticky="w", pady=10)

    def select_mother_folder(self):
        path = filedialog.askdirectory(title="Select PhotoMan mother folder")
        if path:
            self.mother_folder_var.set(path)
            self.update_event_preview()

    def update_event_preview(self):
        name = self.event_name_var.get().strip()
        mother = self.mother_folder_var.get().strip()
        if not name or not mother:
            self.event_preview_var.set("Select a mother folder and enter an event name.")
            return
        safe_name = "_".join(name.split())
        self.event_preview_var.set(f"Will create: {os.path.join(mother, time.strftime('%Y-%m-%d') + '_' + safe_name)}")

    def create_event_workspace(self):
        name = self.event_name_var.get().strip()
        mother = self.mother_folder_var.get().strip()
        if not name or not mother:
            messagebox.showwarning("Event Setup", "Enter an event name and select a mother folder.")
            return
        safe_name = "_".join(name.split())
        event_folder = os.path.join(mother, time.strftime("%Y-%m-%d") + "_" + safe_name)
        folders = [
            os.path.join(event_folder, "01_Raw_Archive"),
            os.path.join(event_folder, "02_Locked", "Landscape"),
            os.path.join(event_folder, "02_Locked", "Portrait"),
            os.path.join(event_folder, "03_Print_Output", "Landscape"),
            os.path.join(event_folder, "03_Print_Output", "Portrait"),
            os.path.join(event_folder, "04_Failed"),
            os.path.join(event_folder, "05_Logs")
        ]
        try:
            for folder in folders:
                os.makedirs(folder, exist_ok=True)
            self.event_name = name
            self.mother_folder = mother
            self.event_folder = event_folder
            self.sd_dest_raw = os.path.join(event_folder, "01_Raw_Archive")
            self.sd_dest_prot = os.path.join(event_folder, "02_Locked")
            self.source_folder = self.sd_dest_prot
            self.output_folder = os.path.join(event_folder, "03_Print_Output")
            self.dest_raw_var.set(self.sd_dest_raw)
            self.dest_prot_var.set(self.sd_dest_prot)
            self.out_entry.delete(0, tk.END)
            self.out_entry.insert(0, self.output_folder)
            self.event_status.config(text=f"Active event: {name}\nWorkspace: {event_folder}", foreground=self.colors["success"])
            self.save_config(silent=True)
            messagebox.showinfo("Event Ready", f"Workspace created successfully.\n\n{event_folder}")
        except OSError as error:
            messagebox.showerror("Event Setup Error", str(error))

    def process_gui_queue(self):
        try:
            while True:
                task = self.gui_queue.get_nowait()
                action = task.get("action")

                if action == "log":
                    self.log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {task['msg']}\n")
                    self.log_box.see(tk.END)
                elif action == "sorter_log":
                    self.sorter_log_area.config(state='normal')
                    self.sorter_log_area.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {task['msg']}\n")
                    self.sorter_log_area.see(tk.END)
                    self.sorter_log_area.config(state='disabled')
                    if hasattr(self, "import_log_box"):
                        self.import_log_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {task['msg']}\n")
                        self.import_log_box.see(tk.END)
                elif action == "preview":
                    self.tk_p = ImageTk.PhotoImage(task['image'])
                    self.prev_lbl.config(image=self.tk_p, text="")
                elif action == "update_count":
                    self.count_lbl.config(text=f"Total Prints\n{task['count']}")
                elif action == "import_progress":
                    total = max(1, task.get("total", 0))
                    self.import_progress.config(maximum=total, value=task.get("current", 0))
                    self.import_progress_status.set(task.get("message", "SD card transfer"))
                elif action == "print_queue_progress":
                    total = max(1, task.get("total", 0))
                    self.print_queue_progress.config(maximum=total, value=task.get("current", 0))
                    self.print_progress_status.set(task.get("message", "Print queue"))
                elif action == "print_group_progress":
                    self.group_progress_status.set(task.get("message", "Print group"))
                elif action == "gallery_refresh":
                    self.gallery_stale = True
                    if getattr(self, "current_view", "") == "Photo Gallery":
                        self.refresh_gallery()
                        self.gallery_stale = False
                elif action == "gallery_thumbnail":
                    if task.get("generation") == getattr(self, "gallery_generation", 0):
                        card = self.gallery_card_map.get(task.get("card_id"))
                        if card and os.path.exists(task["proxy"]):
                            with Image.open(task["proxy"]) as image:
                                photo = ImageTk.PhotoImage(image.copy())
                            self.gallery_images.append(photo)
                            card.preview.config(image=photo, text="")
                elif action == "clear_prev":
                    self.prev_lbl.config(image='', text="Waiting for next group...")
                elif action == "enable_reprint":
                    self.reprint_btn.config(state="normal")

                self.gui_queue.task_done()
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_gui_queue)

    # ========================================================
    # TAB 1: DESIGNER
    # ========================================================
    def setup_designer(self):
        control_shell = tk.Frame(self.design_tab, width=300, bg="#f8f9fa")
        control_shell.pack(side="left", fill="y", padx=10, pady=10)
        control_shell.pack_propagate(False)
        control_scroll = ttk.Scrollbar(control_shell, orient="vertical")
        control_scroll.pack(side="right", fill="y")
        control_canvas = tk.Canvas(control_shell, bg="#f8f9fa", highlightthickness=0, width=280)
        control_canvas.configure(yscrollcommand=control_scroll.set)
        control_canvas.pack(side="left", fill="both", expand=True)
        control_scroll.configure(command=control_canvas.yview)
        ctrl = tk.Frame(control_canvas, width=280, bg="#f8f9fa", relief="ridge", bd=2)
        control_window = control_canvas.create_window((0, 0), window=ctrl, anchor="nw")
        ctrl.bind("<Configure>", lambda event: control_canvas.configure(scrollregion=control_canvas.bbox("all")))
        control_canvas.bind("<Configure>", lambda event: control_canvas.itemconfigure(control_window, width=event.width))
        control_shell.bind("<Enter>", lambda event: control_canvas.bind_all("<MouseWheel>", lambda wheel: control_canvas.yview_scroll(-int(wheel.delta / 120), "units")))
        control_shell.bind("<Leave>", lambda event: control_canvas.unbind_all("<MouseWheel>"))

        tk.Label(ctrl, text="Design Controls", font=("Arial", 14, "bold"), bg="#f8f9fa").pack(pady=(15, 5))
        orientation_frame = ttk.LabelFrame(ctrl, text="Template Orientation", padding=8)
        orientation_frame.pack(fill="x", padx=15, pady=(4, 10))
        self.template_orientation_var = tk.StringVar(value="Landscape")
        ttk.Radiobutton(orientation_frame, text="Landscape template", variable=self.template_orientation_var,
                        value="Landscape", command=lambda: self.switch_template_orientation("Landscape")).pack(anchor="w", pady=2)
        ttk.Radiobutton(orientation_frame, text="Portrait template", variable=self.template_orientation_var,
                        value="Portrait", command=lambda: self.switch_template_orientation("Portrait")).pack(anchor="w", pady=2)
        ttk.Button(ctrl, text="Load Overlay PNG", command=self.load_template).pack(fill="x", padx=15, pady=5)

        ratio_frame = tk.LabelFrame(ctrl, text="Box Aspect Ratio", bg="#f8f9fa")
        ratio_frame.pack(fill="x", padx=15, pady=10)

        self.box_ratio = tk.StringVar(value="Landscape (3:2)")
        ttk.Radiobutton(ratio_frame, text="Landscape (3:2)", variable=self.box_ratio, value="Landscape (3:2)").pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(ratio_frame, text="Portrait (2:3)", variable=self.box_ratio, value="Portrait (2:3)").pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(ratio_frame, text="Landscape (4:3)", variable=self.box_ratio, value="Landscape (4:3)").pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(ratio_frame, text="Portrait (3:4)", variable=self.box_ratio, value="Portrait (3:4)").pack(anchor="w", padx=10, pady=2)
        ttk.Radiobutton(ratio_frame, text="Freeform", variable=self.box_ratio, value="Freeform").pack(anchor="w", padx=10, pady=2)
        self.lock_ratio_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ratio_frame, text="Lock shape when resizing", variable=self.lock_ratio_var).pack(anchor="w", padx=10, pady=(4, 1))
        self.show_crop_guide_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ratio_frame, text="Show 3:2 crop guide", variable=self.show_crop_guide_var,
                        command=self._refresh_crop_guide).pack(anchor="w", padx=10, pady=(0, 4))

        action_frame = tk.LabelFrame(ctrl, text="Box Actions", bg="#f8f9fa")
        action_frame.pack(fill="x", padx=15, pady=10)
        action_top = tk.Frame(action_frame, bg="#f8f9fa")
        action_top.pack(fill="x", padx=5, pady=(2, 3))
        tk.Button(action_top, text="Duplicate Selected", command=self.duplicate_box, bg="#007bff", fg="white",
                  font=("Arial", 9, "bold")).pack(side="left", fill="x", expand=True)
        tk.Button(action_top, text="Delete Selected", command=self.delete_selected_box, bg="#ff4c4c", fg="white",
                  font=("Arial", 9, "bold")).pack(side="left", fill="x", expand=True, padx=(6, 0))
        action_bottom = tk.Frame(action_frame, bg="#f8f9fa")
        action_bottom.pack(fill="x", padx=5, pady=(2, 4))
        tk.Label(action_bottom, text="Source:", bg="#f8f9fa").pack(side="left")
        self.dup_src_var = tk.StringVar(value="1")
        self.dup_src_combo = ttk.Combobox(action_bottom, textvariable=self.dup_src_var, values=[str(i) for i in range(1, 11)], width=5, state="readonly")
        self.dup_src_combo.pack(side="left", padx=5)

        coord_frame = tk.LabelFrame(ctrl, text="Selected Box Size & Position", bg="#f8f9fa")
        coord_frame.pack(fill="x", padx=15, pady=10)

        coord_top = tk.Frame(coord_frame, bg="#f8f9fa")
        coord_top.pack(fill="x", padx=5, pady=2)
        tk.Label(coord_top, text="Box#:", bg="#f8f9fa").pack(side="left")
        self.coord_idx_var = tk.StringVar(value="1")
        self.coord_idx_combo = ttk.Combobox(coord_top, textvariable=self.coord_idx_var, values=[str(i) for i in range(1, 11)], width=5, state="readonly")
        self.coord_idx_combo.pack(side="left", padx=5)
        tk.Button(coord_top, text="Load", command=self.load_coord_box, width=6).pack(side="left", padx=2)

        coord_grid = tk.Frame(coord_frame, bg="#f8f9fa")
        coord_grid.pack(fill="x", padx=5, pady=2)
        tk.Label(coord_grid, text="X1:", bg="#f8f9fa", width=4).grid(row=0, column=0)
        self.coord_x1 = tk.Entry(coord_grid, width=8)
        self.coord_x1.grid(row=0, column=1, padx=2)
        tk.Label(coord_grid, text="Y1:", bg="#f8f9fa", width=4).grid(row=0, column=2)
        self.coord_y1 = tk.Entry(coord_grid, width=8)
        self.coord_y1.grid(row=0, column=3, padx=2)
        tk.Label(coord_grid, text="X2:", bg="#f8f9fa", width=4).grid(row=1, column=0)
        self.coord_x2 = tk.Entry(coord_grid, width=8)
        self.coord_x2.grid(row=1, column=1, padx=2)
        tk.Label(coord_grid, text="Y2:", bg="#f8f9fa", width=4).grid(row=1, column=2)
        self.coord_y2 = tk.Entry(coord_grid, width=8)
        self.coord_y2.grid(row=1, column=3, padx=2)
        tk.Label(coord_grid, text="W:", bg="#f8f9fa", width=4).grid(row=2, column=0)
        self.coord_w = tk.Entry(coord_grid, width=8)
        self.coord_w.grid(row=2, column=1, padx=2)
        tk.Label(coord_grid, text="H:", bg="#f8f9fa", width=4).grid(row=2, column=2)
        self.coord_h = tk.Entry(coord_grid, width=8)
        self.coord_h.grid(row=2, column=3, padx=2)
        tk.Button(coord_frame, text="Apply Changes", command=self.apply_coord_changes, bg="#28a745", fg="white", font=("Arial", 9, "bold")).pack(fill="x", padx=5, pady=3)

        tk.Label(ctrl, text="Drag to draw a box. Click a box to select it. Drag its handles to resize, or set W/H above. Delete removes only the selected box.",
                 fg="#555", bg="#f8f9fa", justify="left", wraplength=240).pack(padx=15, pady=5)
        tk.Button(ctrl, text="Clear All Boxes", command=self.clear_boxes, bg="#ff4c4c", fg="white", font=("Arial", 10, "bold")).pack(fill="x", padx=15, pady=(14, 5))
        tk.Button(ctrl, text="SAVE CONFIG", command=self.save_config, bg="#28a745", fg="white", font=("Arial", 11, "bold"), height=2).pack(fill="x", padx=15, pady=18)

        center_frame = tk.Frame(self.design_tab, bg="#e9ecef")
        center_frame.pack(side="right", expand=True, fill="both", padx=10, pady=10)

        self.canvas_wrapper = tk.Frame(center_frame, relief="solid", bd=1)
        self.canvas_wrapper.pack(expand=True)

        self.canvas = tk.Canvas(self.canvas_wrapper, bg="#333", width=600, height=800, cursor="cross")
        self.canvas.pack()

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Delete>", lambda event: self.delete_selected_box())
        self.canvas.bind("<BackSpace>", lambda event: self.delete_selected_box())
        self.canvas.bind("<Escape>", lambda event: self._deselect())

        self.action = None
        self.selected_item = None
        self.selected_index = None
        self.handle_regions = {}
        self.resize_handle = None
        self.resize_start = (0, 0)
        self.resize_box = [0, 0, 0, 0]
        self._coord_baseline = None
        self.scale = getattr(self, "scale", 1) or 1
        self._refresh_box_pickers()

    def _template_state(self):
        return {"path": self.template_path, "boxes": self.boxes[:], "labels": self.box_labels[:]}

    def _restore_template_state(self, state):
        self.template_path = state.get("path", "")
        self.boxes = state.get("boxes", [])[:]
        self.box_labels = state.get("labels", [])[:]
        self.box_items = []
        self.selected_item = None
        self.selected_index = None
        self.handle_regions = {}
        self.canvas.delete("all")
        if self.template_path and os.path.exists(self.template_path):
            with Image.open(self.template_path) as img:
                image_width = img.width
                disp = img.copy()
            disp.thumbnail((760, 760))
            self.scale = image_width / disp.width
            self.tk_t = ImageTk.PhotoImage(disp)
            self.canvas.config(width=disp.width, height=disp.height)
            self.canvas.create_image(0, 0, anchor="nw", image=self.tk_t, tags="bg_template")
            self.canvas.create_rectangle(2, 2, disp.width-2, disp.height-2, outline="#00A06A", width=2, dash=(5, 5), tags="template_border")
            for box in self.boxes:
                scaled = [x / self.scale for x in box]
                self.box_items.append(self.canvas.create_rectangle(*scaled, outline=self.colors["wine"], width=2, tags="photobox"))
            self.relabel_boxes()
        else:
            self.boxes = []
            self.box_labels = []
        self._refresh_box_pickers()

    def switch_template_orientation(self, orientation):
        if orientation == self.template_orientation:
            return
        self.template_profiles[self.template_orientation] = self._template_state()
        self.template_orientation = orientation
        self._restore_template_state(self.template_profiles.get(orientation, {}))

    def load_template(self):
        p = filedialog.askopenfilename(filetypes=[("PNG", "*.png")])
        if not p:
            return
        try:
            with Image.open(p) as image:
                disp = image.copy()
            disp.thumbnail((600, 800))
            template_scale = image.width / disp.width
        except (OSError, UnidentifiedImageError) as error:
            messagebox.showerror("Template Error", "Could not open that overlay PNG:\n" + str(error))
            return
        self.template_path = p
        self.scale = template_scale
        self.tk_t = ImageTk.PhotoImage(disp)
        self.canvas.config(width=disp.width, height=disp.height)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_t, tags="bg_template")
        self.canvas.create_rectangle(
            2, 2, disp.width-2, disp.height-2,
            outline="#00ff00", width=3, dash=(5, 5), tags="template_border"
        )
        self.boxes = []
        self.box_items = []
        self.box_labels = []
        self.selected_item = None
        self.selected_index = None
        self.handle_regions = {}
        self._refresh_box_pickers()

    # --- canvas geometry helpers -------------------------------------------------
    def _canvas_size(self):
        width = int(float(self.canvas.cget("width") or 0))
        height = int(float(self.canvas.cget("height") or 0))
        return max(width, 1), max(height, 1)

    def _clamp_rect(self, x1, y1, x2, y2, min_size=12):
        width, height = self._canvas_size()
        x1, x2 = sorted((float(x1), float(x2)))
        y1, y2 = sorted((float(y1), float(y2)))
        x1 = max(0.0, min(x1, width - min_size))
        x2 = max(x1 + min_size, min(x2, width))
        y1 = max(0.0, min(y1, height - min_size))
        y2 = max(y1 + min_size, min(y2, height))
        return int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))

    # --- selection and handles ---------------------------------------------------
    def _deselect(self):
        self.selected_item = None
        self.selected_index = None
        self.handle_regions = {}
        for item in self.box_items:
            self.canvas.itemconfig(item, outline=self.colors["wine"], width=2)
        self.canvas.delete("resizehandle")
        self.canvas.delete("cropguide")

    def _select_box(self, index):
        if not (0 <= index < len(self.box_items)):
            return
        self.selected_index = index
        self.selected_item = self.box_items[index]
        for position, item in enumerate(self.box_items):
            selected = position == index
            self.canvas.itemconfig(item, outline="#F2B705" if selected else self.colors["wine"],
                                   width=4 if selected else 2)
        self.dup_src_var.set(str(index + 1))
        self.coord_idx_var.set(str(index + 1))
        self.load_box_coords()
        self._update_selection_handles()
        self._refresh_crop_guide()

    def _update_selection_handles(self):
        self.canvas.delete("resizehandle")
        self.handle_regions = {}
        if self.selected_index is None or not (0 <= self.selected_index < len(self.box_items)):
            return
        x1, y1, x2, y2 = self.canvas.coords(self.box_items[self.selected_index])
        center_x, center_y = (x1 + x2) / 2, (y1 + y2) / 2
        half = 4
        positions = {
            "nw": (x1, y1), "n": (center_x, y1), "ne": (x2, y1),
            "e": (x2, center_y), "se": (x2, y2), "s": (center_x, y2),
            "sw": (x1, y2), "w": (x1, center_y),
        }
        for name, (point_x, point_y) in positions.items():
            self.canvas.create_rectangle(point_x - half, point_y - half, point_x + half, point_y + half,
                                         fill="#F2B705", outline="#7A1F3D", width=1, tags=("resizehandle",))
            self.handle_regions[name] = (point_x - half, point_y - half, point_x + half, point_y + half)

    def _handle_at(self, x, y):
        for name, (x1, y1, x2, y2) in self.handle_regions.items():
            if x1 - 3 <= x <= x2 + 3 and y1 - 3 <= y <= y2 + 3:
                return name
        return None

    def _refresh_box_pickers(self):
        count = len(self.box_items)
        values = [str(i) for i in range(1, max(count, 10) + 1)] if count else []
        for combo, variable in ((getattr(self, "dup_src_combo", None), getattr(self, "dup_src_var", None)),
                                (getattr(self, "coord_idx_combo", None), getattr(self, "coord_idx_var", None))):
            if combo is None or variable is None:
                continue
            combo.config(values=values)
            if not count:
                variable.set("")
                continue
            try:
                current = int(variable.get())
            except (TypeError, ValueError):
                current = 1
            variable.set(str(min(max(current, 1), count)))

    # --- 3:2 crop guide ----------------------------------------------------------
    def _refresh_crop_guide(self):
        if self.selected_index is None or not (0 <= self.selected_index < len(self.box_items)):
            self.canvas.delete("cropguide")
            return
        self._draw_crop_guide(self.canvas.coords(self.box_items[self.selected_index]))

    def _draw_crop_guide(self, box_coords):
        self.canvas.delete("cropguide")
        if not getattr(self, "show_crop_guide_var", None) or not self.show_crop_guide_var.get():
            return
        x1, y1, x2, y2 = (float(value) for value in box_coords)
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        width, height = x2 - x1, y2 - y1
        if width < 10 or height < 10:
            return
        ratio = 3 / 2
        if width / height > ratio:
            photo_width, photo_height = width, width / ratio
        else:
            photo_width, photo_height = height * ratio, height
        if abs(photo_width - width) < 1.5 and abs(photo_height - height) < 1.5:
            return
        center_x, center_y = (x1 + x2) / 2, (y1 + y2) / 2
        photo_x1, photo_y1 = center_x - photo_width / 2, center_y - photo_height / 2
        photo_x2, photo_y2 = center_x + photo_width / 2, center_y + photo_height / 2
        self.canvas.create_rectangle(photo_x1, photo_y1, photo_x2, photo_y2,
                                     outline="#2F6FED", dash=(4, 3), width=1, tags=("cropguide",))
        shade = "#4C4658"
        if photo_width > width:
            self.canvas.create_rectangle(photo_x1, photo_y1, x1, photo_y2, fill=shade, stipple="gray25", outline="", tags=("cropguide",))
            self.canvas.create_rectangle(x2, photo_y1, photo_x2, photo_y2, fill=shade, stipple="gray25", outline="", tags=("cropguide",))
        else:
            self.canvas.create_rectangle(photo_x1, photo_y1, photo_x2, y1, fill=shade, stipple="gray25", outline="", tags=("cropguide",))
            self.canvas.create_rectangle(photo_x1, y2, photo_x2, photo_y2, fill=shade, stipple="gray25", outline="", tags=("cropguide",))
        self.canvas.create_text(max(photo_x1 + 6, 8), max(photo_y1 - 8, 8), anchor="w",
                                text="3:2 photo · shaded = cut", fill="#2F6FED",
                                font=("Arial", 8, "bold"), tags=("cropguide",))
        if self.canvas.find_withtag("photobox"):
            self.canvas.tag_lower("cropguide", "photobox")

    # --- mouse interaction -------------------------------------------------------
    def on_press(self, e):
        self.canvas.focus_set()
        handle = self._handle_at(e.x, e.y)
        if handle and self.selected_item is not None:
            self.action = "resize"
            self.resize_handle = handle
            self.resize_start = (e.x, e.y)
            self.resize_box = list(self.canvas.coords(self.selected_item))
            return
        for item in reversed(list(self.canvas.find_withtag("photobox"))):
            x1, y1, x2, y2 = self.canvas.coords(item)
            if x1 <= e.x <= x2 and y1 <= e.y <= y2:
                self._select_box(self.box_items.index(item))
                self.action = "move"
                self.start_x, self.start_y = e.x, e.y
                return
        self._deselect()
        self.action = "draw"
        self.sx, self.sy = e.x, e.y
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#F2B705", width=2, dash=(3, 2), tags="photobox")

    def on_drag(self, e):
        if self.action == "move" and self.selected_item:
            dx = e.x - self.start_x
            dy = e.y - self.start_y
            x1, y1, x2, y2 = self.canvas.coords(self.selected_item)
            width, height = self._canvas_size()
            dx = min(max(dx, -x1), width - x2)
            dy = min(max(dy, -y1), height - y2)
            self.canvas.coords(self.selected_item, x1 + dx, y1 + dy, x2 + dx, y2 + dy)
            self.start_x, self.start_y = e.x, e.y
            self._update_selection_handles()
            self._refresh_crop_guide()
        elif self.action == "resize" and self.selected_item:
            self._resize_selected(e)
        elif self.action == "draw":
            self._update_draw_rect(e)

    def _update_draw_rect(self, e):
        dx = e.x - self.sx
        dy = e.y - self.sy
        ratio_factors = {"Landscape (3:2)": 2 / 3, "Portrait (2:3)": 3 / 2,
                         "Landscape (4:3)": 3 / 4, "Portrait (3:4)": 4 / 3}
        factor = ratio_factors.get(self.box_ratio.get())
        if factor is None:
            coords = (self.sx, self.sy, e.x, e.y)
        else:
            height_direction = 1 if dy >= 0 else -1
            coords = (self.sx, self.sy, self.sx + dx, self.sy + abs(dx) * factor * height_direction)
        self.canvas.coords(self.rect, *coords)
        self._draw_crop_guide(self.canvas.coords(self.rect))

    def _resize_selected(self, e):
        start_x1, start_y1, start_x2, start_y2 = self.resize_box
        dx = e.x - self.resize_start[0]
        dy = e.y - self.resize_start[1]
        handle = self.resize_handle or ""
        minimum = 16
        x1, y1, x2, y2 = start_x1, start_y1, start_x2, start_y2
        if "w" in handle:
            x1 = min(start_x1 + dx, start_x2 - minimum)
        if "e" in handle:
            x2 = max(start_x2 + dx, start_x1 + minimum)
        if "n" in handle:
            y1 = min(start_y1 + dy, start_y2 - minimum)
        if "s" in handle:
            y2 = max(start_y2 + dy, start_y1 + minimum)
        if self.lock_ratio_var.get() and len(handle) == 2:
            ratio = max(0.05, (start_x2 - start_x1) / max(1.0, start_y2 - start_y1))
            width, height = x2 - x1, y2 - y1
            if width / max(1.0, height) > ratio:
                height = width / ratio
            else:
                width = height * ratio
            if "w" in handle:
                x1 = x2 - width
            else:
                x2 = x1 + width
            if "n" in handle:
                y1 = y2 - height
            else:
                y2 = y1 + height
        x1, y1, x2, y2 = self._clamp_rect(x1, y1, x2, y2, minimum)
        self.canvas.coords(self.selected_item, x1, y1, x2, y2)
        self._update_selection_handles()
        self._refresh_crop_guide()

    def on_release(self, e):
        if self.action in ("move", "resize") and self.selected_item:
            self.sync_boxes()
            self.load_box_coords()
            self._update_selection_handles()
            self._refresh_crop_guide()
        elif self.action == "draw":
            coords = self.canvas.coords(self.rect)
            x1, y1, x2, y2 = min(coords[0], coords[2]), min(coords[1], coords[3]), max(coords[0], coords[2]), max(coords[1], coords[3])
            if abs(x2 - x1) < 10 or abs(y2 - y1) < 10:
                self.canvas.delete(self.rect)
                self.canvas.delete("cropguide")
            else:
                x1, y1, x2, y2 = self._clamp_rect(x1, y1, x2, y2)
                self.canvas.coords(self.rect, x1, y1, x2, y2)
                self.box_items.append(self.rect)
                self.box_labels.append(len(self.box_items))
                self.sync_boxes()
                self._select_box(len(self.box_items) - 1)
        self.action = None

    def sync_boxes(self):
        scale = getattr(self, "scale", 1) or 1
        self.boxes = [[float(value) * scale for value in self.canvas.coords(item)] for item in self.box_items]
        if len(self.box_labels) < len(self.box_items):
            self.box_labels = list(range(1, len(self.box_items) + 1))
        self.box_labels = self.box_labels[:len(self.box_items)]
        self.relabel_boxes()
        self._refresh_box_pickers()

    def relabel_boxes(self):
        self.canvas.delete("boxlabel")
        for i, item in enumerate(self.box_items):
            c = self.canvas.coords(item)
            label_x = c[0] + 15
            label_y = c[1] + 15
            label_text = str(self.box_labels[i]) if i < len(self.box_labels) else str(i + 1)
            self.canvas.create_text(label_x, label_y, text=label_text, fill="yellow", font=("Arial", 12, "bold"), tags=("boxlabel"))

    def clear_boxes(self):
        self.canvas.delete("photobox")
        self.canvas.delete("boxlabel")
        self.canvas.delete("resizehandle")
        self.canvas.delete("cropguide")
        self.boxes = []
        self.box_items = []
        self.box_labels = []
        self.selected_item = None
        self.selected_index = None
        self.handle_regions = {}
        self._refresh_box_pickers()

    def delete_selected_box(self):
        if not self.box_items:
            messagebox.showinfo("Delete Box", "There are no photo boxes to delete.")
            return
        index = self.selected_index
        if index is None or not (0 <= index < len(self.box_items)):
            messagebox.showinfo("Delete Box", "Click a photo box first, then delete it.")
            return
        self.canvas.delete(self.box_items.pop(index))
        if index < len(self.box_labels):
            self.box_labels.pop(index)
        self.selected_item = None
        self.selected_index = None
        self.handle_regions = {}
        self.canvas.delete("resizehandle")
        self.canvas.delete("cropguide")
        self.sync_boxes()

    def duplicate_box(self):
        if not self.box_items:
            messagebox.showinfo("Duplicate Box", "Draw a photo box first, then duplicate it.")
            return
        index = self.selected_index
        if index is None:
            try:
                index = int(self.dup_src_var.get()) - 1
            except (TypeError, ValueError):
                index = -1
        if not (0 <= index < len(self.box_items)):
            messagebox.showwarning("Duplicate Box", "Select a photo box or pick a valid source number.")
            return
        x1, y1, x2, y2 = (int(round(value)) for value in self.canvas.coords(self.box_items[index]))
        width, height = x2 - x1, y2 - y1
        canvas_width, canvas_height = self._canvas_size()
        offset = 24
        new_x = x1 + offset if x1 + offset + width <= canvas_width else max(0, x1 - offset)
        new_y = y1 + offset if y1 + offset + height <= canvas_height else max(0, y1 - offset)
        new_rect = self.canvas.create_rectangle(new_x, new_y, new_x + width, new_y + height,
                                                outline=self.colors["wine"], width=2, tags="photobox")
        self.box_items.append(new_rect)
        self.box_labels.append(self.box_labels[index] if index < len(self.box_labels) else index + 1)
        self.sync_boxes()
        self._select_box(len(self.box_items) - 1)

    def load_coord_box(self):
        try:
            index = int(self.coord_idx_var.get()) - 1
        except (TypeError, ValueError):
            return
        self._select_box(index)

    def load_box_coords(self):
        try:
            index = self.selected_index
            if index is None:
                index = int(self.coord_idx_var.get()) - 1
            if not (0 <= index < len(self.box_items)):
                return
            x1, y1, x2, y2 = (int(round(value)) for value in self.canvas.coords(self.box_items[index]))
            entries = (("x1", self.coord_x1, x1), ("y1", self.coord_y1, y1),
                       ("x2", self.coord_x2, x2), ("y2", self.coord_y2, y2),
                       ("w", self.coord_w, x2 - x1), ("h", self.coord_h, y2 - y1))
            baseline = {}
            for name, entry, value in entries:
                entry.delete(0, tk.END)
                entry.insert(0, str(value))
                baseline[name] = str(value)
            self._coord_baseline = baseline
        except Exception as error:
            messagebox.showerror("Load Error", str(error))

    def apply_coord_changes(self):
        index = self.selected_index
        if index is None:
            try:
                index = int(self.coord_idx_var.get()) - 1
            except (TypeError, ValueError):
                index = -1
        if not (0 <= index < len(self.box_items)):
            messagebox.showwarning("Apply", "Select a photo box first.")
            return
        try:
            values = {name: int(float(entry.get())) for name, entry in (
                ("x1", self.coord_x1), ("y1", self.coord_y1), ("x2", self.coord_x2),
                ("y2", self.coord_y2), ("w", self.coord_w), ("h", self.coord_h))}
        except ValueError:
            messagebox.showerror("Apply", "Enter valid numbers for the box position and size.")
            return
        x1, y1, x2, y2 = values["x1"], values["y1"], values["x2"], values["y2"]
        baseline = self._coord_baseline or {}
        width_changed = str(values["w"]) != baseline.get("w") and values["w"] > 0
        height_changed = str(values["h"]) != baseline.get("h") and values["h"] > 0
        if width_changed or height_changed:
            x2, y2 = x1 + values["w"], y1 + values["h"]
        x1, y1, x2, y2 = self._clamp_rect(x1, y1, x2, y2)
        self.canvas.coords(self.box_items[index], x1, y1, x2, y2)
        self.sync_boxes()
        self._select_box(index)
    # ========================================================
    # TAB 2: ENGINE
    # ========================================================
    def setup_gallery(self):
        self.gallery_view.columnconfigure(0, weight=1)
        self.gallery_view.rowconfigure(2, weight=1)
        toolbar = tk.Frame(self.gallery_view, bg=self.colors["surface"])
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        tk.Label(toolbar, text="Printed Photos", bg=self.colors["surface"], fg=self.colors["text"],
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(toolbar, text="Preview and queue reprints without interrupting the system.",
                 bg=self.colors["surface"], fg=self.colors["muted"]).pack(side="left", padx=14)
        ttk.Button(toolbar, text="Refresh", command=self.refresh_gallery, style="Secondary.TButton").pack(side="right")
        self.gallery_size_var = tk.IntVar(value=420)
        ttk.Label(toolbar, text="Thumbnail size").pack(side="right", padx=(18, 6))
        ttk.Scale(toolbar, from_=160, to=520, variable=self.gallery_size_var,
                  command=self.schedule_gallery_resize).pack(side="right", fill="x", expand=False, ipadx=55)

        self.gallery_status = ttk.Label(self.gallery_view, text="Loading printed photos...", foreground=self.colors["muted"])
        self.gallery_status.grid(row=1, column=0, sticky="w", pady=(0, 8))
        gallery_surface = tk.Frame(self.gallery_view, bg="#f4f0f2", highlightbackground=self.colors["border"], highlightthickness=1)
        gallery_surface.grid(row=2, column=0, sticky="nsew")
        gallery_surface.columnconfigure(0, weight=1)
        gallery_surface.rowconfigure(0, weight=1)
        self.gallery_canvas = tk.Canvas(gallery_surface, bg="#f4f0f2", highlightthickness=0)
        self.gallery_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(gallery_surface, orient="vertical", command=self.gallery_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.gallery_canvas.configure(yscrollcommand=scrollbar.set)
        self.gallery_inner = tk.Frame(self.gallery_canvas, bg="#f4f0f2")
        self.gallery_window = self.gallery_canvas.create_window((0, 0), window=self.gallery_inner, anchor="nw")
        self.gallery_inner.bind("<Configure>", lambda event: self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all")))
        self.gallery_canvas.bind("<Configure>", self.on_gallery_resize)
        self.gallery_canvas.bind("<Enter>", lambda event: self.gallery_canvas.bind_all("<MouseWheel>", self.gallery_mousewheel))
        self.gallery_canvas.bind("<Leave>", lambda event: self.gallery_canvas.unbind_all("<MouseWheel>"))
        self.gallery_images = []
        self.gallery_card_map = {}
        self.gallery_sections = []
        self.gallery_generation = 0
        self.gallery_resize_job = None
        self.gallery_stale = False
        self.root.after(150, self.refresh_gallery)

    def schedule_gallery_resize(self, _value=None):
        if self.gallery_resize_job:
            self.root.after_cancel(self.gallery_resize_job)
        self.gallery_resize_job = self.root.after(250, self.refresh_gallery)

    def on_gallery_resize(self, event):
        self.gallery_canvas.itemconfigure(self.gallery_window, width=event.width)
        self.reflow_gallery()

    def gallery_mousewheel(self, event):
        self.gallery_canvas.yview_scroll(-int(event.delta / 120), "units")

    def get_thumbnail_proxy(self, path, size):
        os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)
        source_stat = os.stat(path)
        cache_key = hashlib.sha1(f"{path}|{source_stat.st_size}|{source_stat.st_mtime_ns}|{size}".encode("utf-8")).hexdigest()
        proxy_path = os.path.join(THUMBNAIL_CACHE_DIR, f"{cache_key}.jpg")
        if os.path.exists(proxy_path):
            return proxy_path
        temporary = f"{proxy_path}.part"
        with Image.open(path) as image:
            preview = ImageOps.exif_transpose(image).convert("RGB")
            preview.thumbnail((size, size), RESAMPLE_METHOD)
            preview.save(temporary, "JPEG", quality=82, optimize=True)
        os.replace(temporary, proxy_path)
        return proxy_path

    def reflow_gallery(self):
        if not hasattr(self, "gallery_sections"):
            return
        size = max(160, int(float(self.gallery_size_var.get())))
        columns = max(1, self.gallery_canvas.winfo_width() // (size + 42))
        for section, cards in self.gallery_sections:
            for column in range(columns):
                section.grid_columnconfigure(column, weight=1)
            for index, card in enumerate(cards):
                card.grid(row=index // columns, column=index % columns, padx=7, pady=7, sticky="n")

    def refresh_gallery(self):
        if not hasattr(self, "gallery_inner"):
            return
        self.gallery_resize_job = None
        self.gallery_generation += 1
        generation = self.gallery_generation
        for child in self.gallery_inner.winfo_children():
            child.destroy()
        self.gallery_images = []
        self.gallery_card_map = {}
        self.gallery_sections = []
        total = 0
        size = max(160, int(float(self.gallery_size_var.get())))
        proxy_jobs = []
        for orientation in ("Landscape", "Portrait"):
            folder = os.path.join(self.output_folder, orientation)
            files = []
            if os.path.isdir(folder):
                files = sorted((entry.path for entry in os.scandir(folder)
                                if entry.is_file() and entry.name.lower().endswith((".jpg", ".jpeg"))),
                               key=os.path.getmtime, reverse=True)
            section = tk.LabelFrame(self.gallery_inner, text=f"{orientation}  ({len(files)})", bg="#f4f0f2",
                                    fg=self.colors["wine"], font=("Segoe UI", 10, "bold"), padx=10, pady=10)
            section.pack(fill="x", padx=14, pady=(12, 0))
            if not files:
                tk.Label(section, text="No printed photos yet.", bg="#f4f0f2", fg=self.colors["muted"]).pack(anchor="w", pady=8)
                continue
            cards = []
            for index, path in enumerate(files):
                card = tk.Frame(section, width=size + 28, height=size + 100, bg="white",
                                 highlightbackground="#ded5d9", highlightthickness=1)
                card.pack_propagate(False)
                preview_area = tk.Frame(card, width=size + 14, height=size + 14, bg="#eee8eb")
                preview_area.pack_propagate(False)
                preview_area.pack(padx=7, pady=(7, 3))
                card.preview = tk.Label(preview_area, text="Loading preview...", bg="#eee8eb", fg=self.colors["muted"])
                card.preview.pack(fill="both", expand=True)
                tk.Label(card, text=os.path.basename(path), bg="white", fg=self.colors["text"],
                         font=("Segoe UI", 8), width=22).pack(padx=5)
                ttk.Button(card, text="Reprint", style="Secondary.TButton",
                           command=lambda photo_path=path: self.queue_gallery_reprint(photo_path)).pack(pady=7)
                cards.append(card)
                self.gallery_card_map[id(card)] = card
                proxy_jobs.append((path, id(card), size, generation))
                total += 1
            self.gallery_sections.append((section, cards))
        self.reflow_gallery()
        self.gallery_status.config(text=f"{total} printed photo(s) available | Loading cached previews...")

        def load_proxies():
            for path, card_id, proxy_size, job_generation in proxy_jobs:
                try:
                    proxy = self.get_thumbnail_proxy(path, proxy_size)
                    self.gui_queue.put({"action": "gallery_thumbnail", "proxy": proxy,
                                       "card_id": card_id, "generation": job_generation})
                except (OSError, UnidentifiedImageError):
                    pass
        threading.Thread(target=load_proxies, daemon=True).start()
        self.gallery_status.config(text=f"{total} printed photo(s) available")

    def queue_gallery_reprint(self, file_path):
        copies = simpledialog.askinteger("Reprint Photo", "How many copies should be added to the print queue?",
                                         parent=self.root, minvalue=1, maxvalue=99, initialvalue=1)
        if copies:
            self.silent_print(file_path, copies=copies)
            self.queue_log(f"🖼️ Added {copies} reprint(s) to the queue: {os.path.basename(file_path)}")

    def setup_engine(self):
        left = tk.Frame(self.engine_tab, width=390, bg=self.colors["surface"])
        left.pack(side="left", fill="y", padx=(10, 18), pady=10)
        left.pack_propagate(False)

        tk.Label(left, text="Print Monitor", font=("Segoe UI", 16, "bold"), bg=self.colors["surface"],
                 fg=self.colors["text"]).pack(anchor="w", pady=(4, 2))
        tk.Label(left, text="Locked photos are processed automatically.", font=("Segoe UI", 9),
                 bg=self.colors["surface"], fg=self.colors["muted"]).pack(anchor="w", pady=(0, 14))

        progress_frame = tk.LabelFrame(left, text="Live progress", padx=10, pady=8, bg=self.colors["surface"],
                                       fg=self.colors["wine"], font=("Segoe UI", 9, "bold"), bd=1, relief="solid")
        progress_frame.pack(fill="x", pady=(0, 8))
        self.import_progress_status = tk.StringVar(value="SD card: waiting for transfer")
        ttk.Label(progress_frame, textvariable=self.import_progress_status).pack(anchor="w")
        self.import_progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=1, value=0)
        self.import_progress.pack(fill="x", pady=(4, 8))
        self.print_progress_status = tk.StringVar(value="Print queue: empty")
        ttk.Label(progress_frame, textvariable=self.print_progress_status).pack(anchor="w")
        self.print_queue_progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=1, value=0)
        self.print_queue_progress.pack(fill="x", pady=(4, 0))
        self.group_progress_status = tk.StringVar(value="Print group: waiting for locked photos")
        ttk.Label(progress_frame, textvariable=self.group_progress_status).pack(anchor="w", pady=(7, 0))

        out_frame = tk.LabelFrame(left, text="Print output", padx=10, pady=8, bg=self.colors["surface"],
                                  fg=self.colors["wine"], font=("Segoe UI", 9, "bold"), bd=1, relief="solid")
        out_frame.pack(fill="x", pady=5)
        self.out_entry = tk.Entry(out_frame, width=35, bg="#fbfafb", fg=self.colors["text"],
                                  relief="flat", highlightthickness=1, highlightbackground="#ddd5d8",
                                  highlightcolor=self.colors["wine"])
        self.out_entry.insert(0, self.output_folder)
        self.out_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(out_frame, text="Browse", command=self.select_output, style="Secondary.TButton").pack(side="right")

        tk.Frame(left, height=1, bg=self.colors["border"]).pack(fill="x", pady=14)

        print_ctrl_frame = tk.Frame(left, bg=self.colors["surface"])
        print_ctrl_frame.pack(fill="x", pady=5)

        self.mode_var = tk.StringVar(value="Print + JPEG")
        cb = ttk.Combobox(print_ctrl_frame, textvariable=self.mode_var, values=("Print + JPEG", "JPEG Only"), state="readonly", width=15)
        cb.pack(side="left", padx=(0, 5))

        ttk.Button(print_ctrl_frame, text="Printer Preferences", command=self.open_printer_preferences, style="Secondary.TButton").pack(side="left", fill="x", expand=True)

        self.reprint_btn = ttk.Button(print_ctrl_frame, text="Reprint Last", command=self.reprint_last, state="disabled", style="Secondary.TButton")
        self.reprint_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))

        sharpen_frame = tk.LabelFrame(left, text="Sharpen print output", padx=10, pady=8, bg=self.colors["surface"],
                                      fg=self.colors["wine"], font=("Segoe UI", 9, "bold"), bd=1, relief="solid")
        sharpen_frame.pack(fill="x", pady=5)
        self.sharpen_var = tk.IntVar(value=self.sharpen_value)
        sharpen_slider = ttk.Scale(sharpen_frame, from_=0, to=100, variable=self.sharpen_var, command=self.on_sharpen_change)
        sharpen_slider.pack(fill="x", padx=5, pady=2)
        self.sharpen_lbl = tk.Label(sharpen_frame, text=f"Sharpness: {self.sharpen_value}%", bg=self.colors["surface"], fg=self.colors["info"],
                                    font=("Segoe UI", 9, "bold"))
        self.sharpen_lbl.pack()

        self.count_lbl = tk.Label(left, text="Total Prints\n0", font=("Segoe UI", 18, "bold"), bg=self.colors["surface"], fg=self.colors["info"])
        self.count_lbl.pack(pady=10)

        btn_frame = tk.Frame(left, bg=self.colors["surface"])
        btn_frame.pack(fill="x", pady=10)
        self.start_btn = tk.Button(btn_frame, text="START SYSTEM", bg=self.colors["success"], fg="white", activebackground="#1f6342",
                                   activeforeground="white", relief="flat", font=("Segoe UI", 11, "bold"), command=self.start_engine, height=2, width=15)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.stop_btn = tk.Button(btn_frame, text="STOP", bg="#b42318", fg="white", activebackground="#8e1c13",
                                  activeforeground="white", relief="flat", font=("Segoe UI", 10, "bold"), state="disabled", command=self.stop_engine, height=2, width=8)
        self.stop_btn.pack(side="right")

        self.log_box = scrolledtext.ScrolledText(left, width=45, height=12, font=("Consolas", 8), bg="#fbfafb",
                                                 fg=self.colors["text"], relief="solid", bd=1, highlightthickness=0)
        self.log_box.pack(pady=10, fill="both", expand=True)

        self.prev_frame = ttk.LabelFrame(self.engine_tab, text=" Live Print Preview ", padding=10)
        self.prev_frame.pack(side="right", fill="both", expand=True, padx=(0, 10), pady=10)
        self.prev_lbl = tk.Label(self.prev_frame, text="Ready for the next print group", bg="#f1ecef",
                                 fg=self.colors["muted"], font=("Segoe UI", 11))
        self.prev_lbl.pack(expand=True, fill="both")

    def select_output(self):
        path = filedialog.askdirectory()
        if path: self.output_folder = path; self.out_entry.delete(0, tk.END); self.out_entry.insert(0, path)

    def on_sharpen_change(self, val):
        self.sharpen_value = int(float(val))
        self.sharpen_lbl.config(text=f"Sharpness: {self.sharpen_value}%")

    def open_printer_preferences(self):
        try:
            printer_name = win32print.GetDefaultPrinter()
            PRINTER_ALL_ACCESS = 0x000F000C
            hprinter = win32print.OpenPrinter(printer_name, {"DesiredAccess": PRINTER_ALL_ACCESS})
            win32print.DocumentProperties(0, hprinter, printer_name, None, None, 14)
            win32print.ClosePrinter(hprinter)
            self.queue_log(f"🖨️ Opened settings for: {printer_name}")
        except Exception as e:
            messagebox.showerror("Printer Settings Error", f"Could not open properties for default printer:\n{e}")

    def reprint_last(self):
        if self.last_printed_file and os.path.exists(self.last_printed_file):
            self.queue_log(f"🔄 FAILSAFE TRIPPED: Reprinting {os.path.basename(self.last_printed_file)}")
            self.silent_print(self.last_printed_file)
        else:
            messagebox.showwarning("Reprint Error", "No previous print found in memory or file was deleted.")

    def queue_log(self, msg):
        self.gui_queue.put({"action": "log", "msg": msg})

    def start_engine(self):
        if not self.require_license():
            return
        self.template_profiles[self.template_orientation] = self._template_state()
        enabled_orientations = [orientation for orientation, enabled in (
            ("Landscape", self.landscape_enabled_var.get()),
            ("Portrait", self.portrait_enabled_var.get())) if enabled]
        if not enabled_orientations:
            return messagebox.showerror("Error", "Enable Landscape or Portrait in Settings.")
        for orientation in enabled_orientations:
            profile = self.template_profiles.get(orientation, {})
            if not profile.get("path") or not profile.get("boxes"):
                return messagebox.showerror("Error", f"Configure the {orientation} template before starting.")
        if not os.path.exists(self.source_folder): return messagebox.showerror("Error", "Source folder missing!")
        source_path = self.src_path_var.get().strip()
        import_paths = {
            "Locked destination": self.dest_prot_var.get(),
            "RAW destination": self.dest_raw_var.get(),
        }
        invalid_import_paths = [f"{name}: {path}" for name, path in import_paths.items() if not os.path.isdir(path)]
        if not source_path or source_path.startswith("Click to select"):
            invalid_import_paths.insert(0, "SD source: select the camera card folder in Settings")
        if invalid_import_paths:
            return messagebox.showerror("Import Error", "Select valid photo-import folders before starting:\n\n" + "\n".join(invalid_import_paths))

        self.sorter_start_time = time.time()
        self.is_sorting = True
        self.btn_start_sorter.config(text="STOP COPYING", bg="#dc3545")
        self.sorter_thread = threading.Thread(target=self.sorter_monitor_loop, daemon=True)
        self.sorter_thread.start()

        self.is_running = True
        self.engine_start_time = time.time()
        self.start_btn.config(state="disabled", text="SYSTEM RUNNING"); self.stop_btn.config(state="normal")
        self.out_entry.config(state="disabled")
        self.header_status.config(text="● RUNNING", fg=self.colors["success"])

        with self.input_queue.mutex: self.input_queue.queue.clear()
        self.processed_files.clear()

        self.handler = SalikhaHandler(self.queue_file, self.queue_log, self.engine_start_time)
        self.obs = Observer()
        self.obs.schedule(self.handler, self.source_folder, recursive=True)
        self.obs.start()

        self.poller_thread = threading.Thread(target=self.safety_net_loop, daemon=True)
        self.poller_thread.start()

        self.processor_thread = threading.Thread(target=self.logic_processor_loop, daemon=True)
        self.processor_thread.start()

        self.queue_log(f"🚀 ENGINE STARTED: Waiting for NEW photos only.")

    def stop_engine(self):
        if hasattr(self, 'obs'): self.obs.stop()
        self.is_running = False
        self.is_sorting = False
        self.start_btn.config(state="normal", text="START SYSTEM"); self.stop_btn.config(state="disabled")
        self.out_entry.config(state="normal")
        self.btn_start_sorter.config(text="START MONITORING & COPYING", bg="#007bff", state="normal")
        self.header_status.config(text="● READY", fg=self.colors["success"])
        self.queue_log("🛑 Engine Stopped.")

    def queue_file(self, file_path):
        norm_path = os.path.normpath(file_path)
        with self.queue_lock:
            if norm_path not in self.processed_files:
                try:
                    mtime = os.path.getmtime(norm_path)
                    if mtime >= self.engine_start_time:
                        self.processed_files.add(norm_path)
                        self.input_queue.put(norm_path)
                except OSError:
                    pass

    def safety_net_loop(self):
        while self.is_running:
            try:
                files = []
                for root, dirs, names in os.walk(self.source_folder):
                    files.extend(os.path.join(root, name) for name in names)
                files = [f for f in files if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
                files.sort(key=os.path.getctime)
                for f in files:
                    norm_f = os.path.normpath(f)
                    if norm_f not in self.processed_files:
                        if os.path.getmtime(norm_f) >= self.engine_start_time:
                            self.queue_log(f"🔎 Safety Net found missed file: {os.path.basename(f)}")
                            self.queue_file(norm_f)
                time.sleep(3.0)
            except: time.sleep(3.0)

    def logic_processor_loop(self):
        sessions = {"Landscape": [], "Portrait": []}
        self.queue_log("ℹ️ Landscape and Portrait print queues are ready.")

        while self.is_running:
            try:
                file_path = self.input_queue.get(timeout=1.0)
                retries = 0
                while not self.is_file_ready(file_path):
                    if not self.is_running: break
                    retries += 1
                    if retries % 15 == 0: self.queue_log(f"⏳ Waiting for lock release: {os.path.basename(file_path)}")
                    time.sleep(0.2)
                if not self.is_running: break

                orientation = self.get_orientation_folder(file_path)
                if orientation not in sessions or not self._orientation_enabled(orientation):
                    self.queue_log(f"⏭️ Skipped {os.path.basename(file_path)}: {orientation} queue disabled.")
                    self.input_queue.task_done()
                    continue
                profile = self.template_profiles.get(orientation, {})
                labels = profile.get("labels", [])
                required_count = len(set(labels))
                if not profile.get("path") or not required_count:
                    self.queue_log(f"⚠️ {orientation} template is not configured.")
                    self.input_queue.task_done()
                    continue
                sessions[orientation].append(file_path)
                current_step = len(sessions[orientation])
                self.gui_queue.put({"action": "print_group_progress",
                                   "message": f"{orientation} print group: {current_step}/{required_count} photos"})
                self.queue_log(f"📸 {orientation}: {current_step}/{required_count} - {os.path.basename(file_path)}")

                if current_step == required_count:
                    self.update_preview_and_save(sessions[orientation], save_to_disk=True, orientation=orientation)
                    self.queue_log(f"✅ {orientation} print group complete.")
                    self.gui_queue.put({"action": "print_group_progress",
                                       "message": f"{orientation} print group complete"})
                    sessions[orientation] = []
                    time.sleep(1.5)
                    self.gui_queue.put({"action": "clear_prev"})
                else:
                    self.update_preview_and_save(sessions[orientation], save_to_disk=False, orientation=orientation)

                self.input_queue.task_done()
            except queue.Empty: continue
            except Exception as e: self.queue_log(f"❌ Processing Error: {e}")

    def _orientation_enabled(self, orientation):
        if orientation == "Landscape":
            return self.landscape_enabled_var.get()
        if orientation == "Portrait":
            return self.portrait_enabled_var.get()
        return False

    def is_file_ready(self, filepath):
        if not os.path.exists(filepath): return False
        try:
            with open(filepath, 'ab'): pass
            return True
        except: return False

    def update_preview_and_save(self, current_files, save_to_disk, orientation="Landscape"):
        try:
            profile = self.template_profiles.get(orientation, {})
            template_path = profile.get("path") or self.template_path
            boxes = profile.get("boxes", self.boxes)
            box_labels = profile.get("labels", self.box_labels)
            with Image.open(template_path) as t:
                overlay = t.convert("RGBA")
                canvas = Image.new("RGBA", overlay.size, (255, 255, 255, 255))

                unique_labels = []
                for lbl in box_labels:
                    if lbl not in unique_labels:
                        unique_labels.append(lbl)
                label_to_idx = {lbl: idx for idx, lbl in enumerate(unique_labels)}

                for box_idx, (box, label) in enumerate(zip(boxes, box_labels)):
                    photo_idx = label_to_idx[label]
                    if photo_idx < len(current_files):
                        with Image.open(current_files[photo_idx]) as img:
                            img = ImageOps.exif_transpose(img)
                            w, h = int(box[2]-box[0]), int(box[3]-box[1])
                            img = ImageOps.fit(img, (w, h), RESAMPLE_METHOD)
                            canvas.paste(img, (int(box[0]), int(box[1])))
                canvas.paste(overlay, (0, 0), overlay)

                prev = canvas.copy()
                prev.thumbnail((600, 800), RESAMPLE_METHOD)
                self.gui_queue.put({"action": "preview", "image": prev})

                if save_to_disk:
                    if not self.require_license(notify=False):
                        return
                    output_folder = os.path.join(self.output_folder, orientation)
                    if not os.path.exists(output_folder):
                        try:
                            os.makedirs(output_folder, exist_ok=True)
                        except Exception as e:
                            self.queue_log(f"❌ Cannot create output folder: {output_folder}")
                            return
                    filename = f"Print_{orientation}_{int(time.time() * 1000)}.jpg"
                    out_path = os.path.join(output_folder, filename)

                    save_img = canvas.convert("RGB")

                    if self.sharpen_value > 0:
                        radius = self.sharpen_value / 10.0
                        percent = self.sharpen_value * 2
                        save_img = save_img.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent))

                    try:
                        save_img.save(out_path, "JPEG", quality=98)
                        self.queue_log(f"💾 SAVED: {filename}")
                    except PermissionError:
                        self.queue_log(f"❌ Permission denied saving {filename} - file may be open elsewhere")
                        return

                    self.last_printed_file = out_path
                    self.gui_queue.put({"action": "enable_reprint"})
                    self.gui_queue.put({"action": "gallery_refresh"})

                    if "Print" in self.mode_var.get() and self.auto_print_var.get():
                        self.silent_print(out_path, notify=False)
                        self.print_count += 1
                        self.gui_queue.put({"action": "update_count", "count": self.print_count})
        except Exception as e:
            self.queue_log(f"Render Error: {e} | Orientation: {orientation}")

    def silent_print(self, file_path, copies=1, notify=True):
        self.queue_print(file_path, copies=copies, notify=notify)

    def queue_print(self, file_path, copies=1, notify=True):
        if not self.require_license(notify=notify):
            return
        if not os.path.exists(file_path):
            self.queue_log(f"❌ Cannot queue missing print: {os.path.basename(file_path)}")
            return
        with self.print_queue_lock:
            self.print_queue_total += copies
            total = self.print_queue_total
            self.print_queue_pending += copies
        for _ in range(copies):
            self.print_queue.put(file_path)
        self.gui_queue.put({"action": "print_queue_progress", "current": total - self.print_queue_pending,
                           "total": total, "message": f"Print queue: {self.print_queue_pending} waiting"})
        if not getattr(self, "print_queue_thread", None) or not self.print_queue_thread.is_alive():
            self.print_queue_thread = threading.Thread(target=self._print_queue_loop, daemon=True)
            self.print_queue_thread.start()

    def _print_queue_loop(self):
        while True:
            file_path = self.print_queue.get()
            with self.print_queue_lock:
                total = self.print_queue_total
                completed = total - self.print_queue_pending
            self.gui_queue.put({"action": "print_queue_progress", "current": completed,
                               "total": total, "message": f"Printing {completed + 1}/{total}: {os.path.basename(file_path)}"})
            success = self._print_worker(file_path)
            with self.print_queue_lock:
                self.print_queue_pending = max(0, self.print_queue_pending - 1)
                completed = self.print_queue_total - self.print_queue_pending
                queue_finished = self.print_queue_pending == 0
                if queue_finished:
                    self.print_queue_total = 0
            if success:
                message = "Print queue complete" if queue_finished else f"Printed {completed}/{total}"
            else:
                message = f"Print failed: {os.path.basename(file_path)}. See Logs for details."
            self.gui_queue.put({"action": "print_queue_progress", "current": completed,
                                "total": max(1, total), "message": message})
            self.print_queue.task_done()

    def _print_worker(self, file_path):
        if not self.require_license(notify=False):
            return False
        try:
            with Image.open(file_path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
            orientation = (
                win32con.DMORIENT_LANDSCAPE if image.width >= image.height
                else win32con.DMORIENT_PORTRAIT
            )
            printer_name = win32print.GetDefaultPrinter()
            printer = win32print.OpenPrinter(printer_name)
            try:
                settings = win32print.GetPrinter(printer, 9)["pDevMode"]
                if settings is None:
                    settings = win32print.GetPrinter(printer, 2)["pDevMode"]
                if settings is None:
                    raise RuntimeError("Could not load printer preferences.")
                settings.Fields |= win32con.DM_ORIENTATION
                settings.Orientation = orientation
                result = win32print.DocumentProperties(
                    0, printer, printer_name, settings, settings,
                    win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
                )
                if result != win32con.IDOK or settings.Orientation != orientation:
                    raise RuntimeError("The printer driver rejected the layout orientation.")
            finally:
                win32print.ClosePrinter(printer)

            handle = win32gui.CreateDC("WINSPOOL", printer_name, settings)
            dc = None
            try:
                dc = win32ui.CreateDCFromHandle(handle)
                page_width = dc.GetDeviceCaps(win32con.HORZRES)
                page_height = dc.GetDeviceCaps(win32con.VERTRES)
                dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
                dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
                # Fit and center in physical units to preserve the layout's aspect ratio.
                scale = min(page_width / dpi_x / image.width, page_height / dpi_y / image.height)
                width = max(1, round(image.width * scale * dpi_x))
                height = max(1, round(image.height * scale * dpi_y))
                left = (page_width - width) // 2
                top = (page_height - height) // 2
                dib = ImageWin.Dib(image)

                if not self.require_license(notify=False):
                    return False
                dc.StartDoc(file_path)
                try:
                    dc.StartPage()
                    dib.draw(dc.GetHandleOutput(), (left, top, left + width, top + height))
                    dc.EndPage()
                    dc.EndDoc()
                except Exception:
                    dc.AbortDoc()
                    raise
            finally:
                if dc is None:
                    win32gui.DeleteDC(handle)
                else:
                    dc.DeleteDC()
            return True
        except Exception as e:
            self.queue_log(f"❌ PRINTER ERROR: {os.path.basename(file_path)} ({e})")
            return False

    # ========================================================
    # TAB 3: SD CARD SORTER
    # ========================================================
    def setup_sorter(self):
        container = ttk.Frame(self.sorter_tab)
        container.pack(expand=True, fill="both", padx=50, pady=20)

        # Step 1: Source
        ttk.Label(container, text="Step 1: Select SD Card (Source)", font=("Arial", 11, "bold")).pack(pady=(10, 2))
        self.src_path_var = tk.StringVar(value=self.sd_source or "Click to select SD Card...")
        tk.Button(container, textvariable=self.src_path_var, command=self.select_sorter_source, width=80, relief="groove", bg="#fff").pack(pady=2)

        # Step 2: Locked Destination
        ttk.Label(container, text="Step 2: Folder for LOCKED Files (L/P separated)", font=("Arial", 11, "bold")).pack(pady=(15, 2))
        self.dest_prot_var = tk.StringVar(value=self.sd_dest_prot or "Click to select destination...")
        tk.Button(container, textvariable=self.dest_prot_var, command=self.select_sorter_dest_prot, width=80, relief="groove", bg="#fff").pack(pady=2)

        # Step 3: Raw Destination (Merged)
        ttk.Label(container, text="Step 3: Folder for RAW Archive (all photos merged)", font=("Arial", 11, "bold")).pack(pady=(15, 2))
        self.dest_raw_var = tk.StringVar(value=self.sd_dest_raw or "Click to select destination...")
        tk.Button(container, textvariable=self.dest_raw_var, command=self.select_sorter_dest_raw, width=80, relief="groove", bg="#fff").pack(pady=2)

        self.btn_start_sorter = tk.Button(container, text="START MONITORING & COPYING", bg="#007bff", fg="white", font=("Arial", 12, "bold"), command=self.toggle_sorter, height=2)
        self.btn_start_sorter.pack(pady=25, fill="x", padx=100)

        self.sorter_log_area = scrolledtext.ScrolledText(container, height=12, state='disabled', font=("Consolas", 9), bg="#f4f4f4")
        self.sorter_log_area.pack(padx=10, pady=10, fill="both", expand=True)

    def select_sorter_source(self):
        path = filedialog.askdirectory()
        if path: self.src_path_var.set(path)

    def select_sorter_dest_prot(self):
        path = filedialog.askdirectory()
        if path: self.dest_prot_var.set(path)

    def select_sorter_dest_raw(self):
        path = filedialog.askdirectory()
        if path: self.dest_raw_var.set(path)

    def sorter_queue_log(self, message):
        self.gui_queue.put({"action": "sorter_log", "msg": message})

    def is_locked(self, filepath):
        return not (os.stat(filepath).st_mode & stat.S_IWRITE)

    def get_orientation_folder(self, filepath):
        try:
            with Image.open(filepath) as img:
                width, height = img.size
                exif = img.getexif()
                orientation_tag = exif.get(274)

                if orientation_tag in [5, 6, 7, 8]:
                    width, height = height, width

                if width >= height:
                    return "Landscape"
                else:
                    return "Portrait"
        except (UnidentifiedImageError, OSError):
            return "Raw"
        except Exception:
            return "Raw"

    def is_photo_from_current_session(self, filepath, source_stat):
        """Use filesystem time first, then camera EXIF time for removable cards."""
        if max(source_stat.st_ctime, source_stat.st_mtime) >= self.sorter_start_time:
            return True
        try:
            with Image.open(filepath) as img:
                exif = img.getexif()
                capture_time = exif.get(36867) or exif.get(306)
            if not capture_time:
                return False
            captured = time.strptime(str(capture_time), "%Y:%m:%d %H:%M:%S")
            return time.mktime(captured) >= self.sorter_start_time
        except (ValueError, TypeError, UnidentifiedImageError, OSError, OverflowError):
            return False

    def find_available_sd_source(self, configured_path):
        """Find the configured card path, even when Windows assigns a new drive letter."""
        configured_path = os.path.normpath(configured_path)
        if os.path.isdir(configured_path):
            return configured_path
        drive, tail = os.path.splitdrive(configured_path)
        relative_path = tail.lstrip("\\/") or "DCIM"
        drive_mask = windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if not (drive_mask & (1 << index)):
                continue
            root = f"{chr(65 + index)}:\\"
            if windll.kernel32.GetDriveTypeW(c_wchar_p(root)) != 2:
                continue
            candidate = os.path.join(root, relative_path)
            if os.path.isdir(candidate):
                return os.path.normpath(candidate)
        return None

    def get_sd_card_identity(self, source_path):
        drive = os.path.splitdrive(os.path.abspath(source_path))[0] + "\\"
        volume_name = create_unicode_buffer(261)
        filesystem_name = create_unicode_buffer(261)
        serial_number = c_ulong()
        max_component_length = c_ulong()
        flags = c_ulong()
        if windll.kernel32.GetVolumeInformationW(
                c_wchar_p(drive), volume_name, len(volume_name), byref(serial_number),
                byref(max_component_length), byref(flags), filesystem_name, len(filesystem_name)):
            return f"{serial_number.value:08x}"
        return drive.upper()

    def index_sd_files(self, source_path, previous_manifest):
        """Index names and metadata only; image contents are not read during this pass."""
        current_manifest = {}
        candidates = []
        folders = [source_path]
        while folders:
            folder = folders.pop()
            try:
                entries = os.scandir(folder)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            folders.append(entry.path)
                            continue
                        if not entry.name.lower().endswith((".jpg", ".jpeg")):
                            continue
                        item_stat = entry.stat(follow_symlinks=False)
                        relative = os.path.relpath(entry.path, source_path).replace("\\", "/")
                        signature = [item_stat.st_size, item_stat.st_mtime_ns,
                                     not bool(item_stat.st_mode & stat.S_IWRITE)]
                        current_manifest[relative] = signature
                        if previous_manifest.get(relative) != signature:
                            candidates.append((entry.path, entry.name, item_stat))
                    except OSError:
                        continue
        return candidates, current_manifest

    def eject_sd_card(self, source_path):
        """Safely eject the volume after all transfers have completed."""
        drive = os.path.splitdrive(os.path.abspath(source_path))[0]
        if not drive:
            return False
        handle = windll.kernel32.CreateFileW(
            c_wchar_p(f"\\\\.\\{drive}"), 0x80000000 | 0x40000000, 3, None, 3, 0, None)
        if handle == -1:
            return False
        returned = c_ulong()
        try:
            locked = windll.kernel32.DeviceIoControl(handle, 0x00090018, None, 0, None, 0, byref(returned), None)
            if locked:
                windll.kernel32.DeviceIoControl(handle, 0x00090020, None, 0, None, 0, byref(returned), None)
            return bool(windll.kernel32.DeviceIoControl(handle, 0x002D4808, None, 0, None, 0, byref(returned), None))
        finally:
            windll.kernel32.CloseHandle(handle)

    def toggle_sorter(self):
        if not self.is_sorting and not self.require_license():
            return
        if not self.is_sorting:
            paths = {
                "SD source": self.src_path_var.get(),
                "Locked destination": self.dest_prot_var.get(),
                "RAW destination": self.dest_raw_var.get(),
            }
            invalid = [f"{name}: {path}" for name, path in paths.items() if not os.path.isdir(path)]
            if invalid:
                messagebox.showerror("Import Error", "Select valid folders before starting:\n\n" + "\n".join(invalid))
                return

            self.is_sorting = True
            self.btn_start_sorter.config(text="STOP COPYING", bg="#dc3545")
            self.sorter_thread = threading.Thread(target=self.sorter_monitor_loop, daemon=True)
            self.sorter_thread.start()
            self.sorter_queue_log("SD Card Monitoring started...")
        else:
            self.is_sorting = False
            self.btn_start_sorter.config(text="Stopping...", state="disabled", bg="#6c757d")

    def sorter_monitor_loop(self):
        configured_src = self.src_path_var.get()
        src = None
        dest_prot = self.dest_prot_var.get()
        dest_raw = self.dest_raw_var.get()
        valid_exts = ('.jpg', '.jpeg')
        scan_interval = max(0.1, float(self.import_scan_interval_var.get()))
        copy_workers = max(1, int(self.import_workers_var.get()))
        db = open_import_db()
        manifests = load_card_manifests()
        last_scan_state = None
        source_available = False
        eject_attempted = False

        try:
            while self.is_sorting:
                try:
                    available_src = self.find_available_sd_source(configured_src)
                    if not available_src:
                        source_available = False
                        if last_scan_state != "missing":
                            self.sorter_queue_log(f"Waiting for SD card: {configured_src}")
                            last_scan_state = "missing"
                        time.sleep(scan_interval)
                        continue
                    src = available_src
                    if source_available:
                        time.sleep(scan_interval)
                        continue
                    source_available = True
                    eject_attempted = False
                    self.sorter_queue_log(f"SD card detected. Checking new photos in {src}")

                    card_id = self.get_sd_card_identity(src)
                    previous_manifest = manifests.get(card_id, {})
                    indexed_files, current_manifest = self.index_sd_files(src, previous_manifest)
                    self.gui_queue.put({"action": "import_progress", "current": 0,
                                       "total": len(indexed_files),
                                       "message": f"SD card indexed: 0/{len(indexed_files)} new photos"})
                    all_files = []
                    scanned_count = 0
                    already_count = 0
                    for source_path, filename, source_stat in indexed_files:
                        if not self.is_sorting:
                            break
                        scanned_count += 1
                        content_hash = sha256_file(source_path)
                        existing = find_import_by_hash(db, content_hash)
                        if existing and existing[2] == "IMPORTED" and existing[1] and os.path.exists(existing[1]):
                            already_count += 1
                            if scanned_count == len(indexed_files) or scanned_count % 5 == 0:
                                self.gui_queue.put({"action": "import_progress", "current": scanned_count,
                                                   "total": max(1, len(indexed_files)),
                                                   "message": f"Indexing {scanned_count}/{len(indexed_files)} photos"})
                            continue
                        all_files.append((source_path, filename, source_stat, content_hash))
                        if scanned_count == len(indexed_files) or scanned_count % 5 == 0:
                            self.gui_queue.put({"action": "import_progress", "current": scanned_count,
                                               "total": max(1, len(indexed_files)),
                                               "message": f"Indexing {scanned_count}/{len(indexed_files)} photos"})

                    self.gui_queue.put({"action": "import_progress", "current": 0,
                                       "total": len(all_files),
                                       "message": f"Ready to transfer {len(all_files)} new photos"})

                    if not self.is_sorting:
                        break

                    if not all_files:
                        self.sorter_queue_log(f"No new photos. Indexed {len(current_manifest)} files.")
                        self.gui_queue.put({"action": "import_progress", "current": 0, "total": 1,
                                           "message": f"SD card ready: {len(current_manifest)} files already imported"})
                        manifests[card_id] = current_manifest
                        save_card_manifests(manifests)
                        if not eject_attempted:
                            eject_attempted = True
                            if self.eject_sd_card(src):
                                self.sorter_queue_log("SD card safely ejected. Safe to remove.")
                            else:
                                self.sorter_queue_log("Transfer complete, but Windows could not safely eject the SD card.")
                        continue

                    last_scan_state = "processing"

                    all_files.sort(key=lambda x: x[2].st_ctime)
                    copy_tasks = []
                    seen_hashes = set()
                    for source_path, filename, source_stat, content_hash in all_files:
                        if content_hash in seen_hashes:
                            already_count += 1
                            self.sorter_queue_log(f"[DUPLICATE] {filename} skipped (same scan)")
                            continue
                        seen_hashes.add(content_hash)
                        orient = self.get_orientation_folder(source_path)
                        cursor = db.execute("""
                            INSERT OR REPLACE INTO imports
                            (content_hash, source_filename, source_relative_path, file_size,
                             modified_time, created_time, status)
                            VALUES (?, ?, ?, ?, ?, ?, 'IMPORTING')
                        """, (content_hash, filename, os.path.relpath(source_path, src),
                               source_stat.st_size, source_stat.st_mtime, source_stat.st_ctime))
                        db.commit()
                        copy_tasks.append({
                            'source': source_path,
                            'original_filename': filename,
                            'file_ctime': source_stat.st_ctime,
                            'file_size': source_stat.st_size,
                            'content_hash': content_hash,
                            'is_locked': self.is_locked(source_path),
                            'orient': orient,
                            'dest_prot': dest_prot,
                            'dest_raw': dest_raw
                        })

                    with ThreadPoolExecutor(max_workers=copy_workers) as executor:
                        futures = [(executor.submit(self._copy_task, task), task) for task in copy_tasks]
                        copied_count = 0
                        failed_count = 0
                        for future, task in futures:
                            if not self.is_sorting:
                                break
                            try:
                                result = future.result()
                                copied_count += 1
                                self.sorter_queue_log(result)
                                self.gui_queue.put({"action": "import_progress", "current": copied_count + failed_count,
                                                   "total": len(copy_tasks),
                                                   "message": f"Transferring {copied_count + failed_count}/{len(copy_tasks)} photos"})
                                db.execute("""
                                    UPDATE imports SET imported_at = ?, destination_raw = ?,
                                    destination_protected = ?, status = 'IMPORTED'
                                    WHERE content_hash = ?
                                """, (time.strftime("%Y-%m-%dT%H:%M:%S"), task['raw_destination'],
                                      task.get('protected_destination'), task['content_hash']))
                                db.commit()
                            except Exception as e:
                                failed_count += 1
                                self.gui_queue.put({"action": "import_progress", "current": copied_count + failed_count,
                                                   "total": len(copy_tasks),
                                                   "message": f"Transferring {copied_count + failed_count}/{len(copy_tasks)} photos ({failed_count} failed)"})
                                db.execute("UPDATE imports SET status = 'FAILED' WHERE content_hash = ?",
                                           (task['content_hash'],))
                                db.commit()
                                self.sorter_queue_log(f"[FAILED] {task['original_filename']}: {e}")

                    self.sorter_queue_log(
                        f"Import complete. New photos: {copied_count}; Already imported: {already_count}; "
                        f"Failed: {failed_count}; Total scanned: {scanned_count}"
                    )
                    if copied_count > 0:
                        self.root.after(100, lambda count=copied_count: TransferNotification(self.root, count, dest_raw))
                    if failed_count == 0:
                        self.gui_queue.put({"action": "import_progress", "current": len(copy_tasks),
                                           "total": max(1, len(copy_tasks)),
                                           "message": f"Transfer complete: {copied_count} photos"})
                        manifests[card_id] = current_manifest
                        save_card_manifests(manifests)
                        if not eject_attempted:
                            eject_attempted = True
                            if self.eject_sd_card(src):
                                self.sorter_queue_log("SD card safely ejected. Safe to remove.")
                            else:
                                self.sorter_queue_log("Transfer complete, but Windows could not safely eject the SD card.")
                    else:
                        # Re-index a failed batch instead of marking it complete or ejecting the card.
                        source_available = False
                    time.sleep(scan_interval)

                except (OSError, PermissionError):
                    if last_scan_state != "inaccessible":
                        self.sorter_queue_log(f"SD source temporarily inaccessible: {src}")
                        last_scan_state = "inaccessible"
                    time.sleep(scan_interval)
                    continue
        finally:
            db.close()

        self.btn_start_sorter.config(text="START MONITORING & COPYING", bg="#007bff", state="normal")
        self.sorter_queue_log("Monitoring Stopped.")

    def _copy_readonly_file(self, source, dest):
        try:
            shutil.copyfile(source, dest)
            return True
        except (OSError, shutil.Error):
            return False

    def _atomic_copy_verified(self, source, destination, expected_size, expected_hash):
        temporary = f"{destination}.part-{uuid.uuid4().hex}"
        try:
            if os.path.exists(destination):
                raise FileExistsError(destination)
            shutil.copyfile(source, temporary)
            if os.path.getsize(temporary) != expected_size or sha256_file(temporary) != expected_hash:
                raise IOError(f"Verification failed for {os.path.basename(source)}")
            # Windows rename is atomic and refuses to overwrite an existing file.
            os.rename(temporary, destination)
        finally:
            if os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    def _copy_task(self, task):
        if not self.require_license(notify=False):
            raise PermissionError("License required to import photos.")
        source = task['source']
        original_filename = task['original_filename']
        is_locked = task['is_locked']
        orient = task['orient']
        dest_prot = task['dest_prot']
        dest_raw = task['dest_raw']

        os.makedirs(dest_raw, exist_ok=True)
        # Reserve a unique output name, then release the lock during the expensive copies.
        with self.sd_destination_lock:
            new_filename = generate_sd_filename(source, dest_raw)
        target_path_raw = os.path.join(dest_raw, new_filename)
        self._atomic_copy_verified(source, target_path_raw, task['file_size'], task['content_hash'])

        target_path_prot = None
        if is_locked:
            prot_folder = os.path.join(dest_prot, orient)
            os.makedirs(prot_folder, exist_ok=True)
            target_path_prot = os.path.join(prot_folder, new_filename)
            self._atomic_copy_verified(target_path_raw, target_path_prot,
                                       task['file_size'], task['content_hash'])

        task['raw_destination'] = target_path_raw
        task['protected_destination'] = target_path_prot
        if is_locked:
            return f"[LOCKED | {orient}] {original_filename} → {new_filename}"
        return f"[RAW] {original_filename} → {new_filename}"

    # ========================================================
    # DATA SAVING / LOADING
    # ========================================================
    def save_config(self, silent=False):
        self.template_profiles[self.template_orientation] = self._template_state()
        data = {
            "template": self.template_path,
            "boxes": self.boxes,
            "box_labels": self.box_labels,
            "source": self.source_folder,
            "output": self.output_folder,
            "sd_source": self.src_path_var.get() if hasattr(self, 'src_path_var') and os.path.isdir(self.src_path_var.get()) else "",
            "sd_dest_prot": self.dest_prot_var.get() if hasattr(self, 'dest_prot_var') and os.path.isdir(self.dest_prot_var.get()) else "",
            "sd_dest_raw": self.dest_raw_var.get() if hasattr(self, 'dest_raw_var') and os.path.isdir(self.dest_raw_var.get()) else "",
            "sharpen_value": self.sharpen_value,
            "event_name": self.event_name_var.get() if hasattr(self, "event_name_var") else self.event_name,
            "mother_folder": self.mother_folder_var.get() if hasattr(self, "mother_folder_var") else self.mother_folder,
            "event_folder": self.event_folder,
            "auto_print": self.auto_print_var.get() if hasattr(self, "auto_print_var") else False,
            "landscape_enabled": self.landscape_enabled_var.get() if hasattr(self, "landscape_enabled_var") else True,
            "portrait_enabled": self.portrait_enabled_var.get() if hasattr(self, "portrait_enabled_var") else True,
            "verify_copy": self.verify_copy_var.get() if hasattr(self, "verify_copy_var") else True,
            "import_scan_interval": self.import_scan_interval_var.get() if hasattr(self, "import_scan_interval_var") else 2,
            "import_workers": self.import_workers_var.get() if hasattr(self, "import_workers_var") else 10,
            "sound_notifications": self.sound_notifications_var.get() if hasattr(self, "sound_notifications_var") else True,
            "template_orientation": self.template_orientation,
            "templates": self.template_profiles
        }
        with open(CONFIG_FILE, "w") as f: json.dump(data, f)
        if not silent:
            messagebox.showinfo("Salikha", "Settings and Layout successfully saved!")

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)

                self.template_path = data.get("template", "")
                if self.template_path and os.path.exists(self.template_path):
                    img = Image.open(self.template_path)
                    disp = img.copy()
                    disp.thumbnail((600, 800))
                    self.scale = img.width / disp.width
                    self.tk_t = ImageTk.PhotoImage(disp)
                    self.canvas.config(width=disp.width, height=disp.height)
                    self.canvas.create_image(0, 0, anchor="nw", image=self.tk_t, tags="bg_template")
                    self.canvas.create_rectangle(
                        2, 2, disp.width-2, disp.height-2,
                        outline="#00ff00", width=3, dash=(5, 5), tags="template_border"
                    )

                self.boxes = data.get("boxes", [])
                self.box_labels = data.get("box_labels", [])
                self.box_items = []
                for box in self.boxes:
                    scaled_box = [x / self.scale for x in box]
                    item = self.canvas.create_rectangle(*scaled_box, outline="#00aaff", width=3, tags="photobox")
                    self.box_items.append(item)
                if len(self.box_labels) < len(self.box_items):
                    self.box_labels = [i + 1 for i in range(len(self.box_items))]
                self.relabel_boxes()
                self.selected_item = None
                self.selected_index = None
                self.handle_regions = {}
                self.canvas.delete("resizehandle")
                self.canvas.delete("cropguide")
                self._refresh_box_pickers()
                self.template_profiles["Landscape"] = self._template_state()
                configured_templates = data.get("templates")
                if configured_templates:
                    self.template_profiles.update(configured_templates)
                    self.template_orientation = data.get("template_orientation", "Landscape")
                    self.template_orientation_var.set(self.template_orientation)
                    self._restore_template_state(self.template_profiles.get(self.template_orientation, {}))

                src = data.get("source"); out = data.get("output")
                if src:
                    self.source_folder = src
                    if not os.path.exists(src): os.makedirs(src, exist_ok=True)
                if out:
                    self.output_folder = out
                    self.out_entry.delete(0, tk.END); self.out_entry.insert(0, out)
                    if not os.path.exists(out): os.makedirs(out, exist_ok=True)

                if data.get("sd_source") and hasattr(self, 'src_path_var'): self.src_path_var.set(data["sd_source"])
                if data.get("sd_dest_prot") and hasattr(self, 'dest_prot_var'): self.dest_prot_var.set(data["sd_dest_prot"])
                if data.get("sd_dest_raw") and hasattr(self, 'dest_raw_var'): self.dest_raw_var.set(data["sd_dest_raw"])

                self.event_name = data.get("event_name", "")
                self.mother_folder = data.get("mother_folder", "")
                self.event_folder = data.get("event_folder", "")
                if self.event_folder:
                    self.source_folder = os.path.join(self.event_folder, "02_Locked")
                    self.output_folder = os.path.join(self.event_folder, "03_Print_Output")
                    self.sd_dest_prot = os.path.join(self.event_folder, "02_Locked")
                    self.sd_dest_raw = os.path.join(self.event_folder, "01_Raw_Archive")
                    self.dest_prot_var.set(self.sd_dest_prot)
                    self.dest_raw_var.set(self.sd_dest_raw)
                    self.out_entry.delete(0, tk.END)
                    self.out_entry.insert(0, self.output_folder)
                    os.makedirs(self.source_folder, exist_ok=True)
                    os.makedirs(self.output_folder, exist_ok=True)
                if hasattr(self, "event_name_var"):
                    self.event_name_var.set(self.event_name)
                    self.mother_folder_var.set(self.mother_folder)
                    self.update_event_preview()
                if self.event_folder and hasattr(self, "event_status"):
                    self.event_status.config(text=f"Active event: {self.event_name}\nWorkspace: {self.event_folder}", foreground=self.colors["success"])
                if hasattr(self, "auto_print_var"):
                    self.auto_print_var.set(data.get("auto_print", False))
                    self.landscape_enabled_var.set(data.get("landscape_enabled", True))
                    self.portrait_enabled_var.set(data.get("portrait_enabled", True))
                    self.verify_copy_var.set(data.get("verify_copy", True))
                    self.import_scan_interval_var.set(data.get("import_scan_interval", 2))
                    self.import_workers_var.set(data.get("import_workers", 10))
                    self.sound_notifications_var.set(data.get("sound_notifications", True))

                if data.get("sharpen_value") is not None:
                    self.sharpen_value = data.get("sharpen_value", 0)
                    if hasattr(self, 'sharpen_var'):
                        self.sharpen_var.set(self.sharpen_value)
                    if hasattr(self, 'sharpen_lbl'):
                        self.sharpen_lbl.config(text=f"Sharpness: {self.sharpen_value}%")

            except Exception as e:
                print(f"Error loading config: {e}")

class SalikhaHandler(FileSystemEventHandler):
    def __init__(self, callback, log_func, engine_start_time):
        self.callback = callback; self.log = log_func
        self.engine_start_time = engine_start_time

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(('.jpg', '.jpeg', '.png')):
            self.callback(event.src_path)

    def on_moved(self, event):
        if not event.is_directory and event.dest_path.lower().endswith(('.jpg', '.jpeg', '.png')):
            self.callback(event.dest_path)

def main():
    import sys
    from licensing import LicenseManager, LicenseError
    from licensing_ui import show_license_dialog
    root = tk.Tk()
    root.withdraw()
    resource_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(resource_dir, "Salikha Photoman Ico.ico")
    if os.path.isfile(icon_path):
        root.photoman_icon_path = icon_path
        root.iconbitmap(icon_path)
    try:
        manager = LicenseManager(APP_DATA_DIR)
        try:
            manager.require()
        except LicenseError:
            if not show_license_dialog(root, manager, required=True):
                root.destroy()
                return
        SalikhaStudioPro(root, license_manager=manager)
        root.deiconify()
        manager.refresh_in_background()
        root.mainloop()
    except (LicenseError, OSError) as exc:
        messagebox.showerror("Salikha License", str(exc), parent=root)
        root.destroy()

if __name__ == "__main__":
    main()
