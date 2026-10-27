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
import readinglist

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
    "rl_url": "",            # reading list: League of Comic Geeks link
    "rl_text": "",           # reading list: pasted text
    "rl_last": "",           # reading list: name of the last list loaded (reopened on start)
    "tab": "0",
}

STATUS_TEXT = {"matched": "Matched", "confirmed": "Confirmed", "check": "Check this",
               "not found": "Not found", "": "-"}


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
        self.geometry("900x780")
        self.minsize(700, 600)

        self.settings = load_settings()
        self.verbose = tk.BooleanVar(value=bool(self.settings.pop("verbose", False)))
        self.hide_chrome = tk.BooleanVar(value=bool(self.settings.pop("hide_chrome", True)))
        self.vars = {k: tk.StringVar(value=str(v)) for k, v in self.settings.items()}
        self.log_q = queue.Queue()
        self.ui_q = queue.Queue()          # work the background thread wants done on screen
        self.worker = None
        self.plan = None

        self._build()
        if self.vars["rl_last"].get():
            saved = readinglist.load_saved_plan(self.vars["rl_last"].get())
            if saved:
                self._show_plan(saved)
        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # =================================================================
    #  layout
    # =================================================================
    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        self.tabs = ttk.Notebook(root)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        root.rowconfigure(0, weight=3)
        single = ttk.Frame(self.tabs, padding=10)
        rlist = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(single, text="Single series")
        self.tabs.add(rlist, text="Reading list")
        try:
            self.tabs.select(int(self.vars["tab"].get()))
        except (ValueError, tk.TclError):
            pass
        self._build_single(single)
        self._build_reading_list(rlist)

        # shared buttons
        btns = ttk.Frame(root)
        btns.grid(row=1, column=0, sticky="ew", pady=8)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Button(btns, text="Open folder", command=self._open_folder).pack(side="left", padx=6)
        ttk.Button(btns, text="Settings", command=self._open_settings).pack(side="right")
        ttk.Checkbutton(btns, text="Show details", variable=self.verbose,
                        command=self._set_verbose).pack(side="right", padx=10)

        # log
        logf = ttk.LabelFrame(root, text="Progress", padding=6)
        logf.grid(row=2, column=0, sticky="nsew")
        root.rowconfigure(2, weight=2)
        self.log = tk.Text(logf, height=9, wrap="word", state="disabled",
                           bg="#1e1e1e", fg="#dcdcdc", insertbackground="#dcdcdc",
                           font=("Consolas", 10))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _save_folder_row(self, parent, row, pad):
        ttk.Label(parent, text="Save comics in").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(parent, textvariable=self.vars["final_root"]).grid(row=row, column=1, columnspan=2,
                                                                      sticky="ew", **pad)
        ttk.Button(parent, text="Browse...", command=self._pick_root).grid(row=row, column=3, **pad)

    def _build_single(self, dl):
        pad = {"padx": 8, "pady": 4}
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

        self._save_folder_row(dl, 5, pad)

        self.preview = ttk.Label(dl, foreground="gray")
        self.preview.grid(row=6, column=0, columnspan=4, sticky="w", **pad)
        for k in ("final_root", "sub_path", "folder_name", "filename_prefix", "start_issue"):
            self.vars[k].trace_add("write", lambda *_: self._update_preview())
        self._update_preview()

        self.start_btn = ttk.Button(dl, text="Start", command=self._start)
        self.start_btn.grid(row=7, column=0, sticky="w", padx=8, pady=(10, 0))

    def _build_reading_list(self, f):
        pad = {"padx": 8, "pady": 4}
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="League of Comic Geeks list").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.vars["rl_url"]).grid(row=0, column=1, columnspan=3, sticky="ew", **pad)

        self.rl_text_shown = tk.BooleanVar(value=bool(self.vars["rl_text"].get().strip()))
        toggle = ttk.Checkbutton(f, text="Or paste a list instead (lines like: Superman #1-176, #0, Annual #1-12)",
                                 variable=self.rl_text_shown, command=self._toggle_rl_text)
        toggle.grid(row=1, column=1, columnspan=3, sticky="w", **pad)
        self.rl_text = tk.Text(f, height=5, wrap="word", font=("Consolas", 9))
        self.rl_text.insert("1.0", self.vars["rl_text"].get())
        self.rl_text_row = 2
        self._toggle_rl_text()

        self._save_folder_row(f, 3, pad)

        btns = ttk.Frame(f)
        btns.grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 4))
        self.rl_load_btn = ttk.Button(btns, text="1. Load list", command=self._rl_load)
        self.rl_load_btn.pack(side="left")
        self.rl_find_btn = ttk.Button(btns, text="2. Find on Batcave", command=self._rl_find, state="disabled")
        self.rl_find_btn.pack(side="left", padx=6)
        self.rl_dl_btn = ttk.Button(btns, text="3. Download all", command=self._rl_download, state="disabled")
        self.rl_dl_btn.pack(side="left")

        self.rl_title = ttk.Label(f, text="No list loaded yet.", font=("Segoe UI", 10, "bold"))
        self.rl_title.grid(row=5, column=0, columnspan=4, sticky="w", padx=8, pady=(6, 0))

        tablef = ttk.Frame(f)
        tablef.grid(row=6, column=0, columnspan=4, sticky="nsew", padx=8, pady=4)
        f.rowconfigure(6, weight=1)
        cols = ("get", "series", "issues", "found", "batcave", "status")
        self.tree = ttk.Treeview(tablef, columns=cols, show="headings", height=9, selectmode="browse")
        for c, label, w, anchor in (("get", "Get", 45, "center"), ("series", "Series", 260, "w"),
                                    ("issues", "Issues", 55, "center"), ("found", "Found", 70, "center"),
                                    ("batcave", "Batcave match", 260, "w"),
                                    ("status", "Status", 90, "w")):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor=anchor, stretch=c in ("series", "batcave"))
        self.tree.tag_configure("check", foreground="#b26a00")
        self.tree.tag_configure("missing", foreground="#c62828")
        self.tree.tag_configure("off", foreground="gray")
        tsb = ttk.Scrollbar(tablef, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Double-1>", self._tree_double)

        self.rl_summary = ttk.Label(f, foreground="gray",
                                    text="Click Get to include or skip a series. Double-click a row to change its Batcave match.")
        self.rl_summary.grid(row=7, column=0, columnspan=4, sticky="w", padx=8)

    def _toggle_rl_text(self):
        if self.rl_text_shown.get():
            self.rl_text.grid(row=self.rl_text_row, column=1, columnspan=3, sticky="ew", padx=8, pady=4)
        else:
            self.rl_text.grid_remove()

    # =================================================================
    #  helpers
    # =================================================================
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

    def _save_dir(self):
        return os.path.abspath(os.path.expanduser(self.vars["final_root"].get().strip() or DEFAULTS["final_root"]))

    def _target_dir(self):
        if self.tabs.index("current") == 1:
            if self.plan:
                return os.path.join(self._save_dir(), readinglist.safe_filename(self.plan["name"]))
            return self._save_dir()
        return self._single_dir()

    def _single_dir(self):
        parts = [self._save_dir()]
        if self.vars["sub_path"].get().strip():
            parts.append(self.vars["sub_path"].get().strip())
        parts.append(self.vars["folder_name"].get().strip() or "Unnamed")
        return os.path.join(*parts)

    def _update_preview(self):
        try:
            n = int(self.vars["start_issue"].get())
        except ValueError:
            n = 1
        example = os.path.join(self._single_dir(), f"{self._prefix()}_{str(n).zfill(3)}{scraper.ARCHIVE_EXT}")
        self.preview.config(text=f"First file: {example}")

    def _pick_root(self):
        d = filedialog.askdirectory(initialdir=self.vars["final_root"].get() or os.path.expanduser("~"))
        if d:
            self.vars["final_root"].set(os.path.normpath(d))

    def _open_folder(self):
        path = self._target_dir()
        if not os.path.isdir(path):
            path = self._save_dir()
        if os.path.isdir(path):
            os.startfile(path)
        else:
            messagebox.showinfo(APP_NAME, "That folder doesn't exist yet. It's created on the first download.")

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _drain_log(self):
        try:
            while True:
                self._log(self.log_q.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self._set_busy(False)
        self.after(100, self._drain_log)

    def _collect(self):
        data = {k: v.get().strip() for k, v in self.vars.items()}
        data["rl_text"] = self.rl_text.get("1.0", "end").strip()
        data["tab"] = str(self.tabs.index("current"))
        data["verbose"] = self.verbose.get()
        data["hide_chrome"] = self.hide_chrome.get()
        return data

    def _set_verbose(self):
        scraper.VERBOSE = self.verbose.get()   # takes effect right away, even mid-run
        save_settings(self._collect())

    def _apply_browser_settings(self, s):
        scraper.CHROME_PATH = s["chrome_path"]
        scraper.CHROME_PROFILE = s["chrome_profile"]
        scraper.VERBOSE = s["verbose"]
        scraper.HIDE_CHROME = s["hide_chrome"]

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.start_btn.config(state=state)
        self.rl_load_btn.config(state=state)
        has_plan = self.plan is not None
        self.rl_find_btn.config(state="normal" if (has_plan and not busy) else "disabled")
        self.rl_dl_btn.config(state="normal" if (has_plan and not busy) else "disabled")
        self.stop_btn.config(state="normal" if busy else "disabled")

    def _run(self, job):
        """Run job() in the background with its output going to the Progress box."""
        if self.worker:
            messagebox.showinfo(APP_NAME, "Wait for the current job to finish, or press Stop.")
            return
        s = self._collect()
        save_settings(s)
        self._apply_browser_settings(s)
        scraper.STOP_REQUESTED = False
        self._clear_log()
        self._set_busy(True)

        def work():
            writer = QueueWriter(self.log_q)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    job()
                except scraper.BrowserError as e:
                    print(str(e))
                except ValueError as e:      # bad link or list text: show it plainly
                    print(f"\n{e}")
                except Exception as e:      # show any crash in the log instead of closing
                    print(f"\nError: {e}")

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def on_ui(self, fn):
        """Called from the background thread: run fn on the window's thread."""
        self.ui_q.put(fn)

    # =================================================================
    #  single series
    # =================================================================
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

        # Hand the boxes' values to the scraper
        scraper.START_URL = s["start_url"]
        scraper.START_ISSUE = start
        scraper.END_ISSUE = end
        scraper.FINAL_ROOT = self._save_dir()
        scraper.SUB_PATH = s["sub_path"]
        scraper.FOLDER_NAME = s["folder_name"]
        scraper.FILENAME_PREFIX = self._prefix()
        self._run(scraper.run)

    # =================================================================
    #  reading list
    # =================================================================
    def _show_plan(self, plan):
        self.plan = plan
        self.vars["rl_last"].set(plan["name"])
        self.rl_title.config(text=f"{plan['name']}  ({len(plan['items'])} issues, {len(plan['groups'])} series)")
        self.tree.delete(*self.tree.get_children())
        for key in plan["groups"]:
            self.tree.insert("", "end", iid=key)
            self._refresh_row(key)
        self._refresh_summary()
        self._set_busy(bool(self.worker))

    def _refresh_row(self, key):
        g = self.plan["groups"][key]
        status = g.get("status") or ""
        match = g["match"]["title"] if g.get("match") else ""
        found = f"{g['found'][0]}/{g['found'][1]}" if g.get("found") else ""
        partial = bool(g.get("found")) and g["found"][0] < g["found"][1]
        tags = ()
        if not g["include"]:
            tags = ("off",)
        elif status == "not found":
            tags = ("missing",)
        elif status == "check" or partial:
            tags = ("check",)
        self.tree.item(key, values=("Yes" if g["include"] else "No", g["name"], g["count"], found, match,
                                    STATUS_TEXT.get(status, status) if g["include"] else "Skipped"), tags=tags)

    def _refresh_summary(self):
        if not self.plan:
            return
        gs = [g for g in self.plan["groups"].values() if g["include"]]
        issues = sum(g["count"] for g in gs)
        ok = sum(1 for g in gs if g.get("status") in ("matched", "confirmed"))
        check = sum(1 for g in gs if g.get("status") == "check")
        missing = sum(1 for g in gs if g.get("status") == "not found")
        have = sum(g["found"][0] for g in gs if g.get("found"))
        checked = sum(1 for g in gs if g.get("found"))
        todo = len(gs) - ok - check - missing
        parts = [f"{issues} issues in {len(gs)} series", f"{ok} matched"]
        if checked:
            parts.insert(1, f"{have} issues found on Batcave")
        if check:
            parts.append(f"{check} to check (orange)")
        if missing:
            parts.append(f"{missing} not found (red)")
        if todo:
            parts.append(f"{todo} not searched yet")
        self.rl_summary.config(text=" - ".join(parts) + ".   Double-click a row to change its match.")

    def _rl_changed(self, key=None):
        if key:
            self._refresh_row(key)
        self._refresh_summary()
        readinglist.save_plan(self.plan)

    def _tree_click(self, event):
        if not self.plan or self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        key = self.tree.identify_row(event.y)
        if key:
            g = self.plan["groups"][key]
            g["include"] = not g["include"]
            self._rl_changed(key)
            return "break"

    def _tree_double(self, event):
        key = self.tree.identify_row(event.y)
        if key and self.plan and self.tree.identify_column(event.x) != "#1":
            MatchDialog(self, key)

    def _rl_load(self):
        url = self.vars["rl_url"].get().strip()
        text = self.rl_text.get("1.0", "end").strip() if self.rl_text_shown.get() else ""
        if not url and not text:
            messagebox.showerror(APP_NAME, "Paste a League of Comic Geeks list link, or tick "
                                           "'Or paste a list instead' and paste the list.")
            return

        def job():
            with scraper.browser_session() as (page, context, ua):
                if url:
                    plan = readinglist.load_from_link(page, url)
                else:
                    first = text.strip().splitlines()[0].strip()
                    name = first if "#" not in first and len(first) < 80 else "Reading List"
                    plan = readinglist.load_from_text(page, text, name)
            readinglist.save_plan(plan)
            self.on_ui(lambda: self._show_plan(plan))
            print("\nNext: click '2. Find on Batcave'.")
        self._run(job)

    def _rl_find(self, redo=False):
        plan = self.plan

        def job():
            with scraper.browser_session() as (page, context, ua):
                readinglist.find_on_batcave(page, plan, on_update=lambda g: self.on_ui(
                    lambda k=g["name"]: self._rl_changed(k)), redo=redo)
            self.on_ui(self._refresh_summary)
            print("\nCheck any orange or red rows (double-click to fix), then click '3. Download all'.")
        self._run(job)

    def _rl_download(self):
        plan = self.plan
        gs = [g for g in plan["groups"].values() if g["include"]]
        unmatched = [g["name"] for g in gs if not g.get("match")]
        if unmatched:
            names = "\n".join(unmatched[:12]) + ("\n..." if len(unmatched) > 12 else "")
            if not messagebox.askyesno(APP_NAME, f"{len(unmatched)} series have no Batcave match and will be "
                                                 f"skipped:\n\n{names}\n\nDownload the rest anyway?"):
                return
        root = self._save_dir()

        def job():
            with scraper.browser_session() as (page, context, ua):
                readinglist.download(page, context, ua, plan, root)
        self._run(job)

    # =================================================================
    #  shared
    # =================================================================
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
        if self.plan:
            readinglist.save_plan(self.plan)
        scraper.STOP_REQUESTED = True
        self.destroy()


class MatchDialog(tk.Toplevel):
    """Pick, search for, or paste the Batcave series for one row of the reading list."""

    def __init__(self, app, key):
        super().__init__(app)
        self.app, self.key = app, key
        self.g = app.plan["groups"][key]
        self.title(f"Batcave match: {self.g['name']}")
        self.transient(app)
        self.geometry("620x440")
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)

        year = f" (series started {self.g['year']})" if self.g.get("year") else ""
        ttk.Label(f, text=f"{self.g['name']}{year} - {self.g['count']} issues in the list",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")

        if self.g.get("found"):
            have, need = self.g["found"]
            note = f"The chosen Batcave page has {have} of these {need} issues."
            if self.g.get("missing"):
                shown = ", ".join(self.g["missing"][:8]) + (" ..." if len(self.g["missing"]) > 8 else "")
                note += f" Missing: {shown}"
            ttk.Label(f, text=note, foreground="#b26a00" if have < need else "#2e7d32",
                      wraplength=580).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(f, text="Pick the right series:").grid(row=1, column=0, sticky="w", pady=(10, 2))
        self.box = tk.Listbox(f, height=9, activestyle="none")
        self.box.grid(row=2, column=0, columnspan=2, sticky="nsew")
        f.rowconfigure(2, weight=1)
        self.box.bind("<Double-1>", lambda e: self._use_selected())

        search = ttk.Frame(f)
        search.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="Search Batcave for").grid(row=0, column=0, padx=(0, 6))
        self.query = tk.StringVar(value=self.g["name"])
        ttk.Entry(search, textvariable=self.query).grid(row=0, column=1, sticky="ew")
        self.search_btn = ttk.Button(search, text="Search", command=self._search)
        self.search_btn.grid(row=0, column=2, padx=(6, 0))

        paste = ttk.Frame(f)
        paste.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        paste.columnconfigure(1, weight=1)
        ttk.Label(paste, text="Or paste its Batcave page").grid(row=0, column=0, padx=(0, 6))
        self.link = tk.StringVar()
        ttk.Entry(paste, textvariable=self.link).grid(row=0, column=1, sticky="ew")

        btns = ttk.Frame(f)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Skip this series", command=self._skip).pack(side="left")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="Use this", command=self._use_selected).pack(side="left")
        self._fill()

    def _fill(self):
        self.box.delete(0, "end")
        current = (self.g.get("match") or {}).get("url")
        for i, c in enumerate(self.g.get("candidates") or []):
            mark = "   (current)" if c["url"] == current else ""
            self.box.insert("end", f"{c['title']}{mark}")
            if c["url"] == current:
                self.box.selection_set(i)
        if not self.g.get("candidates"):
            self.box.insert("end", "(no results yet - search below or paste a link)")

    def _search(self):
        q = self.query.get().strip()
        if not q:
            return
        g = self.g

        def job():
            print(f"Searching Batcave for '{q}'...")
            with scraper.browser_session() as (page, context, ua):
                found = readinglist.batcave_search(page, q, True)
            for c in found:
                c["score"] = readinglist.score_candidate(g, c)
            found.sort(key=lambda c: -c["score"])
            print(f"   {len(found)} results")

            def show():
                g["candidates"] = found[:15]
                if self.winfo_exists():
                    self._fill()
            self.app.on_ui(show)
        self.app._run(job)

    def _use_selected(self):
        link = self.link.get().strip()
        if link:
            if "/reader/" in link:
                messagebox.showerror(APP_NAME, "That's a reader link for one issue. Paste the series page "
                                               "instead (the page that lists all its issues).", parent=self)
                return
            if not link.startswith("http"):
                messagebox.showerror(APP_NAME, "Paste the full link, starting with https://", parent=self)
                return
            self.g["match"] = {"title": link, "name": "", "year": None, "url": link}
        else:
            sel = self.box.curselection()
            cands = self.g.get("candidates") or []
            if not sel or sel[0] >= len(cands):
                messagebox.showinfo(APP_NAME, "Pick a series from the list, or paste a link.", parent=self)
                return
            c = cands[sel[0]]
            self.g["match"] = {k: c[k] for k in ("title", "name", "year", "url")}
        self.g["status"] = "confirmed"
        self.g["include"] = True
        self.g.pop("found", None)          # re-counted on the next "Find on Batcave"
        self.app._rl_changed(self.key)
        self.destroy()

    def _skip(self):
        self.g["include"] = False
        self.app._rl_changed(self.key)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
