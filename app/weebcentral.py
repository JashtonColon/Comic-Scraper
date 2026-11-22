"""Downloads manga from WeebCentral (weebcentral.com) into .cbz files.

WeebCentral's pages are plain HTML, so normally no browser is needed:
  - /series/<id>/full-chapter-list          every chapter link, newest first
  - /chapters/<id>/images?...long_strip     every page image of one chapter
If the site ever answers with a "verify you are human" check, the same pages are read through the
scraper's Chrome instead (and its cookies are used for the images).

The scraper's settings are used the same way as for Batcave; the numbers are chapter numbers.
"""
import html as htmllib
import os
import re
from urllib.parse import urljoin

import requests

import mangadex
import scraper as S

SITE = "https://weebcentral.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
LINK_RE = re.compile(rf"weebcentral\.com/(series|chapters)/({ULID})", re.I)
CHAPTER_LINK_RE = re.compile(rf'<a[^>]+href="(?:https://weebcentral\.com)?/chapters/({ULID})"[^>]*>(.*?)</a>', re.S | re.I)
IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"[^>]*alt="Page\s*\d+', re.I)
SERIES_IN_PAGE_RE = re.compile(rf'/series/({ULID})/([^"\'?#\s]+)')
NUMBER_RE = re.compile(r"(?:chapter|episode|ch\.?|ep\.?)\s*(\d+(?:\.\d+)?)", re.I)


def is_weebcentral(url):
    return "weebcentral.com" in (url or "").lower()


def parse_link(url):
    """'.../series/<id>/<name>' -> ('series', id); '.../chapters/<id>' -> ('chapter', id)."""
    m = LINK_RE.search(url or "")
    if not m:
        raise ValueError(url)
    return ("series" if m.group(1).lower() == "series" else "chapter"), m.group(2).upper()


def _text(html):
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip())


class Site:
    """Reads WeebCentral pages with plain HTTP, switching to the scraper's Chrome if refused."""

    def __init__(self):
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT, "Referer": SITE + "/"})
        self._browser = None          # (context manager, page, context) once Chrome is needed

    def get(self, path):
        url = urljoin(SITE, path)
        if not self._browser:
            try:
                r = self.http.get(url, timeout=30)
                if r.status_code == 200 and "challenge-platform" not in r.text[:5000]:
                    return r.text
                S.detail(f"      (WeebCentral answered {r.status_code}; using Chrome instead)")
            except requests.RequestException as e:
                S.detail(f"      (WeebCentral didn't answer: {e}; using Chrome instead)")
            self._open_browser()
        page = self._browser[1]
        return page.evaluate("async (u) => { const r = await fetch(u); return await r.text(); }", url)

    def _open_browser(self):
        cm = S.browser_session()
        page, context, user_agent = cm.__enter__()
        self._browser = (cm, page, context)
        page.goto(SITE + "/", wait_until="domcontentloaded")
        S.wait_for_reader(page, True, js_condition="() => !!document.querySelector('a[href*=\"/series/\"]')")
        # pass Chrome's cookies to plain HTTP so the images download fast
        self.http.headers["User-Agent"] = user_agent
        for c in context.cookies():
            self.http.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

    def close(self):
        if self._browser:
            try:
                self._browser[0].__exit__(None, None, None)
            except Exception:
                pass
            self._browser = None


def series_of_chapter(site, chapter_id):
    m = SERIES_IN_PAGE_RE.search(site.get(f"/chapters/{chapter_id}"))
    if not m:
        raise RuntimeError("couldn't find which series this chapter belongs to")
    return m.group(1)


def series_title(site, series_id):
    page = site.get(f"/series/{series_id}")
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    if m:
        return _text(m.group(1))
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    return _text(m.group(1)).split("|")[0].strip() if m else "Manga"


