"""Runs the Comic Scraper engine inside the Android app.

The Windows app drives Chrome through Playwright. On Android there's no Chrome to drive, so this
file gives scraper.py / readinglist.py / weebcentral.py a small "page" object with the same methods
they use (goto, evaluate, wait_for_function, ...) that works on the app's built-in WebView.
The Kotlin side (WebBridge.kt) does the WebView work; everything else is the same Python code
the Windows app runs.

The Kotlin app calls the functions at the bottom (init, run_single, rl_load, rl_find, ...)
from a background thread, and reads the reading-list table with rl_table().
"""
import contextlib
import json
import os
import re
import sys
import threading
import time
import uuid

import requests

import scraper as S
import readinglist as RL
import mangadex
import weebcentral

WEB = None          # the Kotlin WebBridge
_UI = None          # Kotlin callback: _UI.event(name) tells the screen to refresh something

STATUS_TEXT = {"matched": "Matched", "confirmed": "Confirmed", "check": "Check this",
               "not found": "Not found", "upcoming": "Not out yet", "": "-"}

# Playwright accepts either a JS expression or a function; this tells them apart the same way.
FUNCTION_RE = re.compile(r"^(async\s+)?(function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")


class JSError(Exception):
    """JavaScript on the page threw an error."""


# ====================================================================== the page
class Keyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.evaluate("""(key) => {
            const codes = {ArrowRight: 39, ArrowLeft: 37, Enter: 13, Escape: 27, PageDown: 34};
            const init = {key, code: key, keyCode: codes[key] || 0, which: codes[key] || 0, bubbles: true};
            const target = document.activeElement || document.body || document;
            for (const t of [target, document, window]) {
                t.dispatchEvent(new KeyboardEvent('keydown', init));
                t.dispatchEvent(new KeyboardEvent('keyup', init));
            }
        }""", key)


class Mouse:
    def __init__(self, page):
        self.page = page

    def click(self, x, y):
        self.page.evaluate("""([x, y]) => {
            const el = document.elementFromPoint(x, y);
            if (!el) return false;
            const init = {bubbles: true, cancelable: true, clientX: x, clientY: y, view: window};
            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup'])
                el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, init));
            el.click();
            return true;
        }""", [x, y])


class Response:
    def __init__(self, r):
        self._r = r
        self.status = r.status_code
        self.ok = 200 <= r.status_code < 300

    def body(self):
        return self._r.content


class Request:
    """context.request.get(): a plain download that carries the WebView's cookies."""

    def __init__(self, context):
        self.context = context

    def get(self, url, headers=None, timeout=15000):
        return Response(self.context.http().get(url, headers=headers or {}, timeout=timeout / 1000))


class Context:
    COOKIE_SITES = ("https://batcave.biz", "https://leagueofcomicgeeks.com", "https://weebcentral.com",
                    "https://mangadex.org")

    def __init__(self, page):
        self.page = page
        self.request = Request(self)

    def cookies(self):
        out = []
        for site in self.COOKIE_SITES + (self.page.url,):
            if not site.startswith("http"):
                continue
            host = re.sub(r"^https?://([^/]+).*$", r"\1", site)
            domain = "." + re.sub(r"^www\.", "", host)
            for part in (WEB.cookies(site) or "").split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name and not any(c["name"] == name and c["domain"] == domain for c in out):
                    out.append({"name": name, "value": value, "domain": domain, "path": "/"})
        return out

    def http(self):
        s = requests.Session()
        s.headers["User-Agent"] = self.page.user_agent
        for c in self.cookies():
            s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        return s


