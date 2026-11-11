# -*- coding: utf-8 -*-
"""YouTube Downloader: оконная программа для скачивания видео (MP4) и звука (MP3) с YouTube.

При первом запуске сама скачивает в %LOCALAPPDATA%\\YTDownloader\\tools:
  * yt-dlp.exe  — загрузчик (обновляется при каждом запуске)
  * ffmpeg.exe  — склейка видео+звука, MP3
  * deno.exe    — JS-движок, нужен yt-dlp для YouTube
"""
import os
import re
import sys
import json
import queue
import shutil
import zipfile
import threading
import subprocess
import urllib.request

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:  # только для тестов без GUI
    tk = None

APP_NAME = "YouTube Downloader"
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
BASE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "YTDownloader")
TOOLS = os.path.join(BASE, "tools")
SETTINGS = os.path.join(BASE, "settings.json")
YTDLP = os.path.join(TOOLS, "yt-dlp" + EXE)
FFMPEG = os.path.join(TOOLS, "ffmpeg" + EXE)
DENO = os.path.join(TOOLS, "deno" + EXE)
NO_WINDOW = 0x08000000 if IS_WIN else 0

URL_YTDLP = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
URL_FFMPEG = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
URL_DENO = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

FORMATS = {
    "best": ["-f", "bv*+ba/b", "--merge-output-format", "mp4", "--embed-metadata"],
    "compat": ["-f", "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/bv*+ba/b",
               "--merge-output-format", "mp4", "--embed-metadata"],
    "mp3": ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "320K",
            "--embed-thumbnail", "--convert-thumbnails", "jpg", "--embed-metadata"],
}
PROGRESS_TEMPLATE = ("download:[P]%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
                     "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s")

# Цвета
BG = "#EEF0FF"
CARD = "#FFFFFF"
SOFT = "#F4F1FF"
SOFT_H = "#E9E3FF"
BORDER = "#DDD6FF"
ACCENT = "#7B61FF"
ACCENT_H = "#6A4FF0"
TEXT = "#1D1B3A"
MUTED = "#6A6690"
OK = "#22B573"
ERR = "#FF5C7A"
ERR_H = "#F0476A"


# ------------------------------------------------------------------ утилиты
def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def default_out_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads", "YouTube")


def tools_ready():
    return os.path.isfile(YTDLP) and os.path.isfile(FFMPEG)


