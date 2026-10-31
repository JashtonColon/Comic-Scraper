"""Reading lists: turn a League of Comic Geeks list (or pasted list text) into one
download where every issue lands in a single folder, numbered in reading order.

Steps (the app window runs each one for you):
  1. load_from_link / load_from_text  -> a Plan: every issue in order, grouped by series
  2. find_on_batcave                  -> for each series, the matching Batcave series page
  3. download                         -> scans each matched series once (cached), then
                                         downloads the issues in reading order
"""
import datetime
import difflib
import hashlib
import json
import threading
import os
import re
import time
from urllib.parse import quote

import scraper as S
from scraper import detail, problem

LOCG_URL = "https://leagueofcomicgeeks.com"
PLANS_DIR = os.path.join(S.APP_DATA, "reading-lists")
CACHE_DIR = os.path.join(S.APP_DATA, "batcave-cache")
CACHE_DAYS = 3          # rescan a Batcave series after this many days (new uploads)
LOCG_PAGE_SIZE = 140    # League of Comic Geeks returns series issues 140 at a time

# Titles that are reprint collections, not single issues (unticked by default)
COLLECTED_RE = re.compile(r"\b(HC|TP|TPB|Omnibus|Compendium|Hardcover|Graphic Novel|Box Set)\b", re.I)


# =====================================================================
#  Small helpers
# =====================================================================

def norm_title(title):
    """'The Adventures of Superman' and 'Adventures Of Superman' compare equal."""
    t = title.lower().replace("&", " and ")
    t = re.sub(r"^the\s+", "", t.strip())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def norm_num(num):
    """'1,000,000', '1m' and '1000000' are the same issue; '007' is '7'."""
    n = str(num).lower().replace(",", "").strip().lstrip("#")
    if n in ("1m", "one million", "1000000"):
        return "1000000"
    if re.fullmatch(r"\d+", n):
        return str(int(n))
    return n


def split_title(full):
    """'Action Comics Annual #3' -> ('Action Comics Annual', '3'). One-shots get number '1'."""
    m = re.match(r"^(.*?)\s*#\s*([^\s#]+)\s*$", full.strip())
    if m:
        return m.group(1).strip(), norm_num(m.group(2))
    return full.strip(), "1"


def gtitle(group):
    """The series title as League of Comic Geeks writes it, for searching and comparing.
    Rows split by volume are named "Title (year)" but search as plain "Title"."""
    return group.get("title") or group["name"]


def is_annual(series_name):
    return bool(re.search(r"\bannual\b", series_name, re.I))


def safe_filename(name):
    name = name.replace(":", " -").replace("/", "-").replace("\\", "-")
    name = re.sub(r'[<>"|?*]', "", name)
    return re.sub(r"\s+", " ", name).strip().rstrip(".")


def year_of(ts):
    try:
        return datetime.datetime.utcfromtimestamp(int(ts)).year
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def is_out(item, now=None):
    """Has this issue been released? (League of Comic Geeks lists upcoming issues with future dates.)
    Issues with no date count as out."""
    d = item.get("date")
    if not d:
        return True
    try:
        return int(d) <= (now or time.time())
    except (TypeError, ValueError):
        return True


def release_text(item):
    try:
        return datetime.datetime.utcfromtimestamp(int(item["date"])).strftime("%b %d, %Y")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return "soon"


def series_items(plan, group, released_only=False):
    items = [it for it in plan["items"] if it["series"] == group["name"]]
    return [it for it in items if is_out(it)] if released_only else items


def plan_path(plan):
    return os.path.join(PLANS_DIR, safe_filename(plan["name"]) + ".json")


_SAVE_LOCK = threading.Lock()


def save_plan(plan):
    """Save the list. Written to a temp file first, so a crash never leaves half a file. Windows
    sometimes refuses a write for a moment (Errno 22 / 13) while antivirus, OneDrive or another
    window is reading the file: try again a few times, and never stop the job over it."""
    os.makedirs(PLANS_DIR, exist_ok=True)
    path = plan_path(plan)
    with _SAVE_LOCK:
        text = json.dumps(plan, indent=1)
        for attempt in range(6):
            tmp = f"{path}.{os.getpid()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
                return True
            except OSError as e:
                last = e
                time.sleep(0.2 * (attempt + 1))
        problem(f"   (couldn't save the list right now: {last}; it'll be saved on the next change)")
        return False


