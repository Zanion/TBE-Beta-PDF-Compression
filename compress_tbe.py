#!/usr/bin/env python3
"""
compress_tbe.py - shrink the Broken Empires core rulebook from ~726 MB to ~90 MB
                  without losing searchable text.

  Usage:  python3 compress_tbe.py TBE_RPG_Core_Rulebook_v1.0.pdf
          python3 compress_tbe.py TBE_RPG_Core_Rulebook_v1.0.pdf --bookmarks
          python3 compress_tbe.py already_compressed.pdf --bookmarks-only

          With no path it expects the rulebook sitting next to this script.

  Options:
          --bookmarks       also add bookmarks, a clickable contents page, and
                            clickable "see Chapter N" cross-references
          --bookmarks-only  skip the compression and only add that navigation,
                            e.g. to a copy you compressed earlier
          --dpi N           artwork resolution, 36-1200. Default 200, which suits
                            reading on a screen; use 300 if you intend to print it.
                            Lower is smaller and softer, higher is bigger and sharper.

  Writes: TBE_RPG_Core_Rulebook_v1.0_screen.pdf, alongside the input
          (~90 MB, ~13 minutes). Non-default settings are reflected in the name
          (..._screen_300dpi.pdf) so runs at different settings do not overwrite
          each other. Pass a second path to choose the name yourself.

WHY THE FILE IS SO BIG
    On 134 of the 667 pages, a black-and-white grain overlay sits over the page as live
    vector art rather than as an image. It has the look of a bitmap texture run through
    Image Trace. Those 134 pages carry 86% of the file.

    That is real vector data, so every ordinary PDF optimiser dutifully keeps all of
    it. Meanwhile only 24 MB of the original 726 MB is images. The parchment
    background is a placed JPEG of about 46 KB, repeated on every page and they are
    already low resolution. That is why image downsampling achieves nothing here, and
    why a plain Ghostscript pass can actually make the file bigger.

WHAT THIS DOES
    For the 134 affected pages only:
      1. Ghostscript renders the page WITHOUT its text  ->  the artwork, as a JPEG
      2. the page's content stream is filtered down to only its text
      3. the page is rebuilt as that JPEG, with the original text drawn on top
    The grain overlay was a bitmap before it was traced, so turning it back into one
    costs essentially nothing visually. The text never passes through a rasteriser or a
    font re-encoder, so it stays sharp and searchable. The other 533 pages are left
    completely untouched, and ~102 MB of leftover InDesign metadata is also stripped.

    Result, verified page by page against the original: all 667 pages extract identical
    text, and 533 of them render pixel-identical.

NAVIGATION (--bookmarks)
    The rulebook ships with no bookmarks and no links at all. This adds:
      * 207 bookmarks - 20 chapters plus Appendices and Resources, sections nested
      * 207 links over the printed contents pages (PDF pages 8-11)
      * 141 body cross-references made clickable, in 204 rectangles; where the text
        names a section ("Chapter 3 - Skills: Supporting Skills & Help") the link
        goes to that section, and references that wrap across a line get one
        rectangle per line

    Page numbers come from the book's own contents page. Printed page numbers run
    exactly 4 behind PDF page numbers, confirmed against the printed folio on 654 of
    the 668 pages. Re-running replaces this navigation rather than stacking it.

REQUIREMENTS
    Ghostscript and pikepdf.
      Debian/Ubuntu :  sudo apt install ghostscript python3-pikepdf
      macOS         :  brew install ghostscript && pip3 install pikepdf
      Windows       :  install Ghostscript from ghostscript.com, then: pip install pikepdf

    Needs ~1.5 GB of free scratch disk and ~6 GB of RAM at peak.
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

# ---- settings, tuned for this book on a screen-reading target -------------------------
# DPI is the default for --dpi; the others are not exposed as flags.
DPI          = 200      # artwork resolution. 300 if you intend to print it
JPEG_QUALITY = 80       # below ~72 the paper texture starts to smear
MIN_DPI, MAX_DPI = 36, 1200
THRESHOLD    = 500 * 1024   # only rebuild pages heavier than this
JOBS         = min(8, (os.cpu_count() or 4))
DEFAULT_NAME = 'TBE_RPG_Core_Rulebook_v1.0.pdf'
MIN_HEAVY_SHARE = 0.40  # below this, warn that the tool probably won't help
# --------------------------------------------------------------------------------------

try:
    import pikepdf
    from pikepdf import Array, Dictionary, Name, Operator, OutlineItem
except ImportError:
    sys.exit("pikepdf is not installed.\n"
             "  Debian/Ubuntu: sudo apt install python3-pikepdf\n"
             "  macOS/Windows: pip3 install pikepdf")


def find_ghostscript():
    for exe in ('gs', 'gswin64c', 'gswin32c'):
        if shutil.which(exe):
            return exe
    sys.exit("Ghostscript not found.\n"
             "  Debian/Ubuntu: sudo apt install ghostscript\n"
             "  macOS:         brew install ghostscript\n"
             "  Windows:       install from https://ghostscript.com/releases/")


# ======================================================================================
#  Filtering a page's content stream down to just its text.
#
#  Ghostscript can do this itself (-dFILTERVECTOR -dFILTERIMAGE) but must not be used
#  for it: its pdfwrite re-encodes fonts, and for the Type 1C fonts in this book with
#  custom encodings it drops the ToUnicode map. The text still LOOKS right but silently
#  stops being searchable - words like "armor" and "melee:" vanish from copy/paste.
#  Doing it ourselves keeps the original font objects untouched.
# ======================================================================================

PATH_CONSTRUCT = {'m', 'l', 'c', 'v', 'y', 'h', 're'}
PATH_PAINT     = {'S', 's', 'f', 'F', 'f*', 'B', 'B*', 'b', 'b*', 'n'}
CLIP           = {'W', 'W*'}
TEXT_SHOW      = {'Tj', 'TJ', "'", '"'}
# Marked content (BDC/BMC ... EMC) is treated as a nesting level too. If it is not,
# dropping an artwork block can leave its EMC behind as an orphan, which makes the file
# malformed ("Mismatched EMC operator") even though it still renders and extracts fine.
OPENERS        = {'q': 'Q', 'BT': 'ET', 'BDC': 'EMC', 'BMC': 'EMC'}
CLOSERS        = {'Q', 'ET', 'EMC'}


def _instr(op):
    return pikepdf.ContentStreamInstruction([], Operator(op))


_NOP   = _instr('n')
_CLOSE = {'Q': _instr('Q'), 'ET': _instr('ET'), 'EMC': _instr('EMC')}


class _Frame:
    """One nesting level (q/Q, BT/ET, BDC/EMC), plus the operators buffered inside it.

    `pending` holds operators seen at this level that have not been emitted yet.
    `emitted` records whether this level's opener has already been written out - once
    it has, the matching closer must be written too.
    """
    __slots__ = ('pending', 'emitted', 'closer')

    def __init__(self, opener_instr=None, closer=None):
        # the outermost level has no opener and is always considered open
        self.pending = [] if opener_instr is None else [opener_instr]
        self.emitted = opener_instr is None
        self.closer  = closer


def _resources_of(obj, parent):
    r = obj.get('/Resources') if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)) else None
    return r if r is not None else parent


def _rebuild_resources(old, used_xobjects):
    new = pikepdf.Dictionary()
    if old is not None:
        for k in ('/Font', '/ExtGState', '/ColorSpace', '/Pattern', '/Properties', '/ProcSet'):
            if k in old:
                new[k] = old[k]
    if used_xobjects:
        xd = pikepdf.Dictionary()
        for n, s in used_xobjects.items():
            xd[n] = s
        new['/XObject'] = xd
    return new


def filter_ops(container, resources, pdf, form_cache, depth=0):
    """Keep text and the graphics state it depends on; drop everything that paints."""
    try:
        ops = pikepdf.parse_content_stream(container)
    except Exception:
        return [], {}, False

    out, used, has_text = [], {}, False
    stack = [_Frame()]                       # stack[0] is the outermost level
    path_start, clipping = -1, False

    def flush():
        """Emit every buffered operator that is in scope, outermost level first."""
        for fr in stack:
            if fr.pending:
                out.extend(fr.pending)
                fr.pending = []
            fr.emitted = True

    # Iterate instruction objects directly. Unpacking `for operands, op in ops` converts
    # every operand into Python objects (compute heavy) and nearly
    # all of them belong to artwork we are about to throw away.
    for idx, instr in enumerate(ops):
        o = str(instr.operator)

        if o in PATH_CONSTRUCT:
            if path_start < 0:
                path_start = idx
            continue
        if o in CLIP:
            if path_start < 0:
                path_start = idx
            clipping = True
            continue
        if o in PATH_PAINT:
            if clipping:                     # keep the geometry only if it sets a clip,
                pending = stack[-1].pending
                pending.extend(ops[path_start:idx])
                pending.append(_NOP)         # ... and never paint it
            path_start, clipping = -1, False
            continue
        if o == 'sh' or o == 'INLINE IMAGE':
            continue

        if o in OPENERS:
            stack.append(_Frame(instr, OPENERS[o]))
            continue
        if o in CLOSERS:
            # close the nearest frame actually expecting this closer; an unmatched
            # closer is dropped rather than emitted as an orphan
            match = -1
            for k in range(len(stack) - 1, 0, -1):   # never past the outermost level
                if stack[k].closer == o:
                    match = k
                    break
            if match < 0:
                continue
            while len(stack) > match:
                fr = stack.pop()
                if fr.emitted:
                    out.extend(fr.pending)
                    out.append(_CLOSE[fr.closer])
            continue

        if o == 'Do':
            name = instr.operands[0]
            try:
                xo = resources['/XObject'][name]
            except Exception:
                continue
            if str(xo.get('/Subtype')) == '/Image' or depth >= 8:
                continue
            key = xo.objgen
            if key not in form_cache:
                sub_res = _resources_of(xo, resources)
                sub_ops, sub_used, sub_text = filter_ops(xo, sub_res, pdf, form_cache, depth + 1)
                if not sub_text:
                    form_cache[key] = None
                else:
                    nf = pikepdf.Stream(pdf, pikepdf.unparse_content_stream(sub_ops))
                    nf.Type, nf.Subtype = Name('/XObject'), Name('/Form')
                    for k in ('/BBox', '/Matrix', '/Group'):
                        if k in xo:
                            nf[k] = xo[k]
                    nf.Resources = _rebuild_resources(sub_res, sub_used)
                    form_cache[key] = nf
            nf = form_cache[key]
            if nf is None:
                continue
            used[str(name)] = nf
            has_text = True
            flush()
            out.append(instr)
            continue

        if o in TEXT_SHOW:
            has_text = True
            flush()
            out.append(instr)
            continue

        stack[-1].pending.append(instr)

    while len(stack) > 1:                    # tolerate unbalanced input
        fr = stack.pop()
        if fr.emitted:
            out.extend(fr.pending)
            out.append(_CLOSE[fr.closer])
    return out, used, has_text


# ======================================================================================

def jpeg_dims(b):
    """(width, height) from JPEG bytes."""
    i = 2
    while i < len(b) - 9:
        if b[i] != 0xFF:
            i += 1
            continue
        m = b[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack('>HH', b[i + 5:i + 9])
            return w, h
        if m == 0xD8 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xD9:
            break
        i += 2 + struct.unpack('>H', b[i + 2:i + 4])[0]
    raise ValueError('unreadable JPEG')


def page_bytes(page):
    total = 0
    c = page.get('/Contents')
    if c is not None:
        for s in (c if isinstance(c, pikepdf.Array) else [c]):
            try:
                total += len(s.read_raw_bytes())
            except Exception:
                pass
    try:
        xo = page.Resources.get('/XObject', {})
        for k in xo.keys():
            try:
                total += len(xo[k].read_raw_bytes())
            except Exception:
                pass
    except Exception:
        pass
    return total


# =======================================================================================
#  Pipeline steps.
#
#  Everything below runs once per page or once per document, so it is written for
#  clarity. Only filter_ops() above is on the hot path.
# =======================================================================================

def page_pdf_path(work, i):
    return os.path.join(work, f'p{i:05d}.pdf')


def artwork_path(work, i):
    return os.path.join(work, f'a{i:05d}.jpg')


def census(pdf):
    """Stored size of every page, which ones are heavy, and the share they account for."""
    weights = [(i, page_bytes(page)) for i, page in enumerate(pdf.pages)]
    heavy = [i for i, size in weights if size > THRESHOLD]
    counted = sum(size for _, size in weights)
    heavy_bytes = sum(size for _, size in weights if size > THRESHOLD)
    return weights, heavy, (heavy_bytes / counted if counted else 0.0)


def extract_single_pages(pdf, indices, work):
    """Write each heavy page out on its own, so Ghostscript can render it in isolation."""
    for i in indices:
        one = pikepdf.Pdf.new()
        one.pages.append(pdf.pages[i])
        one.save(page_pdf_path(work, i))
        one.close()


def render_artwork(gs, indices, work, on_progress, dpi=DPI):
    """Render each page WITHOUT its text; -dFILTERTEXT leaves the artwork alone."""
    def render_one(i):
        result = subprocess.run(
            [gs, '-dNOPAUSE', '-dBATCH', '-dQUIET', '-dSAFER', '-dFILTERTEXT',
             '-sDEVICE=jpeg', f'-r{dpi}', f'-dJPEGQ={JPEG_QUALITY}',
             f'-sOutputFile={artwork_path(work, i)}', page_pdf_path(work, i)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            detail = result.stdout.decode('utf8', 'replace').strip()
            raise RuntimeError(
                f"Ghostscript failed on page {i + 1} (exit code {result.returncode})."
                + (f"\n  It said: {detail[-400:]}" if detail else ""))

    done = 0
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        for _ in pool.map(render_one, indices):
            done += 1
            on_progress(done)


def image_xobject(pdf, jpg):
    """Wrap raw JPEG bytes as an image XObject. The JPEG is stored as-is, not re-encoded."""
    width, height = jpeg_dims(jpg)
    img = pikepdf.Stream(pdf, jpg)
    img.Type, img.Subtype = Name('/XObject'), Name('/Image')
    img.Width, img.Height, img.BitsPerComponent = width, height, 8
    # gs's jpeg device always emits 3-channel RGB; change this if you change the device
    img.ColorSpace = Name('/DeviceRGB')
    img.Filter = Name('/DCTDecode')
    return img


def rebuild_page(pdf, page, jpg, text_bytes, page_res, used_forms):
    """Replace a page with the artwork JPEG, and the page's own text drawn over it."""
    llx, lly, urx, ury = [float(v) for v in page.MediaBox]
    artwork = image_xobject(pdf, jpg)

    # the original fonts are carried across untouched - that is what keeps the text
    # searchable, and why Ghostscript is never allowed near this half of the page
    resources = _rebuild_resources(page_res, used_forms)
    xobjects = resources.get('/XObject')
    if xobjects is None:
        xobjects = pikepdf.Dictionary()
        resources['/XObject'] = xobjects
    xobjects['/ImArt'] = artwork

    draw_artwork = (f"q {urx-llx:.4f} 0 0 {ury-lly:.4f} "
                    f"{llx:.4f} {lly:.4f} cm /ImArt Do Q").encode()
    page.Contents = pikepdf.Array([pikepdf.Stream(pdf, draw_artwork),
                                   pikepdf.Stream(pdf, text_bytes)])
    page.Resources = resources
    if '/Group' in page:        # an opaque image plus text needs no transparency group
        del page['/Group']