def tool_env():
    env = os.environ.copy()
    env["PATH"] = TOOLS + os.pathsep + env.get("PATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def human_size(b):
    if b is None:
        return "?"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if b < 1024 or unit == "ГБ":
            return ("%.0f %s" if unit in ("Б", "КБ") else "%.1f %s") % (b, unit)
        b /= 1024.0


def human_eta(s):
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


def clean_name(path):
    n = re.split(r"[\\/]", path.strip().strip('"'))[-1]
    n = re.sub(r"\.f\d+(-\d+)?\.\w+$", "", n)
    n = re.sub(r"\.(part|temp|ytdl)$", "", n)
    n = re.sub(r"\.\w{2,5}$", "", n)
    n = re.sub(r"\s*\[[\w-]{11}\]$", "", n)
    return n


def friendly_error(err):
    if not err:
        return "Нажми «Подробности», чтобы увидеть, что случилось."
    rules = [
        (r"Sign in to confirm|not a bot", "YouTube просит подтвердить, что ты не бот. Попробуй ещё раз через пару минут."),
        (r"Private video", "Это приватное видео, его нельзя скачать."),
        (r"age-restricted|confirm your age|inappropriate", "Видео 18+, без входа в аккаунт его не скачать."),
        (r"Video unavailable|is not available", "Видео недоступно."),
        (r"Unsupported URL|is not a valid URL", "Это не похоже на ссылку на видео."),
        (r"getaddrinfo|Unable to download|timed out|Connection|Network", "Проблема с интернетом. Проверь соединение и попробуй ещё раз."),
    ]
    for pat, msg in rules:
        if re.search(pat, err, re.I):
            return msg
    return err


class JobParser:
    """Разбирает вывод yt-dlp в понятный статус."""

    def __init__(self):
        self.name = ""
        self.file = ""
        self.stage = "Подключаюсь к YouTube…"
        self.pct = None
        self.info = ""
        self.item = ""
        self.err = ""

    def feed(self, line):
        L = line.strip()
        if not L:
            return
        if L.startswith("[P]"):
            p = (L[3:].split("|") + [None] * 5)[:5]
            dl, total, est, speed, eta = [num(x) for x in p]
            t = total or est
            self.stage = "Скачиваю…"
            self.pct = min(100.0, dl * 100.0 / t) if (dl is not None and t) else None
            bits = []
            if dl is not None:
                bits.append(human_size(dl) + (" из " + human_size(t) if t else ""))
            if speed:
                bits.append(human_size(speed) + "/с")
            if eta is not None:
                bits.append("осталось " + human_eta(eta))
            self.info = "  ·  ".join(bits)
            return
        m = re.match(r"\[download\] Downloading item (\d+) of (\d+)", L)
        if m:
            self.item = "Плейлист: %s из %s" % m.groups()
            self.pct, self.info = None, ""
            return
        m = re.match(r"\[download\] Destination: (.+)$", L)
        if m:
            self.file, self.name = m.group(1), clean_name(m.group(1))
            self.stage, self.pct, self.info = "Скачиваю…", 0.0, ""
            return
        m = re.match(r"\[download\] (.+) has already been downloaded", L)
        if m:
            self.file, self.name = m.group(1), clean_name(m.group(1))
            self.stage, self.pct, self.info = "Этот файл уже был скачан раньше", 100.0, ""
            return
        m = re.match(r'\[Merger\] Merging formats into "(.+)"', L)
        if m:
            self.file, self.name = m.group(1), clean_name(m.group(1))
            self.stage, self.pct, self.info = "Склеиваю видео и звук…", None, ""
            return
        m = re.match(r"\[ExtractAudio\] Destination: (.+)$", L)
        if m:
            self.file = m.group(1)
            self.stage, self.pct, self.info = "Конвертирую в MP3…", None, ""
            return
        if L.startswith("[ExtractAudio]"):
            self.stage, self.pct, self.info = "Конвертирую в MP3…", None, ""
        elif L.startswith(("[EmbedThumbnail]", "[ThumbnailsConvertor]")):
            self.stage, self.pct, self.info = "Добавляю обложку…", None, ""
        elif L.startswith("[Metadata]"):
            self.stage, self.pct, self.info = "Записываю название и данные…", None, ""
        elif L.startswith("[youtube") and self.pct is None and self.stage.startswith(("Подкл", "Получ")):
            self.stage = "Получаю информацию о видео…"
        elif L.startswith("ERROR:"):
            self.err = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]{11}:\s*)?", "", L)


def build_args(urls, mode, out_dir, playlist):
    return ([YTDLP, "--newline", "--encoding", "utf-8", "--ffmpeg-location", TOOLS,
             "-N", "4", "--windows-filenames", "--no-mtime",
             "--yes-playlist" if playlist else "--no-playlist",
             "-P", out_dir, "-o", "%(title)s [%(id)s].%(ext)s",
             "--progress-template", PROGRESS_TEMPLATE]
            + FORMATS[mode] + ["--"] + list(urls))


def http_download(url, dest, on_progress):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 YTDownloader"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            on_progress(done, total)