def chapter_list(site, series_id):
    """Every chapter, oldest first: [{'id', 'name', 'chapter' (number text or None), 'title'}]."""
    html = site.get(f"/series/{series_id}/full-chapter-list")
    chapters, seen = [], set()
    for cid, inner in CHAPTER_LINK_RE.findall(html):
        if cid in seen:
            continue
        seen.add(cid)
        spans = re.findall(r"<span[^>]*>([^<]*)</span>", inner)
        name = htmllib.unescape(next((s.strip() for s in spans if s.strip()), "")) or _text(inner).split(" Last Read")[0]
        m = NUMBER_RE.search(name)
        chapters.append({"id": cid, "name": name, "chapter": m.group(1) if m else None, "title": name})
    chapters.reverse()                                 # the site lists newest first
    # a number used twice (a new season starting over at 1) keeps the first; the rest get their name
    used = set()
    for c in chapters:
        n = mangadex.chapter_number(c)
        if n is not None and n in used:
            c["chapter"] = None
        elif n is not None:
            used.add(n)
    return chapters


def page_urls(site, chapter_id):
    html = site.get(f"/chapters/{chapter_id}/images?is_prev=False&current_page=1&reading_style=long_strip")
    urls = []
    for src in IMG_RE.findall(html):
        if "/static/" not in src and src not in urls:
            urls.append(urljoin(SITE, src))
    return urls


def run():
    """Same settings as a Batcave run; the issue boxes are chapter numbers (10 to 20 also gets 10.5)."""
    try:
        kind, link_id = parse_link(S.START_URL)
    except ValueError:
        print(f"That WeebCentral link doesn't look right: {S.START_URL}")
        print("   Paste a series page like https://weebcentral.com/series/<id>/<name>")
        print("   or a chapter like https://weebcentral.com/chapters/<id>")
        return

    site = Site()
    folder = os.path.join(S.FINAL_ROOT, S.SUB_PATH or "", S.FOLDER_NAME)
    print(f"Saving to: {folder}")
    try:
        try:
            series_id = series_of_chapter(site, link_id) if kind == "chapter" else link_id
            print(f"WeebCentral: {series_title(site, series_id)}")
            chapters = chapter_list(site, series_id)
        except (requests.RequestException, RuntimeError, S.BrowserError) as e:
            S.problem(f"Couldn't read the chapter list from WeebCentral: {e}")
            return
        if not chapters:
            S.problem("WeebCentral shows no chapters for this series.")
            return

        numbered = [c for c in chapters if mangadex.chapter_number(c) is not None]
        if S.ALL_ISSUES:
            todo = chapters
        else:
            todo = [c for c in numbered if S.START_ISSUE <= mangadex.chapter_number(c) < S.END_ISSUE + 1]
            have = {int(mangadex.chapter_number(c)) for c in todo}
            missing = [str(n) for n in range(S.START_ISSUE, S.END_ISSUE + 1) if n not in have]
            if missing:
                S.problem(f"Not on WeebCentral: chapter {', '.join(missing[:30])}"
                          + (" ..." if len(missing) > 30 else ""))
        print(f"{len(todo)} chapters to get")

        os.makedirs(folder, exist_ok=True)
        top = int(max([mangadex.chapter_number(c) or 0 for c in chapters] + [len(todo)]))
        for ch in todo:
            if S.STOP_REQUESTED:
                print("Stopped by user")
                break
            n = mangadex.chapter_number(ch)
            name = mangadex.file_stem(S.FILENAME_PREFIX, ch) + S.ARCHIVE_EXT
            save_path = S.batch_path(folder, name, int(n) if n is not None else 1, top,
                                     extra="" if n is not None else "Other")
            if os.path.exists(save_path):
                print(f"{ch['name']}: already there ({name})")
                continue
            S.new_issue_log()
            try:
                urls = page_urls(site, ch["id"])
                if not urls:
                    raise RuntimeError("no pages found")
                reader = f"{SITE}/chapters/{ch['id']}"
                results = S.fetch_pages(urls, reader, site.http)
                if any(r[1] is None for r in results) and site._browser:
                    results = S.retry_in_browser(results, urls, reader, site._browser[2])
                ok, info = S.save_cbz(results, save_path)
            except (requests.RequestException, RuntimeError, S.BrowserError) as e:
                ok, info = False, str(e)
            if ok:
                print(f"{ch['name']} saved ({info})")
            else:
                S.problem(f"Error: {info} for {ch['name'].lower()}")
        print("Done")
    finally:
        site.close()