def strip_private_metadata(pdf):
    """Drop per-object XMP and Illustrator/InDesign scratch data. Lossless."""
    root_meta = pdf.Root.get('/Metadata')
    freed = count = 0
    for obj in pdf.objects:
        if not isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            continue
        meta = obj.get('/Metadata')
        if meta is not None and (root_meta is None or meta.objgen != root_meta.objgen):
            try:
                freed += len(meta.read_raw_bytes())
            except Exception:
                pass
            del obj['/Metadata']
            count += 1
        if '/PieceInfo' in obj:
            del obj['/PieceInfo']
    return count, freed


# Marks the link annotations this script creates, so re-running replaces them
# instead of stacking duplicates on top of the contents page.
MARKER = Name('/TBEGeneratedTocLink')

# (level, title, pdf_page, toc_page, x0, y0, x1, y1)
#   pdf_page  - 1-based page the entry points at
#   toc_page  - 1-based page the entry is printed on, for the clickable rectangle
#   x0..y1    - the rectangle, in PDF units with y increasing upward
ENTRIES = [
    (1, '1 - Introduction', 16, 8, 96.0, 592.8, 297.0, 622.0),
    (2, 'What’s In This Book', 19, 8, 96.0, 577.6, 297.0, 594.7),
    (1, '2 - Core Mechanics', 20, 8, 96.0, 460.4, 297.0, 489.6),
    (2, 'Skill Modifiers', 22, 8, 96.0, 445.3, 297.0, 462.3),
    (2, 'Success Levels', 23, 8, 96.0, 431.3, 297.0, 448.3),
    (2, 'Critical Results', 23, 8, 96.0, 417.3, 297.0, 434.3),
    (2, 'Standard and Opposed Rolls', 24, 8, 96.0, 403.3, 297.0, 420.3),
    (2, 'Extended Rolls', 26, 8, 96.0, 389.3, 297.0, 406.3),
    (2, 'Favor', 30, 8, 96.0, 375.3, 297.0, 392.3),
    (2, 'Resolve', 30, 8, 96.0, 361.3, 297.0, 378.3),
    (1, '3 - Skills', 32, 8, 96.0, 244.0, 297.0, 273.3),
    (2, 'Combat Skills', 34, 8, 96.0, 228.9, 297.0, 245.9),
    (2, 'Adventuring Skills', 34, 8, 96.0, 214.9, 297.0, 231.9),
    (2, 'Social Skills', 35, 8, 96.0, 200.9, 297.0, 217.9),
    (2, 'Lore Skills', 36, 8, 96.0, 186.9, 297.0, 203.9),
    (2, 'Languages', 37, 8, 96.0, 172.9, 297.0, 189.9),
    (2, 'Magic Skills', 38, 8, 96.0, 158.9, 297.0, 175.9),
    (2, 'Skill Competency', 38, 8, 96.0, 144.9, 297.0, 161.9),
    (2, 'Finding Information', 40, 8, 96.0, 130.9, 297.0, 147.9),
    (2, 'Supporting Skills & Help', 41, 8, 96.0, 116.9, 297.0, 133.9),
    (1, '4 - Talents', 42, 8, 330.0, 592.8, 531.0, 622.0),
    (2, 'Combat Talents', 44, 8, 330.0, 577.6, 531.0, 594.7),
    (2, 'Combat Maneuver Talents', 48, 8, 330.0, 563.6, 531.0, 580.7),
    (2, 'Adventuring Talents', 49, 8, 330.0, 549.6, 531.0, 566.7),
    (2, 'Social Talents', 50, 8, 330.0, 535.6, 531.0, 552.7),
    (2, 'Lore Talents', 51, 8, 330.0, 521.6, 531.0, 538.7),
    (2, 'Magic Talents', 53, 8, 330.0, 507.6, 531.0, 524.7),
    (2, 'Miscellaneous Talents', 55, 8, 330.0, 493.6, 531.0, 510.7),
    (2, 'Non-Human Talents', 56, 8, 330.0, 479.6, 531.0, 496.7),
    (2, 'Expertise', 58, 8, 330.0, 465.6, 531.0, 482.7),
    (1, '5 - People of The Broken Empires', 60, 8, 330.0, 348.4, 531.0, 377.6),
    (2, 'Humans', 62, 8, 330.0, 333.3, 531.0, 350.3),
    (2, 'Dwarves', 64, 8, 330.0, 319.3, 531.0, 336.3),
    (2, 'Half-Orcs (Uthrak)', 66, 8, 330.0, 305.3, 531.0, 322.3),
    (2, 'Ogres', 68, 8, 330.0, 291.3, 531.0, 308.3),
    (2, 'Bolg Fiir', 70, 8, 330.0, 277.3, 531.0, 294.3),
    (2, 'The Replaced', 72, 8, 330.0, 263.3, 531.0, 280.3),
    (1, '6 - Goals', 74, 8, 330.0, 146.0, 531.0, 175.3),
    (2, 'Coming Up with Goals', 75, 8, 330.0, 130.9, 531.0, 147.9),
    (2, 'Individual & Shared Goals', 76, 8, 330.0, 116.9, 531.0, 133.9),
    (2, 'Specific, Proactive, Completable', 77, 8, 330.0, 102.9, 531.0, 119.9),
    (2, 'A Quick Method', 80, 8, 330.0, 88.9, 531.0, 105.9),
    (1, '7 - Character Creation', 82, 9, 72.0, 592.8, 273.0, 622.0),
    (2, 'Envision Concept & Assign Starting Skills', 84, 9, 72.0, 577.6, 273.0, 594.7),
    (2, 'Choose A Race', 85, 9, 72.0, 563.6, 273.0, 580.7),
    (2, 'Choose “Ability Scores”', 90, 9, 72.0, 549.6, 273.0, 566.7),
    (2, 'Determine Attributes', 91, 9, 72.0, 535.6, 273.0, 552.7),
    (2, 'Pick a Cultural Background', 93, 9, 72.0, 521.6, 273.0, 538.7),
    (2, 'Roll For Life Events', 95, 9, 72.0, 507.6, 273.0, 524.7),
    (2, 'Select a Previous Career', 106, 9, 72.0, 493.6, 273.0, 510.7),
    (2, 'Rounding Out', 112, 9, 72.0, 479.6, 273.0, 496.7),
    (2, 'Equip Your Character', 113, 9, 72.0, 465.6, 273.0, 482.7),
    (2, 'Assign Personality Traits', 114, 9, 72.0, 451.6, 273.0, 468.7),
    (2, 'Create Character Goals', 115, 9, 72.0, 437.6, 273.0, 454.7),
    (2, 'Status (Optional)', 118, 9, 72.0, 423.6, 273.0, 440.7),
    (1, '8 - Experience & Advancement', 124, 9, 72.0, 306.4, 273.0, 335.6),
    (2, 'Gaining XP', 126, 9, 72.0, 291.3, 273.0, 308.3),
    (2, 'Spending XP', 128, 9, 72.0, 277.3, 273.0, 294.3),
    (1, '9 - Equipment', 130, 9, 72.0, 160.0, 273.0, 189.3),
    (2, 'Coins & Haggling', 131, 9, 72.0, 144.9, 273.0, 161.9),
    (2, 'Supply Dice', 132, 9, 72.0, 130.9, 273.0, 147.9),
    (2, 'Encumbrance', 134, 9, 72.0, 116.9, 273.0, 133.9),
    (2, 'Weapons', 135, 9, 72.0, 102.9, 273.0, 119.9),
    (2, 'Shields', 144, 9, 72.0, 88.9, 273.0, 105.9),
    (2, 'Armor', 145, 9, 72.0, 74.9, 273.0, 91.9),
    (2, 'Miscellaneous Items', 149, 9, 72.0, 60.9, 273.0, 77.9),
    (1, '10 - Combat', 150, 9, 306.0, 592.8, 507.0, 622.0),
    (2, 'Combat Round Sequence', 151, 9, 306.0, 577.6, 507.0, 594.7),
    (2, 'Initiative', 152, 9, 306.0, 563.6, 507.0, 580.7),
    (2, 'Surprise', 153, 9, 306.0, 549.6, 507.0, 566.7),
    (2, 'Taking a Turn: Movement', 154, 9, 306.0, 535.6, 507.0, 552.7),
    (2, 'Zone Hazards', 155, 9, 306.0, 521.6, 507.0, 538.7),
    (2, 'Taking a Turn: Actions', 158, 9, 306.0, 507.6, 507.0, 524.7),
    (2, 'Critical Attacks and Defenses in Combat', 165, 9, 306.0, 493.6, 507.0, 510.7),
    (2, 'Combat Maneuvers', 166, 9, 306.0, 479.6, 507.0, 496.7),
    (2, 'Situational Rules', 170, 9, 306.0, 465.6, 507.0, 482.7),
    (2, 'Quick Combat Resolution', 173, 9, 306.0, 451.6, 507.0, 468.7),
    (1, '11 - Wounds, Healing, & Perils', 174, 9, 306.0, 334.4, 507.0, 363.6),
    (2, 'Marking a Wound', 176, 9, 306.0, 319.3, 507.0, 336.3),
    (2, 'The Wound Die', 177, 9, 306.0, 305.3, 507.0, 322.3),
    (2, 'Shock', 177, 9, 306.0, 291.3, 507.0, 308.3),
    (2, 'Impaired', 177, 9, 306.0, 277.3, 507.0, 294.3),
    (2, 'Lethality Level', 178, 9, 306.0, 263.3, 507.0, 280.3),
    (2, 'Dying', 178, 9, 306.0, 249.3, 507.0, 266.3),
    (2, 'Death Threshold', 178, 9, 306.0, 235.3, 507.0, 252.3),
    (2, 'Healing Wounds', 180, 9, 306.0, 221.3, 507.0, 238.3),
    (2, 'Infection', 183, 9, 306.0, 207.3, 507.0, 224.3),
    (2, 'Rest & Recovery', 186, 9, 306.0, 193.3, 507.0, 210.3),
    (2, 'Magical Healing', 188, 9, 306.0, 179.3, 507.0, 196.3),
    (2, 'Other Perils', 192, 9, 306.0, 165.3, 507.0, 182.3),
    (1, '12 - Travel', 200, 10, 96.0, 635.1, 297.0, 654.6),
    (2, 'The Journey', 203, 10, 96.0, 617.6, 297.0, 634.7),
    (2, 'Planning the Route', 204, 10, 96.0, 603.6, 297.0, 620.7),
    (2, 'Travel Lore', 205, 10, 96.0, 589.6, 297.0, 606.7),
    (2, 'The Guide', 206, 10, 96.0, 575.6, 297.0, 592.7),
    (2, 'The Quartermaster', 210, 10, 96.0, 561.6, 297.0, 578.7),
    (2, 'Check for Infection', 213, 10, 96.0, 547.6, 297.0, 564.7),
    (2, 'Describe the Leg', 214, 10, 96.0, 533.6, 297.0, 550.7),
    (2, 'The Scout', 215, 10, 96.0, 519.6, 297.0, 536.7),
    (2, 'Change to Daily Time or Begin A New Leg', 216, 10, 96.0, 505.6, 297.0, 522.7),
    (2, 'The Hexmarch', 222, 10, 96.0, 491.6, 297.0, 508.7),
    (2, 'Events', 226, 10, 96.0, 477.6, 297.0, 494.7),
    (2, 'The Event Tables', 232, 10, 96.0, 463.6, 297.0, 480.7),
    (2, 'Premade Events', 248, 10, 96.0, 449.6, 297.0, 466.7),
    (1, '13 - Intrigue', 252, 10, 96.0, 332.7, 297.0, 352.2),
    (2, 'Social Encounters', 253, 10, 96.0, 315.3, 297.0, 332.3),
    (2, 'Static Encounters', 256, 10, 96.0, 301.3, 297.0, 318.3),
    (2, 'Competitive Encounters', 258, 10, 96.0, 287.3, 297.0, 304.3),
    (2, 'Succeeding in a Social Encounter', 259, 10, 96.0, 273.3, 297.0, 290.3),
    (2, 'Leverage', 261, 10, 96.0, 259.3, 297.0, 276.3),
    (2, 'Special Reactions', 261, 10, 96.0, 245.3, 297.0, 262.3),
    (2, 'Escalation', 263, 10, 96.0, 231.3, 297.0, 248.3),
    (2, 'Formal Competitive Social Encounters', 266, 10, 96.0, 217.3, 297.0, 234.3),
    (2, 'Investigations and Mysteries', 272, 10, 96.0, 203.3, 297.0, 220.3),
    (2, 'Chases', 274, 10, 96.0, 189.3, 297.0, 206.3),
    (1, '14 - Weave Magic', 276, 10, 330.0, 635.1, 531.0, 654.6),
    (2, 'Foundations of Magic', 278, 10, 330.0, 617.6, 531.0, 634.7),
    (2, 'The Bounded Laws of Magic', 280, 10, 330.0, 603.6, 531.0, 620.7),
    (2, 'Binding the Strand', 285, 10, 330.0, 589.6, 531.0, 606.7),
    (2, 'Making the Casting Roll', 287, 10, 330.0, 575.6, 531.0, 592.7),
    (2, 'Shaping Element Guidelines', 289, 10, 330.0, 561.6, 531.0, 578.7),
    (2, 'The Weave Reaction Table', 306, 10, 330.0, 547.6, 531.0, 564.7),
    (2, 'Threads', 310, 10, 330.0, 533.6, 531.0, 550.7),
    (2, 'Fraying', 312, 10, 330.0, 519.6, 531.0, 536.7),
    (2, 'Advanced Magic', 316, 10, 330.0, 505.6, 531.0, 522.7),
    (2, 'Rituals', 320, 10, 330.0, 491.6, 531.0, 508.7),
    (2, 'Summoning', 324, 10, 330.0, 477.6, 531.0, 494.7),
    (2, 'Enchantments', 327, 10, 330.0, 463.6, 531.0, 480.7),
    (2, 'Alchemy', 330, 10, 330.0, 449.6, 531.0, 466.7),
    (2, 'Spell Examples', 332, 10, 330.0, 435.6, 531.0, 452.7),
    (1, '15 - Divine Magic', 348, 10, 330.0, 320.7, 531.0, 340.2),
    (2, 'Piety', 349, 10, 330.0, 303.3, 531.0, 320.3),
    (2, 'Domains', 351, 10, 330.0, 289.3, 531.0, 306.3),
    (2, 'Asking for a Miracle', 351, 10, 330.0, 275.3, 531.0, 292.3),
    (2, 'Miracle Resistance', 354, 10, 330.0, 261.3, 531.0, 278.3),
    (2, 'Increasing Piety', 354, 10, 330.0, 247.3, 531.0, 264.3),
    (2, 'Cast Out', 355, 10, 330.0, 233.3, 531.0, 250.3),
    (2, 'Domain List & Miracles', 358, 10, 330.0, 219.3, 531.0, 236.3),
    (1, '16 - The Gods', 370, 10, 330.0, 104.3, 531.0, 123.8),
    (2, 'Utris', 374, 10, 330.0, 86.9, 531.0, 103.9),
    (2, 'Ilosia', 378, 10, 330.0, 72.9, 531.0, 89.9),
    (2, 'Morrudann', 382, 10, 330.0, 58.9, 531.0, 75.9),
    (2, 'Devona', 386, 11, 72.0, 717.8, 273.0, 734.9),
    (2, 'Latheriel', 390, 11, 72.0, 703.8, 273.0, 720.9),
    (2, 'The Spinners', 394, 11, 72.0, 689.8, 273.0, 706.9),
    (2, 'The Elements', 398, 11, 72.0, 675.8, 273.0, 692.9),
    (2, 'Omnus the One - The New God', 402, 11, 72.0, 661.8, 273.0, 678.9),
    (2, 'The Deep Cold Gods of Mountainholme', 402, 11, 72.0, 647.8, 273.0, 664.9),
    (1, '17 - Gamemastering', 404, 11, 72.0, 532.9, 273.0, 552.4),
    (2, 'The Heart of the Game', 406, 11, 72.0, 515.4, 273.0, 532.5),
    (2, 'The Shape of Play', 408, 11, 72.0, 501.4, 273.0, 518.5),
    (2, 'Framing the Campaign', 412, 11, 72.0, 487.4, 273.0, 504.5),
    (2, 'Running the Game', 415, 11, 72.0, 473.4, 273.0, 490.5),
    (2, 'Characters and Relationships', 423, 11, 72.0, 459.4, 273.0, 476.5),
    (2, 'Investigations, Clues, and Bottlenecks', 427, 11, 72.0, 445.4, 273.0, 462.5),
    (2, 'Managing Time and Travel', 428, 11, 72.0, 431.4, 273.0, 448.5),
    (2, 'World-Building and Consistency', 432, 11, 72.0, 417.4, 273.0, 434.5),
    (2, 'Behind the Screen', 435, 11, 72.0, 403.4, 273.0, 420.5),
    (1, '18 - Bestiary', 440, 11, 72.0, 288.5, 273.0, 308.0),
    (2, 'Enemy Difficulty', 441, 11, 72.0, 271.1, 273.0, 288.1),
    (2, 'Size', 443, 11, 72.0, 257.1, 273.0, 274.1),
    (2, 'Ferocity', 444, 11, 72.0, 243.1, 273.0, 260.1),
    (2, 'Creature Special Abilities', 445, 11, 72.0, 229.1, 273.0, 246.1),
    (2, 'Creatures of The Broken Empires', 448, 11, 72.0, 215.1, 273.0, 232.1),
    (2, 'NPC Traits', 482, 11, 72.0, 201.1, 273.0, 218.1),
    (1, '19 - Solo Roleplaying', 484, 11, 72.0, 86.1, 273.0, 105.6),
    (2, 'Solo Play General Principles', 486, 11, 72.0, 68.7, 273.0, 85.7),
    (2, 'Ask the Weave', 490, 11, 72.0, 54.7, 273.0, 71.7),
    (2, 'Random Events', 491, 11, 306.0, 718.7, 507.0, 735.7),
    (2, 'Empires List', 494, 11, 306.0, 704.7, 507.0, 721.7),
    (2, 'Non-Player Characters', 495, 11, 306.0, 690.7, 507.0, 707.7),
    (1, '20 - The Setting', 500, 11, 306.0, 575.7, 507.0, 595.2),
    (2, 'Timeline of Significant Events', 502, 11, 306.0, 558.3, 507.0, 575.4),
    (2, 'Angevarre', 504, 11, 306.0, 544.3, 507.0, 561.4),
    (2, 'Ansharir', 510, 11, 306.0, 530.3, 507.0, 547.4),
    (2, 'The Baronies', 514, 11, 306.0, 516.3, 507.0, 533.4),
    (2, 'Drangia', 518, 11, 306.0, 502.3, 507.0, 519.4),
    (2, 'Dunblaine', 522, 11, 306.0, 488.3, 507.0, 505.4),
    (2, 'Haedravik', 526, 11, 306.0, 474.3, 507.0, 491.4),
    (2, 'Hoenvall', 530, 11, 306.0, 460.3, 507.0, 477.4),
    (2, 'The Ironlands', 534, 11, 306.0, 446.3, 507.0, 463.4),
    (2, 'Mountainholme', 536, 11, 306.0, 432.3, 507.0, 449.4),
    (2, 'Old Vestria', 538, 11, 306.0, 418.3, 507.0, 435.4),
    (2, 'The Red Wastes', 544, 11, 306.0, 404.3, 507.0, 421.4),
    (2, 'Sattagoya', 548, 11, 306.0, 390.3, 507.0, 407.4),
    (2, 'Serpent’s Teeth Isles', 552, 11, 306.0, 376.3, 507.0, 393.4),
    (2, 'The Shroud', 556, 11, 306.0, 362.3, 507.0, 379.4),
    (2, 'The Southern Peninsula', 560, 11, 306.0, 348.3, 507.0, 365.4),
    (2, 'The Westlands', 564, 11, 306.0, 334.3, 507.0, 351.4),
    (2, 'Tical Dondala', 570, 11, 306.0, 320.3, 507.0, 337.4),
    (2, 'Thessia', 576, 11, 306.0, 306.3, 507.0, 323.4),
    (2, 'Vieksgrad', 580, 11, 306.0, 292.3, 507.0, 309.4),
    (2, 'Organizations', 586, 11, 306.0, 278.3, 507.0, 295.4),
    (2, 'The Constellations', 590, 11, 306.0, 264.3, 507.0, 281.4),
    (2, 'The Old Vestrian Calendar', 591, 11, 306.0, 250.3, 507.0, 267.4),
    (1, 'Appendices', 592, 11, 306.0, 225.7, 507.0, 245.2),
    (2, 'A: Mass Combat', 592, 11, 306.0, 208.3, 507.0, 225.4),
    (2, 'B: Naval Combat', 600, 11, 306.0, 194.3, 507.0, 211.4),
    (2, 'C: Personality Traits', 606, 11, 306.0, 180.3, 507.0, 197.4),
    (2, 'D: Name Tables', 608, 11, 306.0, 166.3, 507.0, 183.4),
    (2, 'E: Treasure', 646, 11, 306.0, 152.3, 507.0, 169.4),
    (1, 'Resources', 654, 11, 306.0, 127.7, 507.0, 147.2),
    (2, 'Empires List', 654, 11, 306.0, 110.3, 507.0, 127.4),
    (2, 'Spellweaver Style List', 655, 11, 306.0, 96.3, 507.0, 113.4),
    (2, 'Character Sheet', 656, 11, 306.0, 82.3, 507.0, 99.4),
    (2, 'Index', 658, 11, 306.0, 68.3, 507.0, 85.4),
    (2, 'Table Index', 661, 11, 306.0, 54.3, 507.0, 71.4),
]


