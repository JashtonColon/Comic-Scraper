"""Search every supported site for a comic or manga by name.

Used by both apps (gui.py on Windows, android_bridge.py on Android). Each result is a dict:
  {"source": "Batcave", "title": "...", "year": "1987", "info": "Ongoing", "url": "https://..."}
Picking a result puts its url in the Single series link box.
"""
import html as htmllib
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import requests

import mangadex
import readinglist
import scraper as S
import weebcentral

SOURCES = ["Batcave", "MangaDex", "WeebCentral"]
MAX_RESULTS = 25            # per site


def _clean(text):
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or ""))).strip()


# ---------------------------------------------------------------- Batcave (needs the browser)
def search_batcave(page, query, first_load=False):
    out = []
    for c in readinglist.batcave_search(page, query, first_load)[:MAX_RESULTS]:
        out.append({"source": "Batcave", "title": c.get("title") or c.get("name") or "",
                    "year": str(c.get("year") or ""), "info": "Comic", "url": c["url"].split("#")[0]})
    return out


# ---------------------------------------------------------------- MangaDex (its API)
def search_mangadex(query):
    api = mangadex.Api()
    data = api.get("/manga", {
        "title": query,
        "limit": MAX_RESULTS,
        "contentRating[]": mangadex.CONTENT_RATINGS,
        "order[relevance]": "desc",
        "availableTranslatedLanguage[]": [mangadex.LANGUAGE],
    })
    out = []
    for m in data.get("data", []):
        a = m.get("attributes") or {}
        info = [x for x in ((a.get("status") or "").title(),
                            f"last ch. {a['lastChapter']}" if a.get("lastChapter") else "") if x]
        out.append({"source": "MangaDex", "title": mangadex._title_of(a), "year": str(a.get("year") or ""),
                    "info": ", ".join(info) or "Manga", "url": f"https://mangadex.org/title/{m['id']}"})
    return out


# ---------------------------------------------------------------- WeebCentral (plain web page)
ARTICLE_RE = re.compile(r"<article\b.*?</article>", re.S | re.I)
SERIES_LINK_RE = re.compile(rf'href="(https://weebcentral\.com/series/{weebcentral.ULID}/[^"]*)"', re.I)


def search_weebcentral(query):
    """Plain web request only: a search never opens a second browser (it runs beside Batcave's)."""
    params = urlencode({"limit": MAX_RESULTS, "offset": 0, "display_mode": "Minimal Display", "text": query,
                        "sort": "Best Match", "order": "Descending", "official": "Any", "anime": "Any",
                        "adult": "Any"})
    r = requests.get(weebcentral.SITE + "/search/data?" + params, timeout=30,
                     headers={"User-Agent": weebcentral.USER_AGENT, "Referer": weebcentral.SITE + "/search"})
    if r.status_code != 200:
        raise RuntimeError(f"the site answered {r.status_code}")
    page = r.text
    out = []
    for art in ARTICLE_RE.findall(page):
        link = SERIES_LINK_RE.search(art)
        if not link:
            continue
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", art, re.S)
        facts = [_clean(d) for d in re.findall(r"<div[^>]*>(.*?)</div>", art, re.S)]
        year = next((f for f in facts if re.fullmatch(r"\d{4}", f)), "")
        info = ", ".join(f for f in facts if f and f != year)
        out.append({"source": "WeebCentral", "title": _clean(h2.group(1)) if h2 else link.group(1),
                    "year": year, "info": info or "Manga", "url": link.group(1)})
    return out


# ---------------------------------------------------------------- all of them
def search_all(query, sources=None, page=None):
    """Search the chosen sites. Batcave needs `page` (the browser); the others don't.
    Returns (results, problems): results grouped by site in SOURCES order."""
    query = (query or "").strip()
    sources = [s for s in (sources or SOURCES) if s in SOURCES]
    if not query or not sources:
        return [], []
    results, problems = {}, []

    def run(name):
        try:
            if name == "MangaDex":
                return search_mangadex(query)
            if name == "WeebCentral":
                return search_weebcentral(query)
        except (requests.RequestException, RuntimeError, KeyError, ValueError, S.BrowserError) as e:
            problems.append(f"{name}: {e}")
        return []

    # MangaDex and WeebCentral are plain web requests: run them while the browser searches Batcave
    others = [s for s in sources if s != "Batcave"]
    with ThreadPoolExecutor(max_workers=max(1, len(others))) as pool:
        futures = {s: pool.submit(run, s) for s in others}
        if "Batcave" in sources:
            if page is None:
                problems.append("Batcave: the browser isn't open")
            else:
                try:
                    results["Batcave"] = search_batcave(page, query, True)
                except Exception as e:                  # a site problem shouldn't lose the others
                    problems.append(f"Batcave: {e}")
        for s, f in futures.items():
            results[s] = f.result()
    ordered = [r for s in SOURCES for r in results.get(s, [])]
    return ordered, problems


def print_summary(query, results, problems):
    counts = {s: sum(1 for r in results if r["source"] == s) for s in SOURCES}
    shown = ", ".join(f"{n} on {s}" for s, n in counts.items() if n) or "nothing"
    print(f"Search '{query}': {shown}")
    for p in problems:
        print(f"   (couldn't search {p})")
