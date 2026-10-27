# Comic Scraper

Downloads comics from batcave.biz into `.cbz` files that open in Calibre, ComicsPlusPlus and other comic readers.

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

## For developers

- Run from source: `pip install -r requirements.txt`, then `python gui.py`
- Build locally: double-click `build.bat`. It needs [Inno Setup 6](https://jrsoftware.org/isdl.php) for the installer.
- Release: push a tag such as `v1.0.0`. GitHub Actions builds the installer and adds it to Releases.