# (source_page, target_page, printed text, rectangles)
#   One rectangle per line of text the reference occupies, so references that wrap
#   across a line break are fully clickable.
XREFS = [
    (24, 150, 'Chapter 10 - Combat',
     ((401.8, 478.8, 482.9, 492.1),)),
    (28, 41, 'Chapter 3 - Skills: Supporting Skills & Help',
     ((486.1, 612.5, 530.4, 623.6), (342, 598.5, 482.3, 609.6),)),
    (28, 276, 'Chapter 14 - Weave Magic',
     ((484.7, 198.8, 522.2, 212.1), (342, 184.8, 417.7, 198.1),)),
    (29, 252, 'Chapter 13 - Intrigue',
     ((160.1, 480, 244.8, 490.2),)),
    (29, 491, 'Chapter 19 - Solo Roleplaying: Random Events',
     ((148.6, 409, 271, 419.2), (72, 395, 134.2, 405.2),)),
    (30, 261, 'Chapter 13 - Intrigue: Leverage',
     ((96, 548.8, 215.5, 562.1),)),
    (30, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((385.8, 545.7, 506.1, 559), (366, 531.7, 396.7, 545),)),
    (30, 276, 'Chapter 14 - Weave Magic',
     ((96, 142.8, 197.4, 156.1),)),
    (31, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((325.7, 674.8, 474.6, 688.1),)),
    (31, 276, 'Chapter 14 - Weave Magic',
     ((72, 366.8, 171.2, 380.1),)),
    (33, 276, 'Chapter 14 - Weave Magic',
     ((115.9, 191.3, 216.8, 204.6),)),
    (33, 348, 'Chapter 15 - Divine Magic',
     ((237, 191.3, 336.4, 204.6),)),
    (36, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((433.1, 86.8, 537, 100.1), (330, 72.8, 377, 86.1),)),
    (38, 276, 'Chapter 14 - Weave Magic',
     ((508.8, 464.8, 542.1, 478.1), (330, 450.8, 398.3, 464.1),)),
    (38, 348, 'Chapter 15 - Divine Magic',
     ((418.1, 450.8, 519.9, 464.1),)),
    (39, 42, 'Chapter 4 - Talents',
     ((394.9, 694.7, 472.8, 708),)),
    (39, 20, 'Chapter 2 - Core Mechanics',
     ((379, 573.8, 497.3, 584.8),)),
    (39, 20, 'Chapter 2 - Core Mechanics',
     ((234.7, 245.9, 257.2, 256.9), (84, 231.9, 180.5, 242.9),)),
    (40, 20, 'Chapter 2 - Core Mechanics',
     ((201.5, 632.8, 303.3, 646.1),)),
    (41, 42, 'Chapter 4 - Talents',
     ((390.2, 202.8, 471.9, 216.1),)),
    (41, 124, 'Chapter 8 - Experience & Advancement',
     ((334.7, 160.8, 505.5, 174.1),)),
    (51, 131, 'Chapter 9 - Equipment: Coins & Haggling',
     ((170.1, 184.8, 284.2, 198.1), (72, 170.8, 116.7, 184.1),)),
    (54, 276, 'Chapter 14 - Weave Magic',
     ((232.3, 128.8, 308.2, 142.1), (96, 114.8, 119.7, 128.1),)),
    (55, 124, 'Chapter 8 - Experience & Advancement',
     ((167.3, 114.8, 279, 128.1), (72, 100.8, 112.7, 114.1),)),
    (56, 276, 'Chapter 14 - Weave Magic',
     ((141.2, 240.8, 242.6, 254.1),)),
    (74, 124, 'Chapter 8 - Experience & Advancement',
     ((243, 170.8, 375.1, 184.1), (66, 156.8, 85.1, 170.1),)),
    (75, 82, 'Chapter 7 - Character Creation',
     ((396.1, 282.3, 516, 295.6),)),
    (76, 82, 'Chapter 7 - Character Creation',
     ((115, 590.8, 231, 604.1),)),
    (77, 124, 'Chapter 8 - Experience & Advancement',
     ((182.7, 492.8, 284.2, 506.1), (72, 478.8, 123.9, 492.1),)),
    (85, 56, 'Chapter 4 - Talents: Non-Human Talents',
     ((306, 366.8, 455.4, 380.1),)),
    (85, 42, 'Chapter 4 - Talents',
     ((466.1, 328.8, 526.7, 342.1), (318, 314.8, 335.7, 328.1),)),
    (96, 440, 'Chapter 18 - Bestiary',
     ((188.8, 674.8, 271.6, 688.1),)),
    (96, 484, 'Chapter 19 - Solo Roleplaying',
     ((358.7, 186.8, 487.3, 200.1),)),
    (110, 310, 'Chapter 14 - Weave Magic: Threads',
     ((231.5, 611.8, 282.3, 625.2), (120, 597.8, 205.8, 611.2),)),
    (112, 128, 'Chapter 8 - Experience & Advancement: Spending XP',
     ((459.4, 497.4, 516.1, 510.7), (366, 483.4, 509.1, 496.7),)),
    (113, 145, 'Chapter 9 - Equipment: Armor',
     ((257.8, 391.7, 278.9, 405.4), (70, 377.7, 169.1, 391.4),)),
    (115, 74, 'Chapter 6 - Goals',
     ((250.8, 576.8, 284.1, 590.1), (72, 562.8, 105.6, 576.1),)),
    (125, 74, 'Chapter 6 - Goals',
     ((441.4, 282.8, 509.3, 296.1),)),
    (128, 58, 'Chapter 4 - Talents: Expertise',
     ((516.4, 646.8, 537, 660.1), (330, 632.8, 424.1, 646.1),)),
    (129, 349, 'Chapter 15 - Divine Magic: Piety',
     ((106.8, 198.8, 232.8, 212.1),)),
    (132, 82, 'Chapter 7 - Character Creation',
     ((223.7, 254.8, 308.2, 268.1), (96, 240.8, 129.4, 254.1),)),
    (133, 200, 'Chapter 12 - Travel',
     ((444.7, 520.8, 512.8, 534.1), (306, 506.8, 312.3, 520.1),)),
    (135, 170, 'Chapter 10 - Combat: Situational Rules',
     ((492.5, 226.8, 513.1, 240.1), (306, 212.8, 437.2, 226.1),)),
    (135, 166, 'Chapter 10 - Combat: Combat Maneuvers',
     ((464.9, 128.8, 518.1, 142.1), (306, 114.8, 415.3, 128.1),)),
    (144, 166, 'Chapter 10 - Combat: Combat Maneuvers',
     ((433.1, 618.8, 537, 632.1), (330, 604.8, 386.2, 618.1),)),
    (146, 276, 'Chapter 14 - Weave Magic',
     ((387.7, 507.9, 498.9, 518.9),)),
    (151, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((430.6, 128.2, 517.6, 141.9), (327, 114.2, 392.3, 127.9),)),
    (152, 145, 'Chapter 9 - Equipment: Armor',
     ((412.7, 429.8, 519.3, 443.1), (354, 415.8, 369, 429.1),)),
    (161, 276, 'Chapter 14 - Weave Magic',
     ((72, 422.8, 172.9, 436.1),)),
    (161, 348, 'Chapter 15 - Divine Magic',
     ((125.8, 408.8, 227.5, 422.1),)),
    (161, 276, 'Chapter 14 - Weave Magic',
     ((115.9, 212.8, 215, 226.1),)),
    (164, 130, 'Chapter 9 - Equipment',
     ((282.5, 450.8, 303, 464.1), (96, 436.8, 162.2, 450.1),)),
    (166, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((96, 184.8, 247.6, 198.1),)),
    (170, 440, 'Chapter 18 - Bestiary',
     ((96, 282.8, 178.3, 296.1),)),
    (171, 130, 'Chapter 9 - Equipment',
     ((166.4, 502.8, 265.7, 516.1),)),
    (171, 440, 'Chapter 18 - Bestiary',
     ((466.4, 422.8, 518.1, 436.1), (306, 408.8, 337.8, 422.1),)),
    (172, 276, 'Chapter 14 - Weave Magic',
     ((461, 254.8, 542.1, 268.1), (330, 240.8, 353.7, 254.1),)),
    (172, 200, 'Chapter 12 - Travel',
     ((330, 86.8, 404.2, 100.1),)),
    (174, 150, 'Chapter 10 - Combat',
     ((157.7, 322.2, 265.2, 340),)),
    (178, 440, 'Chapter 18 - Bestiary',
     ((200.6, 209.8, 292, 223.1),)),
    (179, 130, 'Chapter 9 - Equipment',
     ((214, 646.8, 279.2, 660.1), (72, 632.8, 90.6, 646.1),)),
    (183, 200, 'Chapter 12: Travel',
     ((213.2, 562.8, 278.8, 576.1), (72, 548.8, 78.3, 562.1),)),
    (184, 276, 'Chapter 14 - Weave Magic',
     ((459.6, 660.8, 542.2, 674.1), (330, 646.8, 353.5, 660.1),)),
    (184, 348, 'Chapter 15 - Divine Magic',
     ((374.2, 646.8, 475.2, 660.1),)),
    (186, 200, 'Chapter 12 - Travel',
     ((150.9, 492.8, 225.1, 506.1),)),
    (186, 200, 'Chapter 12 - Travel',
     ((459.3, 240.8, 533.3, 254.1),)),
    (187, 200, 'Chapter 12 - Travel',
     ((250.8, 506.8, 284.1, 520.1), (72, 492.8, 113.3, 506.1),)),
    (188, 348, 'Chapter 15 - Divine Magic',
     ((481.4, 525.9, 518.3, 536.9), (342, 511.9, 415.9, 522.9),)),
    (188, 276, 'Chapter 14 - Weave Magic',
     ((176.1, 174.3, 282, 187.6),)),
    (192, 155, 'Chapter 10 - Combat: Zone Hazards',
     ((254, 282.8, 308.1, 296.1), (96, 268.8, 187, 282.1),)),
    (198, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((420.8, 280.8, 542.2, 294.1), (330, 266.8, 360.3, 280.1),)),
    (199, 155, 'Chapter 10 - Combat: Zone Hazards',
     ((363.9, 240.8, 502.2, 254.1),)),
    (211, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((152.2, 170.8, 284.7, 184.1), (72, 156.8, 93.1, 170.1),)),
    (213, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((194.1, 128.8, 284.1, 142.1), (72, 114.8, 137.2, 128.1),)),
    (215, 153, 'Chapter 10 – Combat: Surprise',
     ((127.3, 192.8, 243.7, 206.1),)),
    (217, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((428.7, 660.8, 518.2, 674.1), (306, 646.8, 374.2, 660.1),)),
    (217, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((327.8, 562.8, 486.7, 576.1),)),
    (217, 130, 'Chapter 9 - Equipment',
     ((72, 548.8, 158.8, 562.1),)),
    (217, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((238.2, 506.8, 284.1, 520.1), (72, 492.8, 178, 506.1),)),
    (217, 440, 'Chapter 18 - Bestiary',
     ((325.9, 422.8, 408.2, 436.1),)),
    (218, 150, 'Chapter 10 - Combat',
     ((177.4, 436.8, 258.4, 450.1),)),
    (220, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((330, 646.8, 481.6, 660.1),)),
    (220, 186, 'Chapter 11 - Wounds, Healing, & Perils: Rest & Recovery',
     ((406.2, 534.8, 542.1, 548.1), (330, 520.8, 420.5, 534.1),)),
    (220, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((432.7, 464.8, 537, 478.1), (330, 450.8, 377, 464.1),)),
    (229, 440, 'Chapter 18 - Bestiary',
     ((330, 618.8, 415.2, 632.1),)),
    (231, 252, 'Chapter 13 - Intrigue',
     ((325.3, 646.8, 405.5, 660.1),)),
    (231, 440, 'Chapter 18 - Bestiary',
     ((375.3, 422.8, 459.4, 436.1),)),
    (231, 440, 'Chapter 18 - Bestiary',
     ((435.7, 352.8, 516, 366.1),)),
    (237, 440, 'Chapter 18 - Bestiary',
     ((216.8, 51.4, 297, 64.4),)),
    (241, 440, 'Chapter 18 - Bestiary',
     ((216.8, 393.4, 297, 406.4),)),
    (242, 440, 'Chapter 18 - Bestiary',
     ((264.8, 393.4, 345, 406.4),)),
    (243, 440, 'Chapter 18 - Bestiary',
     ((216.8, 393.4, 297, 406.4),)),
    (247, 20, 'Chapter 2 - Core Mechanics',
     ((324.7, 564.7, 440.5, 575.8),)),
    (247, 252, 'Chapter 13 - Intrigue',
     ((134.8, 253.7, 221.9, 264.8),)),
    (248, 440, 'Chapter 18 - Bestiary',
     ((255.9, 244.8, 308.1, 258.1), (96, 230.8, 127.7, 244.1),)),
    (262, 440, 'Chapter 18 - Bestiary',
     ((115.5, 492.8, 197.3, 506.1),)),
    (272, 484, 'Chapter 19 - Solo Roleplaying',
     ((177.9, 86.8, 295.9, 100.1),)),
    (291, 192, 'Chapter 11 - Wounds, Healing, & Perils: Other Perils',
     ((409.6, 657.8, 507.3, 671.1), (318, 643.8, 448.3, 657.1),)),
    (294, 150, 'Chapter 10 - Combat',
     ((216.7, 365.8, 272.1, 379.1), (132, 351.8, 162.5, 365.1),)),
    (294, 192, 'Chapter 11 - Wounds, Healing, & Perils: Other Perils',
     ((354, 118.6, 524.1, 131.9), (354, 104.6, 399.6, 117.9),)),
    (296, 192, 'Chapter 11 - Wounds, Healing, & Perils: Other Perils',
     ((244.3, 303.8, 290.1, 317.1), (120, 289.8, 281.3, 303.1),)),
    (299, 443, 'Chapter 18 - Bestiary: Size',
     ((362.7, 646.8, 465.3, 660.1),)),
    (307, 174, 'Chapter 11- Wounds, Healing, & Perils',
     ((125.2, 268.8, 279.3, 282.1),)),
    (308, 440, 'Chapter 18 - Bestiary',
     ((221, 296.8, 303.3, 310.1),)),
    (330, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((96, 520.8, 247.6, 534.1),)),
    (336, 192, 'Chapter 11 - Wounds, Healing, & Perils: Other Perils',
     ((462.5, 310.7, 542, 323), (366, 300.7, 472.1, 313),)),
    (354, 370, 'Chapter 16 - The Gods',
     ((352.3, 212.8, 444.6, 226.1),)),
    (354, 276, 'Chapter 14 - Weave Magic',
     ((207.2, 86.8, 303.4, 100.1),)),
    (355, 174, 'Chapter 11 - Wounds, Healing & Perils',
     ((89.3, 333.4, 262.4, 346.7),)),
    (360, 192, 'Chapter 11 - Wounds, Healing, & Perils: Other Perils',
     ((292.7, 261, 406.6, 273.3), (275, 249.1, 337.1, 261.3),)),
    (364, 276, 'Chapter 14 - Weave Magic',
     ((152.8, 457.1, 244.3, 469.3),)),
    (364, 276, 'Chapter 14 - Weave Magic',
     ((149.7, 343.1, 243.3, 355.4),)),
    (372, 348, 'Chapter 15 - Divine Magic',
     ((274.8, 485.8, 308.1, 499.1), (96, 471.8, 166.6, 485.1),)),
    (375, 500, 'Chapter 20 - The Setting',
     ((109, 94, 196.8, 106.3),)),
    (412, 82, 'Chapter 7 - Character Creation',
     ((282.9, 366.8, 303.1, 380.1), (96, 352.8, 191.3, 366.1),)),
    (422, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((475.9, 475.8, 526.7, 489.1), (342, 461.8, 462.1, 475.1),)),
    (422, 440, 'Chapter 18 - Bestiary',
     ((281.5, 282.8, 302.8, 296.1), (96, 268.8, 161.6, 282.1),)),
    (423, 440, 'Chapter 18 - Bestiary',
     ((179.5, 352.8, 260.4, 366.1),)),
    (423, 440, 'Chapter 18 - Bestiary',
     ((215.1, 338.8, 279.1, 352.1), (72, 324.8, 90.3, 338.1),)),
    (424, 484, 'Chapter 19 - Solo Roleplaying',
     ((177, 674.8, 278.9, 688.1), (132, 660.8, 143.9, 674.1),)),
    (431, 440, 'Chapter 18 - Bestiary',
     ((240.9, 708.7, 261.2, 722), (96, 694.7, 162.5, 708),)),
    (431, 440, 'Chapter 18 - Bestiary',
     ((221.6, 475.7, 266.2, 489), (96, 461.7, 131.3, 475),)),
    (431, 484, 'Chapter 19 - Solo Roleplaying',
     ((127.1, 247.7, 240.6, 261),)),
    (433, 200, 'Chapter 12 - Travel',
     ((383.2, 646.8, 465.5, 660.1),)),
    (433, 200, 'Chapter 12 - Travel',
     ((179.4, 548.8, 252.5, 562.1),)),
    (434, 200, 'Chapter 12 - Travel',
     ((497.1, 310.8, 542.2, 324.1), (330, 296.8, 358.8, 310.1),)),
    (434, 200, 'Chapter 12 - Travel',
     ((275.4, 212.8, 308.1, 226.1), (96, 198.8, 136.8, 212.1),)),
    (438, 150, 'Chapter 10 - Combat',
     ((438.4, 667.8, 519.4, 681.1),)),
    (438, 32, 'Chapter 3 - Skills',
     ((120, 422.8, 182.6, 436.1),)),
    (442, 150, 'Chapter 10 - Combat',
     ((383.4, 226.8, 463.7, 240.1),)),
    (445, 174, 'Chapter 11 - Wounds, Healing, & Other Perils',
     ((154.2, 198.3, 284.1, 211.6), (72, 184.3, 117.6, 197.6),)),
    (446, 174, 'Chapter 11 - Wounds, Healing, & Perils',
     ((96, 225.3, 238.3, 238.6),)),
    (447, 20, 'Chapter 2 - Core Mechanics',
     ((475.2, 200.6, 497.6, 211.7), (318, 186.6, 413.9, 197.7),)),
    (447, 484, 'Chapter 19 - Solo Roleplaying',
     ((338.4, 165.6, 464.7, 176.7),)),
    (448, 130, 'Chapter 9 - Equipment',
     ((492.7, 366.8, 542.7, 380.1), (330, 352.8, 370.7, 366.1),)),
    (461, 276, 'Chapter 14 - Weave Magic',
     ((461.9, 649.5, 509.2, 661.8), (252, 638.5, 299.1, 650.8),)),
    (475, 324, 'Chapter 15 - Weave Magic: Summoning',
     ((125.5, 649.5, 259.2, 661.8),)),
    (488, 200, 'Chapter 12 - Travel',
     ((431.9, 590.7, 504.8, 604),)),
    (489, 200, 'Chapter 12 - Travel',
     ((91.9, 310.8, 164.3, 324.1),)),
    (601, 484, 'Chapter 19 - Solo Roleplaying',
     ((171.4, 492.8, 282, 506.1),)),
    (606, 82, 'Chapter 7 - Character Creation',
     ((260.8, 548.8, 308.2, 562.1), (96, 534.8, 169.5, 548.1),)),
]


