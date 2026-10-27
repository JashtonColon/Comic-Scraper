"""Comic Scraper app window. Run: python app/gui.py  (or use the installed Comic Scraper app)."""
import contextlib
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import scraper

APP_NAME = "Comic Scraper"
FROZEN = getattr(sys, "frozen", False)          # True when running as the installed .exe
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
# icon lives next to the .exe when installed, or in ../assets when running from source
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(CODE_DIR), "assets"))
SETTINGS_FILE = os.path.join(scraper.APP_DATA, "settings.json")
# where earlier versions saved it (copied over the first time)
OLD_SETTINGS_FILES = [os.path.join(CODE_DIR, "settings.json"),
                      os.path.join(os.path.dirname(CODE_DIR), "settings.json")]

DEFAULTS = {
    "start_url": "",
    "start_issue": "1",
    "end_issue": "1",
    "final_root": os.path.join(os.path.expanduser("~"), "Documents", "Comics"),
    "sub_path": "",
    "folder_name": "",
    "filename_prefix": "",
    "chrome_path": "",       # "" = find Chrome/Edge automatically
    "chrome_profile": "",    # "" = app data folder
}


def load_settings():
    data = dict(DEFAULTS)
    for path in [SETTINGS_FILE] + OLD_SETTINGS_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                data.update(json.load(f))
            break
        except (OSError, ValueError):
            continue
    return data


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