def extract_from_zip(zip_path, wanted):
    found = set()
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            base = os.path.basename(member)
            if base.lower() in wanted:
                part = os.path.join(TOOLS, base + ".part")
                with z.open(member) as src, open(part, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(part, os.path.join(TOOLS, base))
                found.add(base.lower())
    return found


# ------------------------------------------------------------------ виджеты
if tk is not None:
    class FlatButton(tk.Label):
        def __init__(self, master, text, command, bg=SOFT, fg=ACCENT_H, hover=SOFT_H, font=None, padx=14, pady=7):
            super().__init__(master, text=text, bg=bg, fg=fg, font=font, padx=padx, pady=pady, cursor="hand2")
            self._bg, self._hover, self._cmd, self.enabled = bg, hover, command, True
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", lambda e: self.config(bg=self._bg))
            self.bind("<ButtonRelease-1>", self._on_click)

        def _on_enter(self, _e):
            if self.enabled:
                self.config(bg=self._hover)

        def _on_click(self, _e):
            if self.enabled and self._cmd:
                self._cmd()

        def update_style(self, text=None, bg=None, hover=None, enabled=None):
            if text is not None:
                self.config(text=text)
            if bg is not None:
                self._bg = bg
                self.config(bg=bg)
            if hover is not None:
                self._hover = hover
            if enabled is not None:
                self.enabled = enabled
                self.config(cursor="hand2" if enabled else "arrow")


# ------------------------------------------------------------------ приложение
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.state = "idle"  # setup | setupfail | updating | idle | run
        self.proc = None
        self.cancelled = False
        self.parser = JobParser()
        self.log_lines = []
        self.log_visible = False
        self.last_file = ""

        self.out_dir = default_out_dir()
        self.mode = "best"
        self.load_settings()

        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.f_title = ("Segoe UI Semibold", 17)
        self.f_sub = ("Segoe UI", 10)
        self.f_norm = ("Segoe UI", 10)
        self.f_bold = ("Segoe UI Semibold", 10)
        self.f_small = ("Segoe UI", 9)
        self.f_big = ("Segoe UI Semibold", 13)
        self.f_label = ("Segoe UI Semibold", 8)

        root.title(APP_NAME)
        root.configure(bg=BG)
        w, h = self.px(760), self.px(690)
        root.geometry("%dx%d+%d+%d" % (w, h, (root.winfo_screenwidth() - w) // 2,
                                        max(0, (root.winfo_screenheight() - h) // 2 - self.px(30))))
        root.minsize(self.px(640), self.px(560))
        try:
            root.iconbitmap(resource("icon.ico"))
        except Exception:
            pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_ui()
        self.set_mode(self.mode)
        self.root.after(100, self.pump)

        if tools_ready():
            self.start_update()
        else:
            self.start_setup()

    def px(self, n):
        return int(n * self.scale)

    # ---------------- настройки
    def load_settings(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("out_dir"):
                self.out_dir = data["out_dir"]
            if data.get("mode") in FORMATS:
                self.mode = data["mode"]
        except Exception:
            pass

    def save_settings(self):
        try:
            os.makedirs(BASE, exist_ok=True)
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"out_dir": self.out_dir, "mode": self.mode}, f, ensure_ascii=False)
        except Exception:
            pass

    # ---------------- интерфейс
    def card(self, parent):
        return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
                        padx=self.px(18), pady=self.px(16))

    def build_ui(self):
        P = self.px
        wrap = tk.Frame(self.root, bg=BG, padx=P(24), pady=P(18))
        wrap.pack(fill="both", expand=True)

        # Шапка
        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x", pady=(0, P(14)))
        logo = tk.Canvas(head, width=P(46), height=P(46), bg=BG, highlightthickness=0)
        logo.pack(side="left", padx=(0, P(12)))
        r = P(12)
        s = P(46)
        for x0, y0, x1, y1 in ((0, r, s, s - r), (r, 0, s - r, s)):
            logo.create_rectangle(x0, y0, x1, y1, fill="#FF4D6D", outline="")
        for cx, cy in ((0, 0), (s - 2 * r, 0), (0, s - 2 * r), (s - 2 * r, s - 2 * r)):
            logo.create_oval(cx, cy, cx + 2 * r, cy + 2 * r, fill="#FF4D6D", outline="")
        logo.create_polygon(P(18), P(13), P(18), P(33), P(34), P(23), fill="white", outline="")
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left")
        tk.Label(titles, text=APP_NAME, font=self.f_title, bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(titles, text="Вставь ссылку — скачаю в максимальном качестве",
                 font=self.f_sub, bg=BG, fg=MUTED).pack(anchor="w")

        # Карточка с настройками
        c1 = self.card(wrap)
        c1.pack(fill="x", pady=(0, P(12)))

        row = tk.Frame(c1, bg=CARD)
        row.pack(fill="x")
        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(row, textvariable=self.url_var, font=("Segoe UI", 11), relief="flat", bg=SOFT,
                                  fg=TEXT, insertbackground=TEXT, highlightthickness=1,
                                  highlightbackground=BORDER, highlightcolor=ACCENT)
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=P(9), ipadx=P(8))
        FlatButton(row, "Вставить", self.paste_url, font=self.f_bold, pady=P(9)).pack(side="left", padx=(P(8), 0))
        self.setup_entry_shortcuts()

        tk.Label(c1, text="ФОРМАТ", font=self.f_label, bg=CARD, fg=MUTED).pack(anchor="w", pady=(P(14), P(6)))
        fmts = tk.Frame(c1, bg=CARD)
        fmts.pack(fill="x")
        self.fmt_cards = {}
        options = [("best", "MP4 · Максимум", "до 4K/8K, 60 fps, HDR"),
                   ("compat", "MP4 · Совместимый", "H.264, открывается везде"),
                   ("mp3", "MP3 · 320 kbps", "только звук + обложка")]
        for i, (key, title, desc) in enumerate(options):
            fmts.columnconfigure(i, weight=1, uniform="fmt")
            box = tk.Frame(fmts, bg=SOFT, highlightthickness=2, highlightbackground=SOFT, cursor="hand2",
                           padx=P(12), pady=P(10))
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else P(5), 0 if i == 2 else P(5)))
            t1 = tk.Label(box, text=title, font=self.f_bold, bg=SOFT, fg=TEXT, cursor="hand2")
            t1.pack(anchor="w")
            t2 = tk.Label(box, text=desc, font=self.f_small, bg=SOFT, fg=MUTED, cursor="hand2")
            t2.pack(anchor="w", pady=(P(2), 0))
            for wdg in (box, t1, t2):
                wdg.bind("<ButtonRelease-1>", lambda e, k=key: self.set_mode(k))
            self.fmt_cards[key] = (box, t1, t2)

        self.pl_var = tk.BooleanVar(value=False)
        tk.Checkbutton(c1, text="Скачать весь плейлист (если ссылка из плейлиста)", variable=self.pl_var,
                       font=self.f_norm, bg=CARD, fg=TEXT, activebackground=CARD, selectcolor=CARD,
                       highlightthickness=0, bd=0, cursor="hand2").pack(anchor="w", pady=(P(12), P(10)))

        folder = tk.Frame(c1, bg=SOFT, padx=P(10), pady=P(6))
        folder.pack(fill="x")
        tk.Label(folder, text="Папка:", font=self.f_bold, bg=SOFT, fg=MUTED).pack(side="left")
        self.out_label = tk.Label(folder, text=self.out_dir, font=self.f_small, bg=SOFT, fg=TEXT, anchor="w")
        self.out_label.pack(side="left", fill="x", expand=True, padx=(P(6), P(6)))
        FlatButton(folder, "Открыть", self.open_dir, bg=CARD, hover=SOFT_H, font=self.f_small, pady=P(4)).pack(side="right")
        FlatButton(folder, "Изменить", self.choose_dir, bg=CARD, hover=SOFT_H, font=self.f_small, pady=P(4)).pack(side="right", padx=(0, P(6)))

        self.go_btn = FlatButton(c1, "⬇  Скачать", self.on_main_button, bg=ACCENT, fg="white", hover=ACCENT_H,
                                 font=self.f_big, pady=P(12))
        self.go_btn.pack(fill="x", pady=(P(16), 0))

        # Карточка со статусом
        c2 = self.card(wrap)
        c2.pack(fill="both", expand=True)
        self.st_title = tk.Label(c2, text="Готов к загрузке", font=self.f_bold, bg=CARD, fg=TEXT, anchor="w")
        self.st_title.pack(fill="x")

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        for name, color in (("Run", ACCENT), ("Ok", OK), ("Err", ERR)):
            style.configure(name + ".Horizontal.TProgressbar", troughcolor=SOFT, background=color,
                            bordercolor=SOFT, lightcolor=color, darkcolor=color, thickness=P(10))
        self.bar = ttk.Progressbar(c2, style="Run.Horizontal.TProgressbar", mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(P(10), P(8)))
        self.bar_indeterminate = False

        self.st_info = tk.Label(c2, text="", font=self.f_small, bg=CARD, fg=MUTED, anchor="w", justify="left",
                                wraplength=P(660))
        self.st_info.pack(fill="x")

        links = tk.Frame(c2, bg=CARD)
        links.pack(fill="x", pady=(P(8), 0))
        self.log_link = tk.Label(links, text="Подробности ▾", font=self.f_small, bg=CARD, fg=ACCENT, cursor="hand2")
        self.log_link.pack(side="left")
        self.log_link.bind("<ButtonRelease-1>", lambda e: self.toggle_log())
        self.show_btn = FlatButton(links, "Показать файл", self.show_file, font=self.f_small, pady=P(4))

        self.log_box = tk.Text(c2, height=8, font=("Consolas", 9), bg="#1E1B33", fg="#CFC9FF", relief="flat",
                               wrap="word", padx=P(8), pady=P(6), state="disabled")

    def setup_entry_shortcuts(self):
        e = self.url_entry
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Вставить", command=self.paste_url)
        menu.add_command(label="Копировать", command=lambda: e.event_generate("<<Copy>>"))
        menu.add_command(label="Вырезать", command=lambda: e.event_generate("<<Cut>>"))
        menu.add_command(label="Очистить", command=lambda: self.url_var.set(""))
        e.bind("<Button-3>", lambda ev: menu.tk_popup(ev.x_root, ev.y_root))
        e.bind("<Return>", lambda ev: self.on_main_button())

        # Ctrl+V/C/X/A работают и на русской раскладке
        def on_ctrl(ev):
            code = ev.keycode
            if code == 86 and ev.keysym.lower() != "v":
                e.event_generate("<<Paste>>")
                return "break"
            if code == 67 and ev.keysym.lower() != "c":
                e.event_generate("<<Copy>>")
                return "break"
            if code == 88 and ev.keysym.lower() != "x":
                e.event_generate("<<Cut>>")
                return "break"
            if code == 65:
                e.select_range(0, "end")
                e.icursor("end")
                return "break"
            return None
        e.bind("<Control-KeyPress>", on_ctrl)
        e.focus_set()

    def set_mode(self, key):
        if self.state == "run":
            return
        self.mode = key
        for k, (box, t1, t2) in self.fmt_cards.items():
            sel = k == key
            bg = CARD if sel else SOFT
            box.config(bg=bg, highlightbackground=ACCENT if sel else SOFT)
            t1.config(bg=bg)
            t2.config(bg=bg)
        self.save_settings()

    def set_status(self, title, info=""):
        self.st_title.config(text=title)
        self.st_info.config(text=info)

    def set_bar(self, pct=None, kind="Run"):
        self.bar.config(style=kind + ".Horizontal.TProgressbar")
        if pct is None:
            if not self.bar_indeterminate:
                self.bar.config(mode="indeterminate")
                self.bar.start(12)
                self.bar_indeterminate = True
        else:
            if self.bar_indeterminate:
                self.bar.stop()
                self.bar.config(mode="determinate")
                self.bar_indeterminate = False
            self.bar["value"] = max(0, min(100, pct))

    def set_button(self, text, kind="go", enabled=True):
        colors = {"go": (ACCENT, ACCENT_H), "cancel": (ERR, ERR_H), "wait": ("#B7AEF0", "#B7AEF0")}
        bg, hover = colors[kind]
        self.go_btn.update_style(text=text, bg=bg, hover=hover, enabled=enabled)

    def toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_box.pack(fill="both", expand=True, pady=(self.px(8), 0))
            self.log_link.config(text="Скрыть подробности ▴")
            self.refresh_log()
        else:
            self.log_box.pack_forget()
            self.log_link.config(text="Подробности ▾")

    def add_log(self, line):
        self.log_lines.append(line)
        if len(self.log_lines) > 600:
            del self.log_lines[:100]

    def refresh_log(self):
        if not self.log_visible:
            return
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", "\n".join(self.log_lines[-200:]))
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    # ---------------- действия
    def paste_url(self):
        try:
            text = self.root.clipboard_get().strip()
        except Exception:
            text = ""
        if text:
            self.url_var.set(text)
            self.url_entry.icursor("end")
        self.url_entry.focus_set()

    def choose_dir(self):
        path = filedialog.askdirectory(initialdir=self.out_dir if os.path.isdir(self.out_dir) else None,
                                       title="Куда сохранять видео?")
        if path:
            self.out_dir = os.path.normpath(path)
            self.out_label.config(text=self.out_dir)
            self.save_settings()

    def open_dir(self):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            if IS_WIN:
                os.startfile(self.out_dir)
            else:
                subprocess.Popen(["xdg-open", self.out_dir])
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Не удалось открыть папку:\n%s" % ex)

    def show_file(self):
        f = self.last_file
        if f and not os.path.isabs(f):
            f = os.path.join(self.out_dir, f)
        if IS_WIN and f and os.path.isfile(f):
            subprocess.Popen('explorer /select,"%s"' % os.path.normpath(f))
        else:
            self.open_dir()

    def on_main_button(self):
        if self.state == "run":
            self.cancel_job()
        elif self.state == "setupfail":
            self.start_setup()
        elif self.state == "idle":
            self.start_download()

    def post(self, kind, data=None):
        self.q.put((kind, data))

    # ---------------- первый запуск: скачивание компонентов
    def start_setup(self):
        self.state = "setup"
        self.set_button("Подготовка…", "wait", False)
        self.set_bar(None)
        self.set_status("Первый запуск: скачиваю компоненты",
                        "Это нужно один раз, около 200 МБ. Подожди немного…")
        threading.Thread(target=self.setup_worker, daemon=True).start()

    def setup_worker(self):
        try:
            os.makedirs(TOOLS, exist_ok=True)
            steps = []
            if not os.path.isfile(YTDLP):
                steps.append(("yt-dlp — загрузчик", URL_YTDLP, None, True))
            if not os.path.isfile(FFMPEG):
                steps.append(("ffmpeg — обработка видео", URL_FFMPEG, {"ffmpeg.exe", "ffprobe.exe"}, True))
            if not os.path.isfile(DENO):
                steps.append(("Deno — нужен для YouTube", URL_DENO, {"deno.exe"}, False))
            n = len(steps)
            for i, (title, url, wanted, required) in enumerate(steps, 1):
                self.post("log", "Скачиваю %s" % url)
                tmp = os.path.join(TOOLS, "download_%d.tmp" % i)
                last = [-1]

                def prog(done, total, i=i, title=title):
                    pct = int(done * 100 / total) if total else None
                    if pct != last[0]:
                        last[0] = pct
                        self.post("setup_progress", (i, n, title, done, total))
                try:
                    http_download(url, tmp, prog)
                    if wanted is None:
                        os.replace(tmp, YTDLP)
                    else:
                        got = extract_from_zip(tmp, wanted)
                        os.remove(tmp)
                        if wanted - got:
                            raise RuntimeError("В архиве не найдено: " + ", ".join(sorted(wanted - got)))
                except Exception as ex:
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception:
                        pass
                    if required:
                        raise
                    self.post("log", "Не удалось скачать %s: %s" % (title, ex))
            self.post("setup_done")
        except Exception as ex:
            self.post("setup_fail", str(ex))

    # ---------------- обновление yt-dlp
    def start_update(self):
        self.state = "updating"
        self.set_button("Проверяю обновления…", "wait", False)
        self.set_bar(None)
        self.set_status("Проверяю обновления…", "Это займёт пару секунд")
        threading.Thread(target=self.update_worker, daemon=True).start()

    def update_worker(self):
        try:
            r = subprocess.run([YTDLP, "-U"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=120, creationflags=NO_WINDOW, env=tool_env())
            self.post("log", r.stdout.decode("utf-8", "replace").strip())
        except Exception as ex:
            self.post("log", "Обновление не удалось: %s" % ex)
        self.post("update_done")

    def become_idle(self, title="Всё готово! Вставь ссылку и нажми «Скачать»", info=""):
        self.state = "idle"
        self.set_button("⬇  Скачать", "go", True)
        self.set_bar(0)
        self.set_status(title, info)

    # ---------------- загрузка видео
    def start_download(self):
        urls = [u for u in re.split(r"\s+", self.url_var.get().replace('"', " ")) if u]
        if not urls:
            self.set_status("Сначала вставь ссылку на видео", "")
            self.url_entry.focus_set()
            return
        try:
            os.makedirs(self.out_dir, exist_ok=True)
        except Exception as ex:
            self.set_status("Не могу создать папку", "%s\n%s" % (self.out_dir, ex))
            return
        self.state = "run"
        self.cancelled = False
        self.parser = JobParser()
        self.last_file = ""
        self.show_btn.pack_forget()
        self.set_button("✕  Отменить", "cancel", True)
        self.set_bar(None)
        self.set_status("Подключаюсь к YouTube…", "")
        args = build_args(urls, self.mode, self.out_dir, self.pl_var.get())
        self.add_log("")
        self.add_log("> " + " ".join(urls))
        threading.Thread(target=self.job_worker, args=(args,), daemon=True).start()

    def job_worker(self, args):
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                                         env=tool_env(), cwd=TOOLS)
            for raw in self.proc.stdout:
                self.post("line", raw.decode("utf-8", "replace").rstrip("\r\n"))
            rc = self.proc.wait()
        except Exception as ex:
            self.post("line", "ERROR: %s" % ex)
            rc = -1
        self.post("job_done", rc)

    def kill_proc(self):
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            else:
                p.kill()
        except Exception:
            pass

    def cancel_job(self):
        self.cancelled = True
        self.set_button("Отменяю…", "wait", False)
        threading.Thread(target=self.kill_proc, daemon=True).start()

    def render_job(self):
        p = self.parser
        self.set_bar(p.pct)
        details = [p.stage + (" %.1f%%" % p.pct if (p.pct is not None and p.stage == "Скачиваю…") else "")]
        if p.info:
            details.append(p.info)
        if p.item:
            details.append(p.item)
        self.set_status(p.name or "Загрузка", "   ·   ".join(details))

    def finish_job(self, rc):
        self.state = "idle"
        self.proc = None
        self.set_button("⬇  Скачать", "go", True)
        p = self.parser
        self.last_file = p.file
        if self.cancelled:
            self.set_bar(0)
            self.set_status("Загрузка отменена", "")
        elif rc == 0:
            self.set_bar(100, "Ok")
            self.set_status("Готово!  ✔  " + (p.name or ""), "Сохранено в: " + self.out_dir)
            self.show_btn.pack(side="right")
            self.url_var.set("")
        else:
            self.set_bar(100, "Err")
            self.set_status("Не получилось скачать", friendly_error(p.err))

    # ---------------- обработка событий из потоков
    def pump(self):
        changed_job = False
        changed_log = False
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "line":
                    self.add_log(data)
                    self.parser.feed(data)
                    changed_job = changed_log = True
                elif kind == "log":
                    if data:
                        self.add_log(data)
                        changed_log = True
                elif kind == "setup_progress":
                    i, n, title, done, total = data
                    pct = done * 100.0 / total if total else None
                    self.set_bar(pct)
                    size = human_size(done) + (" из " + human_size(total) if total else "")
                    self.set_status("Первый запуск: скачиваю компоненты (%d из %d)" % (i, n),
                                    "%s   ·   %s" % (title, size))
                elif kind == "setup_done":
                    if tools_ready():
                        self.start_update()
                    else:
                        self.post("setup_fail", "не все файлы на месте")
                elif kind == "setup_fail":
                    self.state = "setupfail"
                    self.add_log("Ошибка: %s" % data)
                    changed_log = True
                    self.set_bar(100, "Err")
                    self.set_button("↻  Повторить", "go", True)
                    self.set_status("Не получилось скачать компоненты",
                                    "Проверь интернет и нажми «Повторить».  (%s)" % data)
                elif kind == "update_done":
                    self.become_idle()
                elif kind == "job_done":
                    if changed_job:
                        self.render_job()
                        changed_job = False
                    self.finish_job(data)
        except queue.Empty:
            pass
        if changed_job and self.state == "run":
            self.render_job()
        if changed_log:
            self.refresh_log()
        self.root.after(120, self.pump)

    def on_close(self):
        if self.state == "run":
            if not messagebox.askyesno(APP_NAME, "Идёт загрузка. Прервать её и выйти?"):
                return
            self.kill_proc()
        self.root.destroy()


def main():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YTDownloader.App")
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