def load_saved_plan(name):
    try:
        with open(os.path.join(PLANS_DIR, safe_filename(name) + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def build_plan(name, items, source):
    """items: [{'title', 'date', 'href'}] already in reading order."""
    plan = {"name": name or "Reading List", "source": source, "items": [], "groups": {}}
    for pos, it in enumerate(items, 1):
        series, num = split_title(it["title"])
        collected = bool(COLLECTED_RE.search(it["title"]))
        plan["items"].append({"pos": pos, "title": it["title"], "series": series, "num": num,
                              "date": it.get("date"), "href": it.get("href", "")})
        g = plan["groups"].setdefault(series, {
            "name": series, "annual": is_annual(series), "count": 0, "first_href": it.get("href", ""),
            "year": None, "include": not collected, "match": None, "candidates": [], "status": "",
        })
        g["count"] += 1
        if g["year"] is None:
            g["year"] = year_of(it.get("date"))
    add_issue_years(plan)
    return plan


def add_issue_years(plan):
    """For each series, the first and last year its listed issues came out."""
    for g in plan["groups"].values():
        g["first_year"] = g["last_year"] = None
    for it in plan["items"]:
        y = year_of(it.get("date"))
        g = plan["groups"].get(it["series"])
        if y and g:
            g["first_year"] = y if g["first_year"] is None else min(g["first_year"], y)
            g["last_year"] = y if g["last_year"] is None else max(g["last_year"], y)


def merge_saved_matches(plan):
    """If this list was loaded before, keep the Batcave matches and ticks you already chose."""
    old = load_saved_plan(plan["name"])
    if not old:
        return plan
    for key, g in plan["groups"].items():
        o = old.get("groups", {}).get(key)
        if o:
            for k in ("include", "match", "candidates", "status", "year"):
                if o.get(k) is not None:
                    g[k] = o[k]
    return plan


# =====================================================================
#  League of Comic Geeks
# =====================================================================

LIST_JS = r"""
() => {
  let name = (document.querySelector('meta[property="og:title"]')?.content || document.title || '').trim();
  name = name.replace(/\s+by\s+\S+$/, '');
  const items = [...document.querySelectorAll('li.issue[data-parent="0"]')].map(li => {
    const a = [...li.querySelectorAll('a[href*="/comic/"]')].find(x => x.textContent.trim());
    const d = li.querySelector('.date');
    return {title: a ? a.textContent.trim() : '', href: a ? a.getAttribute('href') : '',
            date: d && d.dataset.date ? Number(d.dataset.date) : null};
  }).filter(x => x.title);
  return {name, items};
}
"""

ISSUE_SERIES_JS = r"""
async (href) => {
  const h = await (await fetch(href)).text();
  const d = new DOMParser().parseFromString(h, 'text/html');
  const a = [...d.querySelectorAll('a[href*="/comics/series/"]')].find(x => x.textContent.trim() === 'Series');
  const m = (d.body ? d.body.innerText : '').match(/((?:19|20)\d\d)\s*-\s*((?:19|20)\d\d|Present)/);
  return {series: a ? a.getAttribute('href') : '', start: m ? Number(m[1]) : null};
}
"""

SEARCH_SERIES_JS = r"""
async (q) => {
  const h = await (await fetch('/search/series?keyword=' + encodeURIComponent(q))).text();
  const d = new DOMParser().parseFromString(h, 'text/html');
  return [...d.querySelectorAll('li')].filter(li => li.querySelector('.title a[href*="/comics/series/"]')).map(li => {
    const a = li.querySelector('.title a');
    return {id: a.dataset.id || (a.getAttribute('href').match(/series\/(\d+)/) || [])[1], title: a.textContent.trim(),
            meta: (li.querySelector('.copy-really-small')?.textContent || '').replace(/\s+/g, ' ').trim(),
            count: Number((li.querySelector('.count-issues')?.textContent || '0').trim()) || 0};
  });
}
"""

SERIES_ISSUES_JS = r"""
async ([id, offset]) => {
  const u = `/comic/get_comics?addons=1&list=series&list_option=&list_mode_offset=${offset}&order=date-asc`
          + `&format%5B%5D=1&format%5B%5D=6&series_id=${id}&view=list`;
  const j = await (await fetch(u, {headers: {'X-Requested-With': 'XMLHttpRequest'}})).json();
  const d = new DOMParser().parseFromString(j.list || '', 'text/html');
  return [...d.querySelectorAll('li[data-parent="0"]')].map(li => {
    const a = [...li.querySelectorAll('a[href*="/comic/"]')].find(x => x.textContent.trim());
    const dt = li.querySelector('.date');
    return {title: a ? a.textContent.trim() : '', href: a ? a.getAttribute('href') : '',
            date: dt && dt.dataset.date ? Number(dt.dataset.date) : null};
  }).filter(x => x.title);
}
"""


def _locg_ready(page, url, first_load=True):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    S.wait_for_reader(page, first_load, js_condition="() => document.querySelector('#comic-list, li.issue, .title')")


def load_from_link(page, url):
    """Read a League of Comic Geeks list page, in its own order."""
    if "leagueofcomicgeeks.com" not in url or "/lists/" not in url:
        raise ValueError("That isn't a League of Comic Geeks list link.\n"
                         "It should look like https://leagueofcomicgeeks.com/profile/<name>/lists/<number>/<title>")
    print("Reading the list from League of Comic Geeks...")
    _locg_ready(page, url)
    S.wait_for_reader(page, True, selector="li.issue")
    # long lists can load more as you scroll; keep scrolling until nothing new appears
    last = -1
    for _ in range(40):
        n = page.evaluate("document.querySelectorAll('li.issue[data-parent=\"0\"]').length")
        if n == last:
            break
        last = n
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)
    data = page.evaluate(LIST_JS)
    if not data["items"]:
        raise ValueError("No issues found on that list page.")
    plan = build_plan(data["name"], data["items"], {"type": "link", "url": url})
    print(f"Found {len(plan['items'])} issues across {len(plan['groups'])} series.")
    _fill_series_years(page, plan)
    plan = merge_saved_matches(plan)
    split_volumes(page, plan)
    return plan


def _fill_series_years(page, plan):
    """Look up each series' start year (e.g. Superman 1986-2006 -> 1986) to pick the right volume."""
    print("Checking which volume each series is...")
    for i, g in enumerate(plan["groups"].values(), 1):
        if g.get("locg_series") or not g.get("first_href"):
            continue
        detail(f"   [{i}/{len(plan['groups'])}] {g['name']}")
        try:
            info = page.evaluate(ISSUE_SERIES_JS, g["first_href"])
            g["locg_series"] = info.get("series") or ""
            # annuals sit inside the main series on LoCG (Action Comics 1938-2011), but sites
            # like Batcave list them by their own first year, so keep the first annual's year
            if info.get("start") and not g["annual"]:
                g["year"] = info["start"]
        except Exception as e:
            detail(f"   (couldn't check {g['name']}: {e})")


COMIC_ID_RE = re.compile(r"/comic/(\d+)")


def _comic_id(href):
    m = COMIC_ID_RE.search(href or "")
    return m.group(1) if m else None


def _mixes_volumes(items):
    """Same title but probably several volumes: an issue number repeats, or the issues span decades."""
    seen = set()
    for it in items:
        if it["num"] in seen:
            return True
        seen.add(it["num"])
    years = [y for y in (year_of(it.get("date")) for it in items) if y]
    return bool(years) and max(years) - min(years) > 15


VOLUME_VERSION = 2    # bump to re-check every row for mixed volumes (older lists marked rows done too early)


def split_volumes(page, plan):
    """League of Comic Geeks calls every volume "The Amazing Spider-Man #1", but Batcave has a
    series per volume. Look up which LoCG series each issue belongs to and split the row into
    "The Amazing Spider-Man (1963)", "(1999)", "(2014)"... Needs a League of Comic Geeks page open."""
    groups = plan["groups"]
    split_any = False
    for name in list(groups):
        g = groups[name]
        if g.get("volumes_checked") == VOLUME_VERSION:
            continue
        items = [it for it in plan["items"] if it["series"] == name]
        if len(items) < 2 or not _mixes_volumes(items) or not all(it.get("href") for it in items):
            g["volumes_checked"] = VOLUME_VERSION
            continue
        failed = False
        print(f"   {name}: checking which volume each of its {len(items)} issues is from...")
        vols, todo = [], list(items)
        for _ in range(40):
            if not todo:
                break
            try:
                info = page.evaluate(ISSUE_SERIES_JS, todo[0]["href"])
                sid = re.search(r"/series/(\d+)", info.get("series") or "")
                if not sid:
                    failed = True
                    break
                ids, start = set(), None
                for offset in range(0, 6000, LOCG_PAGE_SIZE):
                    batch = page.evaluate(SERIES_ISSUES_JS, [sid.group(1), offset])
                    if batch and start is None:
                        start = year_of(batch[0].get("date"))     # first issue of the volume
                    ids |= {_comic_id(b["href"]) for b in batch}
                    if len(batch) < LOCG_PAGE_SIZE:
                        break
            except Exception as e:
                problem(f"      (couldn't check volumes: {e})")
                failed = True
                break
            mine = [it for it in todo if _comic_id(it["href"]) in ids] or [todo[0]]
            vols.append((start or info.get("start"), mine))
            todo = [it for it in todo if it not in mine]
        if failed and len(vols) < 2:
            continue                    # leave it unchecked so the next Find tries again
        if todo:
            vols.append((None, todo))
        g["volumes_checked"] = VOLUME_VERSION
        if len(vols) < 2:
            continue
        split_any = True
        template = dict(g)
        del groups[name]
        for start, its in vols:
            key = f"{name} ({start})" if start else name
            while key in groups:
                key += "*"
            ng = dict(template)
            ng.update(name=key, title=name, count=len(its), first_href=its[0]["href"], year=start or g.get("year"),
                      match=None, candidates=[], status="", volumes_checked=VOLUME_VERSION)
            ng.pop("found", None)
            ng.pop("missing", None)
            groups[key] = ng
            for it in its:
                it["series"] = key
        print(f"      -> {len(vols)} volumes: " + ", ".join(str(v[0] or "?") for v in vols))
    if split_any:
        # keep the table in reading order of each row's first issue
        order = {}
        for it in plan["items"]:
            order.setdefault(it["series"], it["pos"])
        plan["groups"] = dict(sorted(groups.items(), key=lambda kv: order.get(kv[0], 0)))
        add_issue_years(plan)
    return split_any


# ---- pasted text, e.g. "Superman #1-176, #0, #1m, Annual #1-12" ----

def parse_list_text(text):
    """Returns ([(title, [(annual, number), ...]), ...], (first_year, last_year) or None)."""
    entries = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*\u2022\u25cb\u25e6\u00b7 ").strip()
        if "#" not in line:
            continue
        title = line[:line.index("#")].strip().rstrip(",")
        if not title:
            continue
        nums = []
        for seg in line[line.index("#"):].split(","):
            seg = seg.strip()
            annual = bool(re.search(r"\bannual\b", seg, re.I))
            m = re.search(r"#\s*([0-9a-zA-Z.,]+?)\s*(?:-\s*#?\s*([0-9]+))?\s*$", seg)
            if not m:
                continue
            a, b = m.group(1), m.group(2)
            if b and a.isdigit():
                nums += [(annual, str(n)) for n in range(int(a), int(b) + 1)]
            else:
                nums.append((annual, norm_num(a)))
        if nums:
            entries.append((title, nums))
    years = [int(y) for y in re.findall(r"\b(19[3-9]\d|20[0-4]\d)\b", text)]
    span = (min(years), max(years)) if years else None
    return entries, span


def _parse_meta_years(meta):
    m = re.search(r"((?:19|20)\d\d)(?:\s*-\s*((?:19|20)\d\d|Present))?", meta or "")
    if not m:
        return None, None
    start = int(m.group(1))
    end = 9999 if m.group(2) == "Present" else int(m.group(2) or start)
    return start, end


def pick_locg_series(title, results, span):
    """Choose the series volume that fits: same title, overlapping the list's years, most issues."""
    exact = [r for r in results if norm_title(r["title"]) == norm_title(title)]
    pool = exact or results
    if not pool:
        return None

    def score(r):
        start, end = _parse_meta_years(r.get("meta"))
        overlap = 0
        if span and start:
            overlap = max(0, min(end, span[1]) - max(start, span[0]) + 1)
        big_publisher = 1 if re.search(r"\b(DC|Marvel|Image|Dark Horse|IDW|Boom|Dynamite)\b", r.get("meta", "")) else 0
        return (overlap > 0, big_publisher, overlap, r.get("count", 0))

    return max(pool, key=score)


def load_from_text(page, text, name="Reading List"):
    """Build the reading order from pasted text, using release dates from League of Comic Geeks."""
    entries, span = parse_list_text(text)
    if not entries:
        raise ValueError("Couldn't find any lines like 'Superman #1-176, #0, Annual #1-12' in that text.")
    print(f"Read {len(entries)} series from the text"
          + (f" (years {span[0]}-{span[1]})" if span else "") + ". Looking them up...")
    page.goto(LOCG_URL + "/", wait_until="domcontentloaded", timeout=60000)
    S.wait_for_reader(page, True, js_condition="() => document.querySelector('a[href*=\"/comics/\"]')")

    found, missing = [], []
    for title, wanted in entries:
        results = page.evaluate(SEARCH_SERIES_JS, title)
        series = pick_locg_series(title, results, span)
        if not series:
            problem(f"   Couldn't find '{title}' on League of Comic Geeks")
            missing += [f"{title} #{n}" for _, n in wanted]
            continue
        detail(f"   {title} -> {series['title']} ({series.get('meta', '')})")
        issues = []
        for offset in range(0, 5000, LOCG_PAGE_SIZE):
            batch = page.evaluate(SERIES_ISSUES_JS, [series["id"], offset])
            issues += batch
            if len(batch) < LOCG_PAGE_SIZE:
                break
        by_key = {}
        for it in issues:
            s_name, num = split_title(it["title"])
            by_key.setdefault((is_annual(s_name), num), it)
        for annual, num in wanted:
            it = by_key.get((annual, num))
            if it:
                found.append(it)
            else:
                missing.append(f"{title}{' Annual' if annual else ''} #{num}")

    # reading order = release order (stable, so same-day issues keep the text's order)
    found.sort(key=lambda it: it["date"] or 0)
    plan = build_plan(name, found, {"type": "text", "text": text})
    plan["not_on_locg"] = missing
    print(f"Found {len(found)} issues across {len(plan['groups'])} series, in release order.")
    if missing:
        problem(f"   {len(missing)} issues weren't found on League of Comic Geeks: " + ", ".join(missing[:10])
                + (" ..." if len(missing) > 10 else ""))
    _fill_series_years(page, plan)
    plan = merge_saved_matches(plan)
    split_volumes(page, plan)
    return plan


# =====================================================================
#  Batcave
# =====================================================================

SEARCH_RESULTS_JS = r"""
() => {
  // the results come after the "Your query found N answers" line; anything before is sidebar
  const marker = [...document.querySelectorAll('div, p, span, h1, h2, h3, h4')]
      .filter(e => /query found/i.test(e.textContent || ''))
      .sort((a, b) => a.textContent.length - b.textContent.length)[0];
  const sidebar = el => { for (let p = el, i = 0; p && i < 6; p = p.parentElement, i++) {
      if (p.tagName === 'ASIDE' || /(^|[\s_-])(side|sidebar)([\s_-]|$)/i.test(p.className || '')) return true; }
    return false; };
  // series pages look like /20229-lex-luthor-the-unauthorized-biography.html
  const isSeries = a => /\/\d+-[^\/]+\.html(#.*)?$/.test(a.getAttribute('href') || '') && !a.href.includes('/reader/');
  const text = document.body ? document.body.innerText : '';
  if (!marker && /unfortunately|no results|nothing (was )?found|did not (return|find)/i.test(text)) return [];
  if (marker && /found\s+0\s+answer/i.test(marker.textContent)) return [];
  const byUrl = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    if (!isSeries(a) || sidebar(a)) continue;
    if (marker && !(marker.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
    const url = a.href.split('#')[0];
    const t = a.textContent.replace(/\s+/g, ' ').trim();
    if (!byUrl.has(url)) byUrl.set(url, {a, title: ''});
    if (t && !/^read$/i.test(t) && t.length > byUrl.get(url).title.length) byUrl.set(url, {a, title: t});
  }
  const out = [];
  for (const [url, {a, title}] of byUrl) {
    if (!title) continue;
    let name = title, year = null, end = null;
    const m = title.match(/^(.+?)\s*\((\d{4})(\s*-\s*(\d{4})?)?\)$/);     // "Superman (1987-2006)"
    if (m) {
      name = m[1]; year = Number(m[2]); end = m[4] ? Number(m[4]) : (m[3] ? 9999 : null);
    } else {
      // no year in the title ("Invasion"): read it from the card, e.g. "DC Comics  1988"
      let card = a;
      for (let i = 0; i < 6 && card.parentElement; i++) {
        card = card.parentElement;
        if (/last issue|read/i.test(card.innerText || '') && card.innerText.length > title.length + 10) break;
      }
      const rest = (card.innerText || '').replace(title, ' ');
      const y = rest.match(/\b(19[3-9]\d|20[0-4]\d)\b/);
      if (y) year = Number(y[1]);
    }
    out.push({title: year && !m ? `${name} (${year})` : title, name, year, end, url});
  }
  return out;
}
"""

SEARCH_DONE_JS = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  return /query found|no results|nothing (was )?found|did not (return|find)|unfortunately/i.test(t);
}
"""

SERIES_LINKS_JS = r"""
() => [...document.querySelectorAll('a[href*="/reader/"]')]
        .map(a => [a.textContent.replace(/\s+/g, ' ').trim(), a.href])
        // every issue link, including one-shots named "... Full"; not the "Start Reading" button
        .filter(([t]) => t && !/^(start|continue|read)\b/i.test(t))