def add_bookmarks(pdf):
    """Replace the document outline with the chapter/section tree above."""
    with pdf.open_outline() as outline:
        outline.root.clear()
        parent = None
        for level, title, page, _tp, _x0, _y0, _x1, _y1 in ENTRIES:
            item = OutlineItem(title, page - 1)      # OutlineItem takes a 0-based index
            if level == 1 or parent is None:
                outline.root.append(item)
                parent = item
            else:
                parent.children.append(item)
    return len(ENTRIES)


def remove_generated_links(pdf):
    """Drop links a previous run added, so re-running replaces rather than stacks."""
    removed = 0
    for page in pdf.pages:
        if '/Annots' not in page:
            continue
        kept = [a for a in page.Annots if MARKER not in a]
        removed += len(page.Annots) - len(kept)
        if kept:
            page.Annots = Array(kept)
        else:
            del page['/Annots']
    return removed


def _link(pdf, rect, target_page):
    annot = Dictionary(
        Type=Name.Annot, Subtype=Name.Link,
        Rect=Array(list(rect)),
        Border=Array([0, 0, 0]),                # no visible box around the link
        A=Dictionary(S=Name.GoTo, D=Array([target_page.obj, Name.Fit])),
    )
    annot[MARKER] = True
    return pdf.make_indirect(annot)