class Page:
    """The Playwright Page methods the scraper uses, done on the Android WebView."""

    def __init__(self):
        self.keyboard = Keyboard(self)
        self.mouse = Mouse(self)
        self.context = Context(self)
        self.user_agent = str(WEB.userAgent())

    # ---- JavaScript
    def evaluate(self, js, arg=None, timeout=60000):
        src = js.strip()
        if FUNCTION_RE.match(src):
            expr = f"({src})({json.dumps(arg)})"
        else:
            expr = f"({src})"
        raw = str(WEB.eval(expr, int(timeout)))
        data = json.loads(raw)
        if data.get("timeout"):
            raise S.PWTimeout("the page didn't answer in time (it may have been loading)")
        if not data.get("ok"):
            raise JSError(data.get("e") or "JavaScript error")
        return data.get("v")

    def _poll(self, check, timeout):
        end = time.time() + timeout / 1000
        while True:
            try:
                if check():
                    return True
            except (JSError, S.PWTimeout):
                pass                                # the page is still changing
            if time.time() >= end:
                raise S.PWTimeout(f"timed out after {timeout / 1000:.0f}s")
            if S.STOP_REQUESTED:
                raise S.PWTimeout("stopped")
            time.sleep(0.25)

    def wait_for_function(self, js, timeout=30000, **_):
        self._poll(lambda: bool(self.evaluate(js, timeout=5000)), timeout)

    def wait_for_selector(self, selector, timeout=30000, **_):
        js = f"!!document.querySelector({json.dumps(selector)})"
        self._poll(lambda: bool(self.evaluate(js, timeout=5000)), timeout)

    # ---- navigation
    def goto(self, url, wait_until=None, timeout=60000, **_):
        tag = uuid.uuid4().hex
        same_doc = False
        try:
            here = self.evaluate("location.href", timeout=3000) or ""
            same_doc = here.split("#")[0] == url.split("#")[0]
            self.evaluate(f"window.__csTag = '{tag}'", timeout=3000)
        except (JSError, S.PWTimeout):
            pass
        WEB.load(url)
        if same_doc:                                # only the #part changed: no new page loads
            time.sleep(1.0)
            return
        def loaded():
            info = self.evaluate("[window.__csTag || '', document.readyState, location.href]", timeout=3000)
            return info[0] != tag and info[1] != "loading" and info[2] != "about:blank"
        self._poll(loaded, timeout)

    @property
    def url(self):
        try:
            return self.evaluate("location.href", timeout=3000) or ""
        except (JSError, S.PWTimeout):
            return ""

    def title(self):
        return self.evaluate("document.title", timeout=5000) or ""

    def content(self):
        return self.evaluate("document.documentElement.outerHTML", timeout=15000) or ""

    @property
    def viewport_size(self):
        try:
            w, h = self.evaluate("[window.innerWidth, window.innerHeight]", timeout=3000)
            return {"width": w, "height": h}
        except (JSError, S.PWTimeout, TypeError, ValueError):
            return {"width": 400, "height": 800}

    # ---- window
    def set_visible(self, visible):
        WEB.setVisible(bool(visible))

    def bring_to_front(self):
        pass

    def screenshot(self, path=None, **_):
        pass                                        # the page's HTML is saved instead

    def close(self):
        pass


_SESSION_LOCK = threading.Lock()


@contextlib.contextmanager
def session():
    """What scraper.browser_session() yields on Android: (page, context, user_agent)."""
    with _SESSION_LOCK:                             # one job uses the WebView at a time
        page = Page()
        try:
            yield page, page.context, page.user_agent
        finally:
            page.set_visible(False)


# ====================================================================== app plumbing
class LogWriter:
    def __init__(self, log):
        self.log = log

    def write(self, text):
        if text:
            self.log.write(str(text))
        return len(text or "")

    def flush(self):
        pass


def init(web, ui, log, files_dir):
    """Called once when the app starts."""
    global WEB, _UI
    WEB, _UI = web, ui
    S.ANDROID_BROWSER = session
    S.USE_MY_CHROME = True         # "our own browser": wait_for_reader shows it for human checks
    S.HIDE_CHROME = True
    sys.stdout = sys.stderr = LogWriter(log)


def _event(name):
    try:
        _UI.event(name)
    except Exception:
        pass


def _apply(settings):
    S.VERBOSE = bool(settings.get("verbose"))
    try:
        S.FILES_PER_FOLDER = max(0, int(settings.get("files_per_folder") or 0))
    except ValueError:
        S.FILES_PER_FOLDER = 100
    S.STOP_REQUESTED = False


def _guard(job):
    """Run a job, printing any problem to the log instead of crashing the app."""
    try:
        job()
    except S.BrowserError as e:
        print(str(e))
    except ValueError as e:
        print(f"\n{e}")
    except Exception as e:
        print(f"\nError: {e}")
    finally:
        _event("done")