"""

OPEN_CHAPTERS_JS = r"""
() => {
  const tab = [...document.querySelectorAll('a, button, li, div, span')]
      .find(e => /^chapters\s*(\(\d+\))?$/i.test((e.textContent || '').trim()));
  if (tab) tab.click();
  return !!tab;
}
"""

NEXT_PAGE_JS = r"""
() => {
  const els = [...document.querySelectorAll('a, button, span, li')];
  const el = els.find(e => /^(forward|next|\u203a|\u00bb|>)$/i.test(e.textContent.trim())
                         && !/disabled/i.test(e.className) && e.getAttribute('aria-disabled') !== 'true'
                         && !e.hasAttribute('disabled'));
  if (!el) return false;
  el.click();
  return true;
}
"""


ANNUAL_YEAR_RE = re.compile(r"(?:\b((?:19|20)\d\d)|'(\d\d))\s+annual\b|\bannual\s+(?:((?:19|20)\d\d)\b|'(\d\d)\b)", re.I)


def full_year(two):
    """'96 -> 1996, '05 -> 2005"""
    n = int(two)
    return str(1900 + n if n >= 30 else 2000 + n)


def annual_year(name):
    """'Mighty Morphin Power Rangers 2018 Annual' -> '2018' (Batcave calls it '_Annual 2018');
    "The Amazing Spider-Man '96 Annual" -> '1996' (Batcave: "_Annual '96")."""
    m = ANNUAL_YEAR_RE.search(name)
    if not m:
        return None
    four = m.group(1) or m.group(3)
    return four if four else full_year(m.group(2) or m.group(4))


def base_name(name):
    """'Superman Annual' -> 'Superman'; 'Mighty Morphin Power Rangers 2018 Annual' -> 'Mighty Morphin Power Rangers'."""
    name = ANNUAL_YEAR_RE.sub(" ", name)
    return re.sub(r"\s+", " ", re.sub(r"\bannual\b", " ", name, flags=re.I)).strip()


def title_score(a, b):
    a, b = norm_title(a), norm_title(b)
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    # "Newstime: The Life and Death of Superman" vs "Newstime", "The Legacy of Superman"
    # vs "Superman: The Legacy of Superman": one name is the other plus a subtitle
    if short and (long_.startswith(short + " ") or long_.endswith(" " + short)
                  or (len(short.split()) >= 3 and short in long_)):
        return 0.85
    return round(difflib.SequenceMatcher(None, a, b).ratio() * 0.8, 3)


def score_candidate(group, cand):
    """How well a Batcave series fits a reading-list series (1.0 = same title, right years)."""
    name = gtitle(group)
    if group.get("annual") and not is_annual(cand["name"]):
        # Batcave usually keeps annuals inside the main series: "Superman (1987-2006)"
        s = title_score(base_name(name), cand["name"]) - 0.03
    else:
        s = title_score(name, cand["name"])
    cand["title_score"] = round(s, 3)
    cand["fits"] = True
    first = group.get("first_year") or group.get("year")
    start, end = cand.get("year"), cand.get("end")
    if first and start:
        last = group.get("last_year") or first
        if start > first + 1:              # series began after these issues came out
            s -= 0.3
            cand["fits"] = False
        elif end and end != 9999 and last > end + 1:   # series had ended before them
            s -= 0.3
            cand["fits"] = False
        elif first > start:                # running then: prefer the most recent such volume
            s -= min(0.001 * (first - start), 0.05)
    return round(s, 3)


def search_queries(group):
    """Batcave's search is picky about punctuation, so try a few spellings."""
    name = gtitle(group)
    clean = re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", name.replace("&", " and ").replace(".", ""))).strip()
    year = group.get("first_year") or group.get("year")
    qs = [f"{name} {year}" if year else name, name, clean]
    no_the = re.sub(r"^the\s+", "", clean, flags=re.I)
    qs.append(no_the)
    if group.get("annual"):
        qs.append(base_name(name))                       # "L.E.G.I.O.N. Annual" -> "L.E.G.I.O.N."
        qs.append(re.sub(r"^the\s+", "", base_name(clean), flags=re.I))
    if re.search(r"micro-?\s?series", clean, re.I):     # Batcave writes it all three ways
        for spelling in ("Micro-Series", "Microseries", "Micro Series"):
            qs.append(re.sub(r"micro-?\s?series", spelling, clean, flags=re.I))
    if ":" in name:
        sub = name.split(":", 1)[1].strip()
        if len(sub.split()) >= 2:                        # "TMNT: Villains Micro-Series" -> "Villains Micro-Series"
            qs.append(sub)
        qs.append(name.split(":")[0].strip())          # "Newstime: The Life..." -> "Newstime"
    if "," in name:                                     # "Peter Parker, The Spectacular Spider-Man"
        after = name.split(",", 1)[1].strip()
        qs += [f"{after} {year}" if year else after, after]
    out = []
    for q in qs:
        if q and q.lower() not in [o.lower() for o in out]:
            out.append(q)
    return out