def _attach(pdf, page_no, annots):
    page = pdf.pages[page_no - 1]
    existing = list(page.Annots) if '/Annots' in page else []
    page.Annots = Array(existing + annots)


def add_toc_links(pdf):
    """Lay a link annotation over each line of the printed contents page."""
    added = 0
    for _level, _title, page_no, toc_page, x0, y0, x1, y1 in ENTRIES:
        _attach(pdf, toc_page, [_link(pdf, (x0, y0, x1, y1), pdf.pages[page_no - 1])])
        added += 1
    return added


def add_xref_links(pdf):
    """Make every "see Chapter N - Title" in the body text clickable."""
    added = 0
    for src_page, target_page, _text, rects in XREFS:
        target = pdf.pages[target_page - 1]
        _attach(pdf, src_page, [_link(pdf, r, target) for r in rects])
        added += len(rects)
    return added


def parse_args(argv):
    """Parse the command line into an options object, or exit with a usage message."""
    me = os.path.basename(__file__)
    usage = (f"usage: python3 {me} [<rulebook>.pdf] [options]\n"
             f"  --bookmarks       also add bookmarks and links\n"
             f"  --bookmarks-only  add bookmarks and links, skip compression\n"
             f"  --dpi N           artwork resolution, {MIN_DPI}-{MAX_DPI} "
             f"(default {DPI}; 300 for print)")

    paths, flags, dpi = [], set(), DPI
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith('--dpi'):
            if '=' in arg:
                value = arg.split('=', 1)[1]
            else:
                i += 1
                if i >= len(argv):
                    sys.exit(f"--dpi needs a number, e.g. --dpi 300\n{usage}")
                value = argv[i]
            try:
                dpi = int(value)
            except ValueError:
                sys.exit(f"--dpi needs a whole number, not {value!r}\n{usage}")
            if not MIN_DPI <= dpi <= MAX_DPI:
                sys.exit(f"--dpi {dpi} is out of range; use {MIN_DPI}-{MAX_DPI}.\n"
                         f"200 suits screen reading, 300 suits printing.")
        elif arg.startswith('-'):
            if arg not in ('--bookmarks', '--bookmarks-only'):
                sys.exit(f"unknown option: {arg}\n{usage}")
            flags.add(arg)
        else:
            paths.append(arg)
        i += 1

    compress = '--bookmarks-only' not in flags
    if not compress and dpi != DPI:
        sys.exit("--dpi only affects compression, which --bookmarks-only skips.\n"
                 "Drop one or the other.")

    if paths:
        src_path = paths[0]
        if not os.path.exists(src_path):
            sys.exit(f"can't find {src_path}\n{usage}")
    else:
        # no path given: expect the rulebook sitting next to this script
        src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_NAME)
        if not os.path.exists(src_path):
            sys.exit(f"expected to find {DEFAULT_NAME} next to this script.\n"
                     f"either put this script beside the PDF, or give the full path:\n"
                     f"  python3 {me} /path/to/the-rulebook.pdf")

    # Name the output after the settings used, so that trying different options does
    # not silently overwrite the previous result. Defaults keep the plain name.
    if compress:
        suffix = '_screen'
        if dpi != DPI:
            suffix += f'_{dpi}dpi'
        suffix += '.pdf'
    else:
        suffix = '_bookmarked.pdf'

    return SimpleNamespace(
        src=src_path,
        out=paths[1] if len(paths) > 1 else os.path.splitext(src_path)[0] + suffix,
        navigation=bool(flags & {'--bookmarks', '--bookmarks-only'}),
        compress=compress,
        dpi=dpi,
    )