def stop():
    S.STOP_REQUESTED = True
    print("\nStopping after the current step...")


def set_verbose(on):
    S.VERBOSE = bool(on)


# ---------------------------------------------------------------- single series
def check_link(url):
    """Text for the line under the link box: (ok, message)."""
    url = (url or "").strip()
    if not url:
        return json.dumps([True, ""])
    try:
        if weebcentral.is_weebcentral(url):
            kind, _ = weebcentral.parse_link(url)
            return json.dumps([True, "WeebCentral " + ("chapter link (gets its whole series)" if kind == "chapter"
                                                      else "series page") + ". Numbers are chapter numbers."])
        if mangadex.is_mangadex(url):
            kind, _ = mangadex.parse_link(url)
            return json.dumps([True, "MangaDex " + ("chapter link" if kind == "chapter" else "title page")
                               + ". Numbers are chapter numbers."])
        kind, series, issue = S.parse_batcave_link(url)
        if kind == "series":
            return json.dumps([True, "Batcave series page. Numbers are the real issue numbers."])
        return json.dumps([True, "Batcave reader link: starts from this issue."])
    except ValueError:
        return json.dumps([False, "Paste a Batcave series page or reader link, a MangaDex title, "
                                  "or a WeebCentral series page."])


def run_single(settings_json):
    """Download one series. Returns '' when it started, or a message saying what's wrong."""
    s = json.loads(settings_json)
    ok, msg = json.loads(check_link(s.get("start_url", "")))
    if not s.get("start_url") or not ok:
        return msg or "Paste a link first."
    if not s.get("folder_name", "").strip():
        return "Give the comic a name (used for its folder)."
    try:
        start, end = int(s.get("start_issue") or 1), int(s.get("end_issue") or 1)
    except ValueError:
        if not s.get("all_issues"):
            return "Issue numbers must be whole numbers."
        start = end = 1
    if end < start and not s.get("all_issues"):
        return "The last issue can't be before the first issue."
    _apply(s)
    S.START_URL = s["start_url"].strip()
    S.START_ISSUE, S.END_ISSUE = start, end
    S.FINAL_ROOT = s["final_root"]
    S.SUB_PATH = s.get("sub_path", "").strip()
    S.FOLDER_NAME = s["folder_name"].strip()
    S.FILENAME_PREFIX = s.get("filename_prefix", "").strip() or "".join(S.FOLDER_NAME.split()) or "Comic"
    S.ALL_ISSUES = bool(s.get("all_issues"))
    threading.Thread(target=_guard, args=(S.run,), daemon=True).start()
    return ""


# ---------------------------------------------------------------- reading list
PLAN = None


def _row(key, g):
    status = g.get("status") or ""
    found = f"{g['found'][0]}/{g['found'][1]}" if g.get("found") and g["found"][1] else ""
    if g.get("upcoming"):
        found = (found + " " if found else "") + f"+{g['upcoming']} soon"
    partial = bool(g.get("found")) and g["found"][0] < g["found"][1]
    if not g["include"]:
        color = "off"
    elif status == "upcoming":
        color = "soon"
    elif status == "not found":
        color = "missing"
    elif status == "check" or partial:
        color = "check"
    else:
        color = ""
    return {"key": key, "include": bool(g["include"]), "name": g["name"], "count": g["count"],
            "found": found, "match": g["match"]["title"] if g.get("match") else "",
            "status": STATUS_TEXT.get(status, status) if g["include"] else "Skipped", "color": color}


def _summary():
    gs = [g for g in PLAN["groups"].values() if g["include"]]
    issues = sum(g["count"] for g in gs)
    ok = sum(1 for g in gs if g.get("status") in ("matched", "confirmed"))
    check = sum(1 for g in gs if g.get("status") == "check")
    missing = sum(1 for g in gs if g.get("status") == "not found")
    soon = sum(g.get("upcoming") or 0 for g in gs)
    waiting = sum(1 for g in gs if g.get("status") == "upcoming")
    have = sum(g["found"][0] for g in gs if g.get("found"))
    checked = sum(1 for g in gs if g.get("found"))
    todo = len(gs) - ok - check - missing - waiting
    parts = [f"{issues} issues in {len(gs)} series", f"{ok} matched"]
    if checked:
        parts.insert(1, f"{have} found on Batcave")
    if check:
        parts.append(f"{check} to check (orange)")
    if missing:
        parts.append(f"{missing} not found (red)")
    if soon:
        parts.append(f"{soon} not out yet (blue)")
    if todo:
        parts.append(f"{todo} not searched yet")
    return " - ".join(parts) + ". Tap a row to change its match."