def batcave_search(page, query, first_load=False):
    url = f"{S.SITE_URL}/search/{quote(query, safe='')}"
    detail(f"   searching Batcave: {query}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        S.wait_for_reader(page, first_load, js_condition=SEARCH_DONE_JS)
    except S.PWTimeout:
        return []
    time.sleep(0.5)
    save_debug(page, "search", query)
    return page.evaluate(SEARCH_RESULTS_JS)


def confident(cands):
    """Top result has the same title, was running when the issues came out, and no other
    series with that title started the same year (that would need a person to decide)."""
    if not cands:
        return False
    best = max(cands, key=lambda c: c["score"])
    if best.get("title_score", 0) < 0.96 or not best.get("fits", True):
        return False
    twins = [c for c in cands if c is not best and c.get("title_score", 0) >= 0.96
             and c.get("fits", True) and c.get("year") == best.get("year")]
    return not twins


def find_match(page, group, first_load=False):
    """Search Batcave for one series. Sets group['candidates'], group['match'] and group['status']."""
    seen, cands = set(), []
    for q in search_queries(group):
        for c in batcave_search(page, q, first_load):
            if c["url"] not in seen:
                seen.add(c["url"])
                c["score"] = score_candidate(group, c)
                cands.append(c)
        first_load = False
        if confident(cands):
            break
    cands.sort(key=lambda c: -c["score"])
    group["candidates"] = cands[:12]
    best = cands[0] if cands else None
    if confident(cands):
        group["match"] = {k: best[k] for k in ("title", "name", "year", "url")}
        group["status"] = "matched"
    elif best and best["score"] >= 0.6:
        group["match"] = {k: best[k] for k in ("title", "name", "year", "url")}
        group["status"] = "check"       # probably right, but worth a look
    else:
        group["match"] = None
        group["status"] = "not found"
    return group


def _max_number(bc):
    nums = [float(e["num"]) for e in bc["entries"]
            if e["explicit"] and not e["annual"] and re.fullmatch(r"-?\d+(\.\d+)?", e["num"])]
    return max(nums) if nums else None


def lookup_with_extras(item, group, bc, extras):
    """The main Batcave series first. Extra series with the same title (Batcave files the 1998
    Amazing Spider-Man's #500-700 under the 1963 series) are only used for numbers higher than
    anything on the main page, so a missing #30 is never filled with a different volume's #30."""
    found = lookup(item, group, bc)
    if found or not extras:
        return found
    top = _max_number(bc)
    if top is None or not re.fullmatch(r"\d+(\.\d+)?", item["num"]) or float(item["num"]) <= top:
        return None
    for extra in extras:
        found = lookup(item, group, extra)
        if found:
            return found
    return None


def coverage(page, plan, group, url, first_load=False):
    """How many of the list's issues for this series are on that Batcave page: (found, needed)."""
    issues = batcave_series_issues(page, url, first_load)
    bc = issue_map(issues)
    items = series_items(plan, group, released_only=True)
    have = [it for it in items if lookup(it, group, bc)]
    missing = [it["title"] for it in items if it not in have]
    return len(have), len(items), missing


SUBTITLE_SKIP = {"the", "a", "an", "of", "and", "in", "on", "vs", "series", "mini", "limited", "micro",
                 "microseries", "special", "one", "shot", "vol", "volume"}


def lacks_subtitle(g, title):
    """'Teenage Mutant Ninja Turtles: Villains Micro-Series' vs Batcave's main 'Teenage Mutant Ninja
    Turtles (2011-)': none of the words after the colon are in the Batcave name."""
    name = gtitle(g)
    if ":" not in name:
        return False
    words = {w for w in norm_title(name.split(":", 1)[1]).split() if w not in SUBTITLE_SKIP and not w.isdigit()}
    return bool(words) and not (words & set(norm_title(title or "").split()))


def taken_by_other_row(plan, g, url):
    return any(o is not g and (o.get("match") or {}).get("url") == url for o in plan["groups"].values())


def verify_match(page, plan, g, first_load=False):
    """Check the chosen series really has the issues. If it has none of them, try the next
    few search results and keep whichever has the most."""
    tried = []
    if g.get("status") != "confirmed" and g.get("candidates"):
        # re-rank with the current rules (matches made by older versions may have picked the wrong volume)
        for c in g["candidates"]:
            c["score"] = score_candidate(g, c)
        g["candidates"].sort(key=lambda c: -c["score"])
        if confident(g["candidates"]) and g["candidates"][0]["url"] != (g.get("match") or {}).get("url"):
            best = g["candidates"][0]
            g["match"] = {k: best.get(k) for k in ("title", "name", "year", "url")}
    options = [g["match"]] if g.get("match") else []
    if g.get("status") != "confirmed":
        for c in g.get("candidates") or []:
            if c.get("score", 0) >= 0.5 and c.get("fits", True) and all(c["url"] != o["url"] for o in options):
                options.append({k: c.get(k) for k in ("title", "name", "year", "url")})
        # never use the main series for a spin-off: "TMNT: Villains Micro-Series" #1-8 are not
        # TMNT (2011-) #1-8. A Batcave series without the subtitle that another row already uses is out.
        wrong = [o for o in options if lacks_subtitle(g, o["title"]) and taken_by_other_row(plan, g, o["url"])]
        if wrong:
            options = [o for o in options if o not in wrong]
            if not options:
                g["match"] = None
                g["status"] = "not found"
                g["found"] = [0, len(series_items(plan, g, released_only=True))]
                problem(f"   {g['name']}: Batcave only has the main series ({wrong[0]['title']}), "
                        "not this one. Double-click the row to search or paste its page.")
                return
            if g.get("match") in wrong:
                g["match"] = options[0]
                g["status"] = "check"
    for opt in options[:4]:
        have, need, missing = coverage(page, plan, g, opt["url"], first_load)
        first_load = False
        tried.append((have, opt, missing))
        detail(f"      {opt['title']}: {have}/{need} of the list's issues")
        if have == need:
            break
    if not tried:
        return
    def strength(t):
        have, opt, _ = t
        c = next((c for c in g.get("candidates") or [] if c["url"] == opt["url"]), {})
        is_current = opt["url"] == (g.get("match") or {}).get("url")
        # more issues wins, but a looser name never replaces the current match
        return (have, c.get("title_score", 1.0 if is_current else 0), is_current)
    current = next((t for t in tried if t[1]["url"] == (g.get("match") or {}).get("url")), None)
    pool = tried
    if current and current[0] > 0:
        cur_ts = next((c.get("title_score", 1.0) for c in g.get("candidates") or []
                       if c["url"] == current[1]["url"]), 1.0)
        pool = [current] + [t for t in tried if t is not current and t[0] > current[0] and
                            next((c.get("title_score", 0) for c in g.get("candidates") or []
                                  if c["url"] == t[1]["url"]), 0) >= cur_ts - 0.01]
    have, best, missing = max(pool, key=strength)
    need = len(series_items(plan, g, released_only=True))
    g["upcoming"] = g["count"] - need
    g["found"] = [have, need]
    g["missing"] = missing[:50]
    if have == 0:
        if g.get("status") != "confirmed":
            g["status"] = "not found"
        problem(f"   {g['name']}: none of its {need} issues are on "
                + (f"{best['title']}" if best else "Batcave"))
        return
    if best["url"] != (g.get("match") or {}).get("url"):
        g["match"] = best
        if g.get("status") != "confirmed":
            g["status"] = "check"
    # higher numbers Batcave filed under another series with the same title (see lookup_with_extras)
    g["extra"] = []
    if missing:
        items = series_items(plan, g, released_only=True)
        main = issue_map(batcave_series_issues(page, best["url"]))
        extras = []
        for c in g.get("candidates") or []:
            if c["url"] == best["url"] or c.get("title_score", 0) < 0.96:
                continue
            extra = issue_map(batcave_series_issues(page, c["url"]))
            gained = [it for it in items if not lookup_with_extras(it, g, main, extras)
                      and lookup_with_extras(it, g, main, extras + [extra])]
            if gained:
                extras.append(extra)
                g["extra"].append({"title": c["title"], "url": c["url"]})
                detail(f"      +{len(gained)} higher-numbered issues from {c['title']}")
            if len(g["extra"]) >= 3:
                break
        if g["extra"]:
            still = [it["title"] for it in items if not lookup_with_extras(it, g, main, extras)]
            have = need - len(still)
            g["found"] = [have, need]
            g["missing"] = still[:50]
    cand = next((c for c in g.get("candidates") or [] if c["url"] == best["url"]), {})
    first = g.get("first_year")
    close_year = bool(first and cand.get("year") and abs(cand["year"] - first) <= 1)
    if g.get("status") == "check" and have == need and (
            cand.get("title_score", 0) >= 0.96 or (cand.get("title_score", 0) >= 0.85 and close_year)):
        g["status"] = "matched"         # right name (or name + subtitle, same year) and every issue is there


CHECK_VERSION = 9     # bump when issue matching changes, so saved counts get redone


def find_on_batcave(page, plan, on_update=None, redo=False):
    if any(g.get("volumes_checked") != VOLUME_VERSION for g in plan["groups"].values()):
        print("Checking for series that mix several volumes...")
        try:
            page.goto(LOCG_URL + "/", wait_until="domcontentloaded", timeout=60000)
            S.wait_for_reader(page, True, js_condition="() => document.querySelector('a[href*=\"/comics/\"]')")
            if split_volumes(page, plan):
                save_plan(plan)
        except Exception as e:
            problem(f"   Couldn't check volumes on League of Comic Geeks: {e}")
    if any("first_year" not in g for g in plan["groups"].values()):
        add_issue_years(plan)          # lists saved by the first version don't have these yet
    if plan.get("check_version") != CHECK_VERSION:
        for g in plan["groups"].values():   # counts from an older version may be wrong: redo them
            g.pop("found", None)
        plan["check_version"] = CHECK_VERSION
    groups = [g for g in plan["groups"].values() if g["include"]]
    todo = [g for g in groups if redo or g.get("status") not in ("matched", "confirmed")
            or not g.get("found") or g["found"][0] < g["found"][1]]
    print(f"Finding {len(todo)} series on Batcave and checking their issues...")
    first = True
    for i, g in enumerate(todo, 1):
        if S.STOP_REQUESTED:
            print("Stopped by user")
            break
        released = len(series_items(plan, g, released_only=True))
        g["upcoming"] = g["count"] - released
        if not released:
            if g.get("status") not in ("matched", "confirmed"):
                g["status"] = "upcoming"
            g["found"] = [0, 0]
            detail(f"   [{i}/{len(todo)}] {g['name']}: not out yet")
            save_plan(plan)
            if on_update:
                on_update(g)
            continue
        if g.get("status") == "upcoming":
            g["status"] = ""            # some of it has come out since
        try:
            if redo or g.get("status") not in ("matched", "confirmed"):
                find_match(page, g, first)
                first = False
            if g.get("match") or g.get("candidates"):
                verify_match(page, plan, g, first)
        except Exception as e:
            if g.get("status") != "confirmed":
                g["status"] = "not found"
            problem(f"   Search failed for {g['name']}: {e}")
        first = False
        shown = g["match"]["title"] if g.get("match") else "no match"
        detail(f"   [{i}/{len(todo)}] {g['name']} -> {shown} ({g['status']})")
        save_plan(plan)
        if on_update:
            on_update(g)
    counts = {}
    for g in groups:
        counts[g.get("status") or "not searched"] = counts.get(g.get("status") or "not searched", 0) + 1
    print("Done: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


DEBUG_DIR = os.path.join(S.APP_DATA, "debug")


def save_debug(page, kind, label):
    """Keep a copy of pages the app read (search results, series lists) so problems can be
    checked later without screenshots. Only the newest 300 are kept."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", label.split("://")[-1])[-90:]
        with open(os.path.join(DEBUG_DIR, f"{kind}-{name}.html"), "w", encoding="utf-8") as f:
            f.write(f"<!-- {page.url} -->\n" + page.content())
        files = sorted((os.path.join(DEBUG_DIR, n) for n in os.listdir(DEBUG_DIR)), key=os.path.getmtime)
        for old in files[:-300]:
            os.remove(old)
    except Exception:
        pass


def _cache_file(url):
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")


def batcave_series_issues(page, url, first_load=False, force=False):
    """Every issue on a Batcave series page as [[title, reader_url], ...]. Clicks through all pages."""
    path = _cache_file(url)
    if not force:
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached["time"] < CACHE_DAYS * 86400 and cached["issues"]:
                return cached["issues"]
        except (OSError, ValueError, KeyError):
            pass
    page.goto(url.split("#")[0] + "#chapters", wait_until="domcontentloaded", timeout=60000)
    try:
        S.wait_for_reader(page, first_load, selector='a[href*="/reader/"]')
    except S.PWTimeout:
        save_debug(page, "series", url)
        return []
    page.evaluate(OPEN_CHAPTERS_JS)
    try:                                 # the issue list can appear a moment after the tab opens
        page.wait_for_function(f"() => ({SERIES_LINKS_JS})().length > 0", timeout=10000)
    except S.PWTimeout:
        save_debug(page, "series", url)
        return []
    save_debug(page, "series", url)
    found = {}
    for n in range(300):
        links = page.evaluate(SERIES_LINKS_JS)
        for title, href in links:
            found.setdefault(href, title)
        detail(f"      page {n + 1}: {len(found)} issues so far")
        first_href = links[0][1] if links else None
        if not page.evaluate(NEXT_PAGE_JS):
            break
        changed = False
        for _ in range(40):                 # wait up to ~10s for the next page of issues
            time.sleep(0.25)
            try:
                now = page.evaluate(SERIES_LINKS_JS)
            except Exception:               # page navigated mid-check
                continue
            if now and now[0][1] != first_href:
                changed = True
                break
        if not changed:
            break
    issues = [[t, h] for h, t in found.items()]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"time": time.time(), "url": url, "issues": issues}, f)
    return issues


def batcave_key(title):
    """Batcave issue link text -> (is_annual, number). Batcave writes them like:
         'Action Comics (1938) Issue #892'        -> (False, '892')
         'L.E.G.I.O.N. _Annual 5 Event: ...'      -> (True, '5')
         'Superman (1987-2006) Annual #3'         -> (True, '3')
         'Superman Annual (1987) Issue #2'        -> (True, '2')
         'Lex Luthor: The Unauthorized Biography Full'  (one-shot) -> (False, '1')"""
    m = re.search(r"annual\s*#?\s*('\d\d\b|\d+)", title, re.I)
    if m:
        n = m.group(1)
        return True, full_year(n[1:]) if n.startswith("'") else norm_num(n)
    annual_series = bool(re.search(r"annual", title, re.I))
    m = (re.search(r"\bissue\s*#?\s*(-?[0-9][0-9.,/]*[a-zA-Z]?|[0-9]*[a-zA-Z]+[0-9]*)", title, re.I)
         or re.search(r"#\s*(-?[0-9][0-9.,/]*[a-zA-Z]?)", title))
    if m:
        return annual_series, norm_num(m.group(1))
    return annual_series, "1"


PART_RE = re.compile(r"\s*\(\s*part\s*(\d+)\s*\)", re.I)


def part_of(title):
    """'book1 1 (Part 2)' -> 2: Batcave splits some big TPBs into parts."""
    m = PART_RE.search(title)
    return int(m.group(1)) if m else None


def issue_map(issues):
    """Batcave issue links -> entries with their annual flag and number. Entries with no number
    in their name ('Free Comic Book Day 2018 Overwatch', one-shots called 'Full') are 'named'."""
    entries = []
    for title, href in issues:
        annual, num = batcave_key(title)
        explicit = bool(re.search(r"annual\s*#?\s*'?\d|\bissue\s*#?\s*-?\w|#\s*-?\d", title, re.I))
        entries.append({"title": title, "href": href, "annual": annual, "num": num, "explicit": explicit,
                        "part": part_of(title)})
    words = [norm_title(e["title"]).split() for e in entries]
    prefix = []                       # words every entry starts with, e.g. "free comic book day 2018"
    for column in zip(*words) if len(words) > 1 else []:
        if len(set(column)) != 1:
            break
        prefix.append(column[0])
    return {"entries": entries, "prefix": set(prefix)}


def _expand_initials(words, other):
    """'tmnt' -> 'teenage mutant ninja turtles' when the other name has those words in a row."""
    out = []
    for w in words:
        hit = None
        if len(w) >= 3 and w.isalpha() and w not in other:
            for i in range(len(other)):
                for j in range(i + 2, min(len(other), i + 8) + 1):
                    if "".join(x[0] for x in other[i:j]) == w:
                        hit = other[i:j]
                        break
                if hit:
                    break
        out += hit or [w]
    return out


def _name_overlap(item_title, entry_title, ignore):
    a_words, b_words = norm_title(item_title).split(), norm_title(entry_title).split()
    a = set(_expand_initials(a_words, b_words)) - ignore      # Batcave: "FCBD 2019 TMNT"
    b = set(_expand_initials(b_words, a_words)) - ignore
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def lookup(item, group, bc):
    """The reader link on a scanned Batcave series for one list item, or None."""
    entries = bc["entries"]
    nums = [item["num"]]
    m = re.search(r"#\s*(\d+(?:\.\d+)?)\b", gtitle(group))
    if m and group.get("count") == 1 and item["num"] == "1" and m.group(1) != "1":
        # LoCG lists some books as one-shots named after the issue: "Marvel Graphic Novel #4 The New
        # Mutants" #1. On Batcave's "Marvel Graphic Novel" series that's #4, not #1 (Captain Marvel).
        nums = [norm_num(m.group(1))]     # never fall back to #1 here: that's a different book
    year = annual_year(gtitle(group)) if group.get("annual") else None
    if year:
        # "2018 Annual #1" is "_Annual 2018" on Batcave; never fall back to plain "Annual #1",
        # which is a different comic (Amazing Spider-Man Annual #1 is from 1964, not '96)
        nums = [year]
        # ...but some volumes number them 1, 2, 3 instead: Amazing Spider-Man (1999) "_Annual 1" is
        # LoCG's "Annual '99", "_Annual 2" is "Annual 2000". Only when no annual there is named by year.
        start = (group.get("match") or {}).get("year")
        annual_nums = [e["num"] for e in entries if e["annual"] and e["explicit"]]
        if (start and annual_nums and year.isdigit() and int(year) >= int(start)
                and not any(n.isdigit() and int(n) >= 1900 for n in annual_nums)):
            nums.append(str(int(year) - int(start) + 1))
    for num in nums:
        hits = [e for e in entries if e["explicit"] and e["annual"] == bool(group.get("annual")) and e["num"] == num]
        if len(hits) == 1:
            return hits[0]["href"]
        parted = [e for e in hits if e.get("part")]
        if len(parted) > 1:               # split into parts on Batcave: download all, as one file
            return [e["href"] for e in sorted(parted, key=lambda e: e["part"])]
        if len(hits) > 1:                 # same number twice (variants): take the closest name
            return max(hits, key=lambda e: _name_overlap(item["title"], e["title"], bc["prefix"]))["href"]
    named = [e for e in entries if not e["explicit"]]
    # match by name only for single specials ("Free Comic Book Day 2018: ... #1", "Lex Luthor: ..."),
    # never for numbered issues of a run like "Invasion! #2"
    special = "#" not in item["title"] or (group["count"] == 1 and item["num"] == "1")
    if named and special:
        # e.g. "Free Comic Book Day 2018: Mighty Morphin Power Rangers - Shattered Grid #1"
        #   -> "Free Comic Book Day 2018 Mighty Morphin Power Rangers"
        title = re.sub(r"\s*#\s*\S+\s*$", "", item["title"])
        best = max(named, key=lambda e: _name_overlap(title, e["title"], bc["prefix"]))
        if _name_overlap(title, best["title"], bc["prefix"]) >= 0.6:
            if best.get("part"):          # gather all parts with the same name
                base = PART_RE.sub("", best["title"]).strip()
                parts = [e for e in named if e.get("part") and PART_RE.sub("", e["title"]).strip() == base]
                return [e["href"] for e in sorted(parts, key=lambda e: e["part"])]
            return best["href"]
    if group["count"] == 1 and len(entries) == 1 and (not entries[0]["explicit"] or not year):
        # one-shot: whatever the single issue is ("... Full"); but a year-named annual must match its year
        return entries[0]["href"]
    return None


# =====================================================================
#  Download
# =====================================================================

def download(page, context, user_agent, plan, root):
    folder = os.path.join(root, safe_filename(plan["name"]))
    os.makedirs(folder, exist_ok=True)
    groups = plan["groups"]
    wanted = [it for it in plan["items"] if groups[it["series"]]["include"]]
    width = max(4, len(str(len(plan["items"]))))
    upcoming = [it for it in wanted if not is_out(it)]
    wanted = [it for it in wanted if is_out(it)]
    print(f"Saving {len(wanted)} issues to: {folder}"
          + (f"  ({len(upcoming)} more aren't out yet and will be picked up on a later run)" if upcoming else ""))

    # write the reading order up front so it's there even if you stop early
    with open(os.path.join(folder, "Reading order.txt"), "w", encoding="utf-8") as f:
        f.write(f"{plan['name']}\n\n")
        for it in wanted:
            f.write(f"{str(it['pos']).zfill(width)}  {it['title']}\n")

    maps, first = {}, True
    saved = skipped = 0
    missing, failed = [], []

    def done(title, ok, info):
        nonlocal saved
        if ok:
            saved += 1
            print(f"   saved {title} ({info})")
        else:
            failed.append(f"{title}  ({info})")
            problem(f"   Error: {title}: {info}")
    pipeline = S.Pipeline(context, user_agent, done)
    try:
        for n, it in enumerate(wanted, 1):
            if S.STOP_REQUESTED:
                print("Stopped by user")
                break
            g = groups[it["series"]]
            fname = f"{str(it['pos']).zfill(width)} - {safe_filename(it['title'])}{S.ARCHIVE_EXT}"
            path = S.batch_path(folder, fname, it["pos"], len(plan["items"]), width)
            if os.path.exists(path):
                skipped += 1
                continue
            if not g.get("match"):
                missing.append(f"{it['title']}  (series not matched on Batcave)")
                continue
            for m in [g["match"]] + (g.get("extra") or []):
                if m["url"] not in maps:
                    S.new_issue_log()
                    print(f"Scanning Batcave for {m['title']}...")
                    issues = batcave_series_issues(page, m["url"], first)
                    first = False
                    maps[m["url"]] = issue_map(issues)
                    detail(f"   {len(issues)} issues listed")
            url = g["match"]["url"]
            reader = lookup_with_extras(it, g, maps[url], [maps[m["url"]] for m in g.get("extra") or []])
            if not reader:
                missing.append(f"{it['title']}  (not on Batcave's {g['match']['title']} page)")
                continue
            S.new_issue_log()
            print(f"[{n}/{len(wanted)}] {it['title']}...")
            pipeline.add(it["title"], reader, path, page, first)
            first = False
    finally:
        pipeline.close()              # wait for the last downloads to finish

    report = os.path.join(folder, "Missing issues.txt")
    if missing or failed or upcoming:
        with open(report, "w", encoding="utf-8") as f:
            if missing:
                f.write("Not found on Batcave:\n" + "\n".join(missing) + "\n\n")
            if failed:
                f.write("Failed to download:\n" + "\n".join(failed) + "\n\n")
            if upcoming:
                f.write("Not out yet (run Download all again after they're released):\n"
                        + "\n".join(f"{it['title']}  ({release_text(it)})" for it in upcoming) + "\n")
    elif os.path.exists(report):
        os.remove(report)
    print(f"Done: {saved} downloaded, {skipped} already there, {len(missing)} not found, {len(failed)} failed"
          + (f", {len(upcoming)} not out yet" if upcoming else ""))
    if missing or failed or upcoming:
        print(f"   The list is in: {report}")
