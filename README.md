# Comic Scraper

Downloads comics from batcave.biz and manga from mangadex.org and weebcentral.com into `.cbz` files that open in Calibre, ComicsPlusPlus and other comic readers.

## Android

Download **ComicScraper-Android.apk** from the [Releases](../../releases) page on your phone and open it.
Android will ask you to allow installing apps from your browser: allow it once.

The Android app does everything the Windows one does (single series, reading lists, MangaDex,
WeebCentral) with its own built-in browser instead of Chrome. The first time you start a download it
asks to "manage all files", so it can save comics in a normal `Comics` folder that reader apps like
ComicScreen, Mihon or Perfect Viewer can open. If Batcave shows a "verify you are human" check, the
browser appears: tap the check and the app goes back by itself.

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
Big downloads are split into subfolders of 100 (`001-100`, `101-200`, ...; reading lists use `0001-0100`)
so folders open quickly. Change the number, or set 0 for one folder, in **Settings → Files per folder**.
Annuals and TPBs/specials from **Get every issue** go in `Annuals` and `Other`.

## Search

Don't have a link? Open the **Search** tab, type a name and pick the sites to look in (Batcave,
MangaDex, WeebCentral). Double-click a result (tap it on Android) and it fills in the link and folder
name on the Single series tab. Then set the issue numbers, or tick **Get every issue**, and press Start.

## Manga

Paste a MangaDex title page (`https://mangadex.org/title/<id>/<name>`) or any chapter link into the
same box. The numbers are then chapter numbers (10 to 20 also gets 10.5), and **Get every issue**
gets the whole manga. A chapter link uses that chapter's language and translation group where it can.
MangaDex has an official API, so no browser opens for manga.

WeebCentral works the same way: paste a series page (`https://weebcentral.com/series/<id>/<name>`)
or any chapter link. Its pages are plain web pages, so Chrome only opens if the site asks you to
verify you're human. Side stories and other unnumbered chapters go in an `Other` folder with **Get every issue**.

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
  weebcentral.py  manga from WeebCentral (plain web pages; Chrome only if the site asks)
  search.py       the Search tab: looks up a name on every site
  android_bridge.py  runs the same engine in the Android app (its built-in browser instead of Chrome)
android/          the Android app (Kotlin screens; built by GitHub Actions)
assets/         icon
installer/      Inno Setup script that makes ComicScraper-Setup.exe
scripts/        build.bat (build the installer), run.bat (run from source)
.github/        automatic build on GitHub
```

## For developers

- Run from source: `pip install -r requirements.txt`, then `python app/gui.py` (or double-click `scripts/run.bat`)
- Build locally: double-click `scripts/build.bat`. It needs [Inno Setup 6](https://jrsoftware.org/isdl.php) for the installer.
- Release: push a tag such as `v1.0.1`. GitHub Actions builds the Windows installer and the Android APK
  and adds both to Releases. Run `git tag` first to see which numbers are taken.
- Android: built only by GitHub Actions (`android/`, Gradle + Chaquopy). Its signing key lives in four
  repository secrets (`ANDROID_KEYSTORE_B64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
  `ANDROID_KEY_PASSWORD`), never in the repo.