def add_navigation(pdf):
    """Bookmarks, clickable contents, clickable cross-references. Returns a summary."""
    needed = max(max(e[2] for e in ENTRIES), max(x[1] for x in XREFS))
    if len(pdf.pages) < needed:
        sys.exit(f"this PDF has {len(pdf.pages)} pages but the contents reference page "
                 f"{needed}.\nNavigation is specific to the core rulebook; "
                 f"re-run without --bookmarks.")
    if len(pdf.pages) != 667:
        print(f"   note: expected 667 pages, found {len(pdf.pages)}; "
              f"navigation may not line up", flush=True)
    marks = add_bookmarks(pdf)
    remove_generated_links(pdf)
    links = add_toc_links(pdf)
    xrefs = add_xref_links(pdf)
    return marks, links, xrefs


def main():
    if any(a in ('-h', '--help', '-help', '/?') for a in sys.argv[1:]):
        print(__doc__)
        return

    opts = parse_args(sys.argv[1:])
    src_path, out_path = opts.src, opts.out
    want_nav, do_compress = opts.navigation, opts.compress
    gs = find_ghostscript() if do_compress else None
    started = time.time()
    work = tempfile.mkdtemp(prefix='tbe_')

    def elapsed():
        return time.time() - started

    rebuilt = skipped = no_text = 0
    nav = None

    try:
        try:
            pdf = pikepdf.open(src_path)
        except pikepdf.PasswordError:
            sys.exit(f"{src_path} is password protected.\n"
                     "Remove the password in a PDF reader first, then run this again.")
        except pikepdf.PdfError as exc:
            sys.exit(f"could not read {src_path} as a PDF.\n  {exc}")
        orig_size = os.path.getsize(src_path)
        n_pages = len(pdf.pages)
        print(f"input : {src_path}  {orig_size/1e6:.0f} MB, {n_pages} pages", flush=True)

        if do_compress:
            print(f"        artwork at {opts.dpi} dpi, JPEG quality {JPEG_QUALITY}",
                  flush=True)
            weights, heavy, heavy_share = census(pdf)
            print(f"{len(heavy)} pages carry the traced artwork ({heavy_share:.0%} of this "
                  f"document's page data); the other {n_pages - len(heavy)} are left untouched",
                  flush=True)

            # Deliberately not checking the filename or the page count: people rename
            # downloads, and editions differ. What matters is whether this PDF actually
            # has the problem this script fixes, which the share above already answers.
            if heavy_share < MIN_HEAVY_SHARE:
                print("\n  WARNING: that is a small share. This script only helps with PDFs\n"
                      "  bloated by traced vector artwork. If yours is mostly images or scans,\n"
                      "  use Ghostscript instead; it will do better than this will.\n"
                      "  Carrying on anyway - pages that would not shrink are left alone.\n",
                      flush=True)

            # 1. one PDF per heavy page, so Ghostscript can work on them individually
            print("extracting heavy pages ...", flush=True)
            extract_single_pages(pdf, heavy, work)

            # 2. render the artwork of each, with the text filtered out
            def report_render(done):
                if done % 20 == 0 or done == len(heavy):
                    print(f"   rendered {done}/{len(heavy)}  ({elapsed():.0f}s)", flush=True)

            render_artwork(gs, heavy, work, report_render, dpi=opts.dpi)

            # 3. rebuild each heavy page as artwork-JPEG + its own text
            print("rebuilding pages (the slow part, ~10s each) ...", flush=True)
            original_size = dict(weights)
            for n, i in enumerate(heavy, 1):
                jpg = open(artwork_path(work, i), 'rb').read()
                page = pdf.pages[i]
                page_res = page.get('/Resources')
                ops, used_forms, has_text = filter_ops(page, page_res, pdf, {})
                text_bytes = pikepdf.unparse_content_stream(ops)

                if len(jpg) + len(text_bytes) >= original_size[i]:   # never grow a page
                    skipped += 1
                    continue
                if not has_text:
                    no_text += 1

                rebuild_page(pdf, page, jpg, text_bytes, page_res, used_forms)
                rebuilt += 1
                if n % 20 == 0 or n == len(heavy):
                    print(f"   rebuilt {n}/{len(heavy)}  ({elapsed():.0f}s)", flush=True)

            # 4. strip leftover InDesign/Illustrator metadata (lossless, ~102 MB here)
            count, freed = strip_private_metadata(pdf)
            print(f"   stripped {count} metadata streams (~{freed/1e6:.0f} MB)", flush=True)

        # 5. bookmarks and links, added before the save so there is only one write
        if want_nav:
            print("adding bookmarks and links ...", flush=True)
            nav = add_navigation(pdf)

        print("writing ...", flush=True)
        pdf.save(out_path, compress_streams=True, recompress_flate=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
        pdf.close()

        out_size = os.path.getsize(out_path)
        print(f"\noutput: {out_path}")
        if do_compress:
            print(f"        {orig_size/1e6:.0f} MB -> {out_size/1e6:.0f} MB "
                  f"({orig_size/out_size:.1f}x smaller) in {elapsed():.0f}s")
            print(f"        {rebuilt} pages rebuilt, {n_pages-rebuilt} untouched")
            if skipped:
                print(f"        {skipped} pages left alone "
                      f"(rebuilding them would not have helped)")
            if no_text:
                print(f"        WARNING: {no_text} rebuilt pages produced no text layer - "
                      f"check those pages before sharing the file")
        else:
            print(f"        {out_size/1e6:.0f} MB in {elapsed():.0f}s "
                  f"(compression skipped)")
        if nav:
            marks, links, xrefs = nav
            tops = sum(1 for e in ENTRIES if e[0] == 1)
            print(f"        {marks} bookmarks ({tops} top-level, {marks - tops} nested)")
            print(f"        {links} links over the contents pages")
            print(f"        {xrefs} cross-reference links in the body text "
                  f"({len(XREFS)} references)")
        if do_compress:
            print("\nTo confirm nothing was lost, compare the text of both files:")
            print(f"    pdftotext '{src_path}' a.txt && pdftotext '{out_path}' b.txt "
                  f"&& diff a.txt b.txt")
            print("    (no output from diff means every page still has "
                  "identical searchable text)")
    except RuntimeError as exc:
        sys.exit(f"\n{exc}\n"
                 "Your Ghostscript may be too old or broken; version 9.50 or newer is\n"
                 "recommended. Check it with: gs --version")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
