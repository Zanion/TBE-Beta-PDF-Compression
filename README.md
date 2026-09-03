# TBE Beta Rulebook PDF Compression & Bookmarks

Shrinks the **beta** Broken Empires core rulebook PDF from about **726 MB to about 90 MB**,
without losing searchable text. Optionally adds bookmarks and clickable links, which the
beta PDF does not come with.

> **Read this first**
>
> - This is an **unofficial, community-made** script. It is not from the publisher.
> - It is written for **one specific file**: the beta `TBE_RPG_Core_Rulebook_v1.0.pdf`
>   (667 pages). It will refuse or misbehave on anything else.
> - It is provided **as-is, with no support**. Nobody is on the hook to fix it, answer
>   questions about it, or update it for future releases. I'm lazy.

## Support the game

**The Broken Empires RPG is available to pre-order from the publisher:**

### [**Pre-order the Core Rulebook**](https://thebrokenempiresrpg.com/products/the-broken-empires-rpg%E2%84%A2-core-rulebook)

This script exists only to make an already-purchased beta PDF easier to live with on a
tablet. If you haven't already, buy the book and support the wonderful people who made it.

## Setup

You need **Ghostscript** and **pikepdf**.

**Linux (Debian/Ubuntu)**
```
sudo apt install ghostscript python3-pikepdf
```

> I didn't bother testing it on Windows/MacOS but it will probably work if you do
> something like the following

**Windows**
1. Install Python from [python.org](https://www.python.org/downloads/)
2. Install Ghostscript from [ghostscript.com](https://ghostscript.com/releases/gsdnld.html)
3. Open terminal and run: `pip install pikepdf`

**macOS**
```
brew install ghostscript
pip3 install pikepdf
```

## Running it

Put `compress_tbe.py` in the same folder as your rulebook PDF, open a terminal in that
folder, and run one of these:

```
# just compress it
python3 compress_tbe.py TBE_RPG_Core_Rulebook_v1.0.pdf

# compress it and add bookmarks and links
python3 compress_tbe.py TBE_RPG_Core_Rulebook_v1.0.pdf --bookmarks

# only add bookmarks and links to a copy you already compressed
python3 compress_tbe.py TBE_RPG_Core_Rulebook_v1.0_screen.pdf --bookmarks-only
```

### Options

| Option | What it does |
|---|---|
| `--bookmarks` | Also add bookmarks, a clickable contents page, and clickable "see Chapter N" references. |
| `--bookmarks-only` | Add that navigation to a PDF you already compressed. Takes seconds. |
| `--dpi N` | How sharp the artwork is. Default `200`, good for reading on screen. Use `300` if you plan to print. Lower numbers give a smaller, softer file. |

Text is not affected by `--dpi`, it stays real and searchable either way. That option
only changes the resolution of the background artwork.

You can combine them:

```
python3 compress_tbe.py TBE_RPG_Core_Rulebook_v1.0.pdf --bookmarks --dpi 300
```

On Windows use `python` instead of `python3`.

If the PDF is sitting next to the script, you can leave the filename off entirely:

```
python3 compress_tbe.py --bookmarks
```

**Output:** `TBE_RPG_Core_Rulebook_v1.0_screen.pdf` in the same folder
(`..._bookmarked.pdf` if you used `--bookmarks-only`).

A non-default `--dpi` is added to the name, so trying different settings never overwrites
an earlier result. `--dpi 300` gives `..._screen_300dpi.pdf`. You can also pass your own
output name as a second argument, which overrides this.

## What to expect

- Takes roughly **15 minutes** on my machine. It prints progress as it goes.
- Needs about **1.5 GB of free disk space** for temporary files (cleaned up automatically)
  and around **6 GB of RAM** at its peak.
- `--bookmarks-only` is fast
- Running it twice is safe; bookmarks and links get replaced

## If something goes wrong

| Message | Fix |
|---|---|
| `pikepdf is not installed` | Run the `pip install pikepdf` step above. |
| `Ghostscript not found` | Install Ghostscript; on Windows make sure you rebooted or reopened the terminal afterwards. |
| `expected to find ... next to this script` | Put the script in the same folder as the PDF, or pass the full path to the PDF. |
| `this PDF has N pages but ...` | You pointed it at something other than the 667-page beta rulebook. |