def rl_table():
    """The reading-list table as JSON for the screen."""
    if not PLAN:
        return json.dumps({"name": "", "title": "", "summary": "", "rows": [], "has_plan": False})
    for _ in range(5):                  # the background job may be changing the plan right now
        try:
            rows = [_row(k, g) for k, g in list(PLAN["groups"].items())]
            return json.dumps({"name": PLAN["name"],
                               "title": f"{PLAN['name']}  ({len(PLAN['items'])} issues, "
                                        f"{len(PLAN['groups'])} series)",
                               "summary": _summary(), "rows": rows, "has_plan": True})
        except (RuntimeError, KeyError, TypeError):
            time.sleep(0.05)
    return json.dumps({"name": PLAN.get("name", ""), "title": PLAN.get("name", ""), "summary": "",
                       "rows": [], "has_plan": True})


def rl_saved_names():
    try:
        names = [f[:-5] for f in os.listdir(RL.PLANS_DIR) if f.endswith(".json")]
        names.sort(key=lambda n: -os.path.getmtime(os.path.join(RL.PLANS_DIR, n + ".json")))
        return json.dumps(names)
    except OSError:
        return "[]"


def rl_open(name):
    global PLAN
    plan = RL.load_saved_plan(name)
    if plan:
        PLAN = plan
    _event("table")
    return bool(plan)


def rl_load(settings_json, url, text):
    s = json.loads(settings_json)
    url, text = (url or "").strip(), (text or "").strip()
    if not url and not text:
        return "Paste a League of Comic Geeks list link, or paste a list."
    _apply(s)

    def job():
        global PLAN
        with S.browser_session() as (page, context, ua):
            if url:
                plan = RL.load_from_link(page, url)
            else:
                first = text.splitlines()[0].strip()
                name = first if "#" not in first and len(first) < 80 else "Reading List"
                plan = RL.load_from_text(page, text, name)
        RL.save_plan(plan)
        PLAN = plan
        _event("table")
        print("\nNext: tap '2. Find on Batcave'.")
    threading.Thread(target=_guard, args=(job,), daemon=True).start()
    return ""


def rl_find(settings_json, redo=False):
    if not PLAN:
        return "Load a list first."
    _apply(json.loads(settings_json))
    plan = PLAN

    def job():
        with S.browser_session() as (page, context, ua):
            RL.find_on_batcave(page, plan, on_update=lambda g: _event("table"), redo=bool(redo))
        _event("table")
        print("\nCheck any orange or red rows (tap to fix), then tap '3. Download all'.")
    threading.Thread(target=_guard, args=(job,), daemon=True).start()
    return ""


def rl_unmatched():
    if not PLAN:
        return "[]"
    return json.dumps([g["name"] for g in PLAN["groups"].values() if g["include"] and not g.get("match")])


def rl_folder(root):
    if not PLAN:
        return root
    return os.path.join(root, RL.safe_filename(PLAN["name"]))


def rl_download(settings_json):
    if not PLAN:
        return "Load a list first."
    s = json.loads(settings_json)
    _apply(s)
    plan, root = PLAN, s["final_root"]

    def job():
        with S.browser_session() as (page, context, ua):
            RL.download(page, context, ua, plan, root)
    threading.Thread(target=_guard, args=(job,), daemon=True).start()
    return ""


def _changed():
    RL.save_plan(PLAN)
    _event("table")


def rl_toggle(key):
    g = PLAN["groups"].get(key)
    if g:
        g["include"] = not g["include"]
        _changed()


