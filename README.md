# Comic Scraper

Downloads comics from batcave.biz and manga from mangadex.org into `.cbz` files that open in Calibre, ComicsPlusPlus and other comic readers.

## Install

Download **ComicScraper-Setup.exe** from the [Releases](../../releases) page and run it.
You don't need Python. You only need Google Chrome or Microsoft Edge, and Edge comes with Windows.

> Windows may show "Windows protected your PC" because the app isn't signed.
> Click **More info → Run anyway**.

## Use

1. Open a comic's first issue on batcave.biz and copy the reader link (`https://batcave.biz/reader/9264/60124`).
2. Paste it into Comic Scraper, pick the issue range and a name, then click **Start**.
3. If a "verify you are human" check appears, click it once. The browser hides again by itself.

Comics are saved to `Documents\Comics` by default (change it with **Browse…**).

## Manga

Paste a MangaDex title page (`https://mangadex.org/title/<id>/<name>`) or any chapter link into the
same box. The numbers are then chapter numbers (10 to 20 also gets 10.5), and **Get every issue**
gets the whole manga. A chapter link uses that chapter's language and translation group where it can.
MangaDex has an official API, so no browser opens for manga.

## Reading lists

The **Reading list** tab downloads a whole reading order into one folder, numbered in order
(`0001 - The Man of Steel #1.cbz`, `0002 - ...`).

1. Paste a League of Comic Geeks list link (e.g. `https://leagueofcomicgeeks.com/profile/magisterp/lists/20923/superman-post-crisis-1986-2002`),
   or tick **Or paste a list instead** and paste lines like `Superman #1-176, #0, #1m, Annual #1-12`
   (those are put in release order using League of Comic Geeks dates).
2. **Load list**, then **Find on Batcave**. Orange rows are probable matches worth checking, red rows
   weren't found: double-click a row to pick another result, search again, or paste the series page.
   Untick **Get** to skip a series (collected editions like Omnibus/TP start unticked).
3. **Download all**. Already-downloaded issues are skipped, so you can stop and continue later.
   `Reading order.txt` and `Missing issues.txt` are written into the folder.

## Project layout

```
app/            the program
  gui.py          the app window
  scraper.py      the engine that opens the reader and downloads pages
  readinglist.py  reading lists: League of Comic Geeks lists, Batcave matching, ordered downloads
  mangadex.py     manga from MangaDex (uses its API, no browser)
assets/         icon
installer/      Inno Setup script that makes ComicScraper-Setup.exe
scripts/        build.bat (build the installer), run.bat (run from source)
.github/        automatic build on GitHub
```

## For developers

- Run from source: `pip install -r requirements.txt`, then `python app/gui.py` (or double-click `scripts/run.bat`)
- Build locally: double-click `scripts/build.bat`. It needs [Inno Setup 6](https://jrsoftware.org/isdl.php) for the installer.
- Release: push a tag such as `v1.0.1`. GitHub Actions builds the installer and adds it to Releases.