class QueueWriter:
    """Sends everything the scraper prints to the log box."""
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        try:
            self.iconbitmap(os.path.join(RESOURCE_DIR, "icon.ico"))
        except Exception:
            pass
        self.geometry("760x620")
        self.minsize(620, 480)

        self.settings = load_settings()
        self.verbose = tk.BooleanVar(value=bool(self.settings.pop("verbose", False)))
        self.hide_chrome = tk.BooleanVar(value=bool(self.settings.pop("hide_chrome", True)))
        self.vars = {k: tk.StringVar(value=str(v)) for k, v in self.settings.items()}
        self.log_q = queue.Queue()
        self.worker = None

        self._build()
        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- layout ----------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # What to download
        dl = ttk.LabelFrame(root, text="What to download", padding=10)
        dl.grid(row=0, column=0, sticky="ew")
        dl.columnconfigure(1, weight=1)

        ttk.Label(dl, text="Link to first issue").grid(row=0, column=0, sticky="nw", **pad)
        link = ttk.Frame(dl)
        link.grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
        link.columnconfigure(0, weight=1)
        ttk.Entry(link, textvariable=self.vars["start_url"]).grid(row=0, column=0, sticky="ew")
        self.link_check = ttk.Label(link)
        self.link_check.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.vars["start_url"].trace_add("write", lambda *_: self._check_link())
        self._check_link()

        ttk.Label(dl, text="Issues").grid(row=1, column=0, sticky="w", **pad)
        issues = ttk.Frame(dl)
        issues.grid(row=1, column=1, sticky="w", **pad)
        ttk.Spinbox(issues, from_=1, to=9999, width=6, textvariable=self.vars["start_issue"]).pack(side="left")
        ttk.Label(issues, text="  to  ").pack(side="left")
        ttk.Spinbox(issues, from_=1, to=9999, width=6, textvariable=self.vars["end_issue"]).pack(side="left")

        ttk.Label(dl, text="Comic name (folder)").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(dl, textvariable=self.vars["folder_name"]).grid(row=2, column=1, columnspan=3, sticky="ew", **pad)

        ttk.Label(dl, text="Group folder (optional)").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(dl, textvariable=self.vars["sub_path"]).grid(row=3, column=1, columnspan=3, sticky="ew", **pad)

        ttk.Label(dl, text="File name prefix (optional)").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(dl, textvariable=self.vars["filename_prefix"]).grid(row=4, column=1, columnspan=3, sticky="ew", **pad)

        ttk.Label(dl, text="Save comics in").grid(row=5, column=0, sticky="w", **pad)
        ttk.Entry(dl, textvariable=self.vars["final_root"]).grid(row=5, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Button(dl, text="Browse...", command=self._pick_root).grid(row=5, column=3, **pad)

        self.preview = ttk.Label(dl, foreground="gray")
        self.preview.grid(row=6, column=0, columnspan=4, sticky="w", **pad)
        for k in ("final_root", "sub_path", "folder_name", "filename_prefix", "start_issue"):
            self.vars[k].trace_add("write", lambda *_: self._update_preview())
        self._update_preview()

        # Buttons
        btns = ttk.Frame(root)
        btns.grid(row=1, column=0, sticky="ew", pady=8)
        self.start_btn = ttk.Button(btns, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Open folder", command=self._open_folder).pack(side="left", padx=6)
        ttk.Button(btns, text="Settings", command=self._open_settings).pack(side="right")
        ttk.Checkbutton(btns, text="Show details", variable=self.verbose,
                        command=self._set_verbose).pack(side="right", padx=10)

        # Log
        logf = ttk.LabelFrame(root, text="Progress", padding=6)
        logf.grid(row=2, column=0, sticky="nsew")
        root.rowconfigure(2, weight=1)
        self.log = tk.Text(logf, height=12, wrap="word", state="disabled",
                           bg="#1e1e1e", fg="#dcdcdc", insertbackground="#dcdcdc",
                           font=("Consolas", 10))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ---------- helpers ----------
    def _check_link(self):
        url = self.vars["start_url"].get().strip()
        if not url:
            self.link_check.config(text="Paste the reader link of the first issue you want",
                                   foreground="gray")
            return
        try:
            series, issue = scraper.parse_reader_url(url)
            self.link_check.config(text=f"Looks good: series {series}, first issue {issue}", foreground="#2e7d32")
        except ValueError:
            self.link_check.config(text="Not a reader link. It should look like "
                                        "https://batcave.biz/reader/9264/60124", foreground="#c62828")

    def _prefix(self):
        p = self.vars["filename_prefix"].get().strip()
        return p or "".join(self.vars["folder_name"].get().split()) or "Comic"

    def _target_dir(self):
        parts = [self.vars["final_root"].get().strip()]
        if self.vars["sub_path"].get().strip():
            parts.append(self.vars["sub_path"].get().strip())
        parts.append(self.vars["folder_name"].get().strip() or "Unnamed")
        return os.path.join(*parts)

    def _update_preview(self):
        try:
            n = int(self.vars["start_issue"].get())
        except ValueError:
            n = 1
        example = os.path.join(self._target_dir(), f"{self._prefix()}_{str(n).zfill(3)}{scraper.ARCHIVE_EXT}")
        self.preview.config(text=f"First file: {example}")

    def _pick_root(self):
        d = filedialog.askdirectory(initialdir=self.vars["final_root"].get() or os.path.expanduser("~"))
        if d:
            self.vars["final_root"].set(os.path.normpath(d))

    def _open_folder(self):
        path = self._target_dir()
        if not os.path.isdir(path):
            path = self.vars["final_root"].get()
        if os.path.isdir(path):
            os.startfile(path)
        else:
            messagebox.showinfo(APP_NAME, "That folder doesn't exist yet. It's created on the first download.")

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_log(self):
        try:
            while True:
                self._log(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
        self.after(100, self._drain_log)

    def _collect(self):
        data = {k: v.get().strip() for k, v in self.vars.items()}
        data["verbose"] = self.verbose.get()
        data["hide_chrome"] = self.hide_chrome.get()
        return data

    def _set_verbose(self):
        scraper.VERBOSE = self.verbose.get()   # takes effect right away, even mid-run
        save_settings(self._collect())

    # ---------- actions ----------
    def _start(self):
        s = self._collect()
        try:
            scraper.parse_reader_url(s["start_url"])
        except ValueError:
            messagebox.showerror(APP_NAME, "Paste the link to the first issue's reader page.\n"
                                 "Example: https://batcave.biz/reader/9264/60124")
            return
        if not s["folder_name"]:
            messagebox.showerror(APP_NAME, "Give the comic a name (used for its folder).")
            return
        try:
            start, end = int(s["start_issue"]), int(s["end_issue"])
        except ValueError:
            messagebox.showerror(APP_NAME, "Issue numbers must be whole numbers.")
            return
        if end < start:
            messagebox.showerror(APP_NAME, "The last issue can't be before the first issue.")
            return

        save_settings(s)

        # Hand the boxes' values to the scraper
        scraper.START_URL = s["start_url"]
        scraper.START_ISSUE = start
        scraper.END_ISSUE = end
        scraper.FINAL_ROOT = os.path.abspath(os.path.expanduser(s["final_root"] or DEFAULTS["final_root"]))
        scraper.SUB_PATH = s["sub_path"]
        scraper.FOLDER_NAME = s["folder_name"]
        scraper.FILENAME_PREFIX = self._prefix()
        scraper.CHROME_PATH = s["chrome_path"]
        scraper.CHROME_PROFILE = s["chrome_profile"]
        scraper.VERBOSE = s["verbose"]
        scraper.HIDE_CHROME = s["hide_chrome"]

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        def work():
            writer = QueueWriter(self.log_q)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    scraper.run()
                except Exception as e:  # show any crash in the log instead of closing
                    print(f"\nError: {e}")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self):
        scraper.STOP_REQUESTED = True
        self._log("\nStopping after the current step...\n")
        self.stop_btn.config(state="disabled")

    def _open_settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.transient(self)
        win.resizable(True, False)
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Browser location").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(f, textvariable=self.vars["chrome_path"], width=60).grid(row=0, column=1, sticky="ew", padx=6)

        def pick_chrome():
            p = filedialog.askopenfilename(title="Find chrome.exe or msedge.exe",
                                           filetypes=[("Browser", "chrome.exe msedge.exe"), ("Programs", "*.exe")])
            if p:
                self.vars["chrome_path"].set(os.path.normpath(p))
        ttk.Button(f, text="Browse...", command=pick_chrome).grid(row=0, column=2, padx=6)

        ttk.Label(f, text="Browser profile folder").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(f, textvariable=self.vars["chrome_profile"], width=60).grid(row=1, column=1, sticky="ew", padx=6)

        ttk.Checkbutton(f, text="Run Chrome hidden (it pops up only if you need to click a check)",
                        variable=self.hide_chrome).grid(row=3, column=1, sticky="w", padx=6, pady=6)

        status = ttk.Label(f)
        status.grid(row=2, column=1, sticky="w", padx=6)

        def refresh(*_):
            typed = self.vars["chrome_path"].get().strip()
            if typed and not os.path.exists(typed):
                status.config(text="Nothing found at that location", foreground="#c62828")
                return
            found = typed or scraper.find_browser()
            if found:
                how = "" if typed else " (found automatically - leave the box empty)"
                status.config(text=f"Using {found}{how}", foreground="#2e7d32")
            else:
                status.config(text="Couldn't find Chrome or Edge - click Browse", foreground="#c62828")
        trace = self.vars["chrome_path"].trace_add("write", refresh)
        win.bind("<Destroy>", lambda e: e.widget is win and self.vars["chrome_path"].trace_remove("write", trace))
        refresh()

        def done():
            save_settings(self._collect())
            win.destroy()
        ttk.Button(f, text="Save", command=done).grid(row=4, column=2, sticky="e", padx=6, pady=(10, 0))

    def _on_close(self):
        save_settings(self._collect())
        scraper.STOP_REQUESTED = True
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