def rl_details(key):
    """Everything the match screen shows for one row."""
    g = PLAN["groups"].get(key) if PLAN else None
    if not g:
        return "{}"
    year = f" (series started {g['year']})" if g.get("year") else ""
    note = ""
    if g.get("found") and g["found"][1]:
        have, need = g["found"]
        note = f"The chosen Batcave page has {have} of the {need} issues that are out."
        if g.get("upcoming"):
            note += f" {g['upcoming']} more aren't released yet."
        if g.get("missing"):
            note += " Missing: " + ", ".join(g["missing"][:8]) + (" ..." if len(g["missing"]) > 8 else "")
    current = (g.get("match") or {}).get("url")
    return json.dumps({"heading": f"{g['name']}{year} - {g['count']} issues in the list",
                       "note": note, "query": g["name"],
                       "candidates": [{"title": c["title"], "current": c["url"] == current}
                                      for c in g.get("candidates") or []]})


def rl_search(settings_json, key, query):
    g = PLAN["groups"].get(key) if PLAN else None
    query = (query or "").strip()
    if not g or not query:
        return "Type something to search for."
    _apply(json.loads(settings_json))

    def job():
        print(f"Searching Batcave for '{query}'...")
        with S.browser_session() as (page, context, ua):
            found = RL.batcave_search(page, query, True)
        for c in found:
            c["score"] = RL.score_candidate(g, c)
        found.sort(key=lambda c: -c["score"])
        print(f"   {len(found)} results")
        g["candidates"] = found[:15]
        _event("candidates")
    threading.Thread(target=_guard, args=(job,), daemon=True).start()
    return ""


def rl_use(key, index, link):
    """Use candidate number `index`, or a pasted Batcave series link. Returns '' or a problem."""
    g = PLAN["groups"].get(key) if PLAN else None
    if not g:
        return "That row is gone. Reload the list."
    link = (link or "").strip()
    if link:
        if "/reader/" in link:
            return ("That's a reader link for one issue. Paste the series page instead "
                    "(the page that lists all its issues).")
        if not link.startswith("http"):
            return "Paste the full link, starting with https://"
        g["match"] = {"title": link, "name": "", "year": None, "url": link}
    else:
        cands = g.get("candidates") or []
        if index is None or index < 0 or index >= len(cands):
            return "Pick a series from the list, or paste a link."
        c = cands[index]
        g["match"] = {k: c[k] for k in ("title", "name", "year", "url")}
    g["status"] = "confirmed"
    g["include"] = True
    g.pop("found", None)                # re-counted on the next "Find on Batcave"
    g.pop("extra", None)
    _changed()
    return ""


def rl_skip(key):
    g = PLAN["groups"].get(key) if PLAN else None
    if g:
        g["include"] = False
        _changed()


# ---------------------------------------------------------------- search
RESULTS = []


def search_start(settings_json, query, sites_json):
    """Search the chosen sites in the background. The screen gets a "results" event when done."""
    import search
    query = (query or "").strip()
    sites = json.loads(sites_json or "[]")
    if not query:
        return "Type a name to search for."
    if not sites:
        return "Tick at least one site to search."
    _apply(json.loads(settings_json))

    def job():
        global RESULTS
        print(f"Searching for '{query}'...")
        if "Batcave" in sites:                          # Batcave's search needs the browser
            with S.browser_session() as (page, context, ua):
                results, problems = search.search_all(query, sites, page)
        else:
            results, problems = search.search_all(query, sites)
        search.print_summary(query, results, problems)
        RESULTS = results
        _event("results")
    threading.Thread(target=_guard, args=(job,), daemon=True).start()
    return ""


def search_results():
    return json.dumps(RESULTS)


def search_pick(index):
    """The link and folder name to put in the Single series tab for result number `index`."""
    if index < 0 or index >= len(RESULTS):
        return "{}"
    r = RESULTS[index]
    print(f"\nPicked {r['title']} ({r['source']}). Set the issue numbers (or tick 'Get every issue'), "
          "then tap Start.")
    return json.dumps({"url": r["url"], "name": RL.safe_filename(r["title"])})


# ---------------------------------------------------------------- updates
def update_check(current):
    """JSON: {"version", "notes", "apk", "page"} when a newer release exists, {} when up to date,
    or {"error": "..."} when GitHub couldn't be reached."""
    import updater
    try:
        rel = updater.check(current)
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps(rel or {})
