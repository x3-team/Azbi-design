#!/usr/bin/env python3
"""Build the AZBI Coffee Google Play feature graphic (1024x500, 24-bit PNG, no alpha).

Steps:
  1. Download the AZBI brandbook PDF (public Yandex Disk link from the project passport).
  2. Extract the brand fonts embedded in it (Galdern, Helvetica Neue Cyr) into src/.fonts/.
     The PDF only embeds per-page subsets, so glyphs are harvested from every subset and
     merged into one usable font per style. Fonts are licensed to AZBI and are not committed.
  3. Render src/feature-graphic.html in headless Chrome at 2x and downsample to 1024x500.

Usage:
  python3 scripts/build.py                # all variants -> ../ (store/google-play/feature-graphic)
  python3 scripts/build.py --guides       # also write *_guides.png with the safe-zone overlay
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pymupdf
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import cmap_format_4
from fontTools.ttLib.tables._g_l_y_f import Glyph
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FONTS = SRC / ".fonts"
CACHE = ROOT / ".cache"
BRANDBOOK_PUBLIC_KEY = "https://disk.yandex.ru/d/Nv1jE79qCbBgbQ"
BRANDBOOK_PATH = "/Azbi brandbook.pdf"

WIDTH, HEIGHT, SCALE = 1024, 500, 2

VARIANTS = {
    "ru": "feature-graphic_ru_1024x500.png",
    "uz": "feature-graphic_uz_1024x500.png",
}

# (PDF font name, merged file name, family name)
FONT_SPECS = [
    ("Galdern-Bold", "AzbiGaldernBold.ttf", "AzbiGaldernBold"),
    ("Galdern-Medium", "AzbiGaldernMedium.ttf", "AzbiGaldernMedium"),
    ("HelveticaNeueCyr-Roman", "AzbiHelvCyrRoman.ttf", "AzbiHelvCyrRoman"),
]


def download_brandbook() -> Path:
    CACHE.mkdir(exist_ok=True)
    pdf = CACHE / "azbi-brandbook.pdf"
    if pdf.exists():
        return pdf
    api = "https://cloud-api.yandex.net/v1/disk/public/resources/download?" + urllib.parse.urlencode(
        {"public_key": BRANDBOOK_PUBLIC_KEY, "path": BRANDBOOK_PATH}
    )
    href = json.load(urllib.request.urlopen(api))["href"]
    print("downloading brandbook ...")
    with urllib.request.urlopen(href) as r, open(pdf, "wb") as f:
        shutil.copyfileobj(r, f)
    return pdf


def _has_outline(font: TTFont, glyph_name: str) -> bool:
    gs = font.getGlyphSet()
    pen = BoundsPen(gs)
    gs[glyph_name].draw(pen)
    return pen.bounds is not None


def _best_cmap(font: TTFont) -> dict[int, str]:
    try:
        return dict(font.getBestCmap() or {})
    except KeyError:
        return {}


def extract_fonts(pdf_path: Path) -> None:
    doc = pymupdf.open(pdf_path)

    subsets: dict[str, list[tuple[int, TTFont]]] = {}
    trace: dict[int, dict[str, int]] = {}
    for page in doc:
        page_fonts = page.get_fonts(full=True)
        for xref, ext, _type, basefont, *_ in page_fonts:
            name = basefont.split("+")[-1]
            if ext != "ttf" or any(x == xref for x, _ in subsets.get(name, [])):
                continue
            buf = doc.extract_font(xref)[3]
            if buf:
                subsets.setdefault(name, []).append((xref, TTFont(io.BytesIO(buf))))
        # CID subsets carry no cmap: recover char -> glyph id from the page text.
        by_name = {f[3].split("+")[-1]: f[0] for f in page_fonts}
        for span in page.get_texttrace():
            xref = by_name.get(span["font"])
            if xref is None:
                continue
            for ch in span["chars"]:
                if ch[0] > 32:
                    trace.setdefault(xref, {})[chr(ch[0])] = ch[1]

    FONTS.mkdir(parents=True, exist_ok=True)
    for pdf_name, out_name, family in FONT_SPECS:
        fonts = subsets[pdf_name]
        base = copy.deepcopy(fonts[0][1])
        glyf, hmtx = base["glyf"], base["hmtx"]
        order = list(base.getGlyphOrder())
        cmap: dict[int, str] = {}

        space = _best_cmap(base).get(32)
        if not space:
            space = "space.azbi"
            glyf.glyphs[space] = Glyph()
            hmtx.metrics[space] = (int(base["head"].unitsPerEm * 0.278), 0)
            order.append(space)
        cmap[32] = space

        for xref, font in fonts:
            src_cmap = _best_cmap(font)
            glyph_order = font.getGlyphOrder()
            # Simple TrueType subsets: trust their cmap. CID subsets: use the text trace.
            pairs = (
                ((chr(c), g) for c, g in src_cmap.items())
                if src_cmap
                else ((ch, glyph_order[gid]) for ch, gid in trace.get(xref, {}).items() if gid < len(glyph_order))
            )
            for ch, g in pairs:
                if ord(ch) in cmap or not _has_outline(font, g):
                    continue
                new_name = f"u{ord(ch):04X}"
                glyph = copy.deepcopy(font["glyf"][g])
                if glyph.isComposite():
                    for comp in glyph.components:
                        comp_name = f"{comp.glyphName}.x{xref}"
                        if comp_name not in glyf.glyphs:
                            glyf.glyphs[comp_name] = copy.deepcopy(font["glyf"][comp.glyphName])
                            hmtx.metrics[comp_name] = font["hmtx"][comp.glyphName]
                            order.append(comp_name)
                        comp.glyphName = comp_name
                glyf.glyphs[new_name] = glyph
                hmtx.metrics[new_name] = font["hmtx"][g]
                order.append(new_name)
                cmap[ord(ch)] = new_name

        base.setGlyphOrder(order)
        glyf.glyphOrder = order
        base["maxp"].numGlyphs = len(order)
        for tag in ("hdmx", "LTSH", "VDMX", "kern", "GPOS", "GSUB", "cvt ", "fpgm", "prep"):
            if tag in base:
                del base[tag]
        if "post" not in base:
            post = newTable("post")
            post.italicAngle = 0
            post.underlinePosition, post.underlineThickness = -100, 50
            post.isFixedPitch = post.minMemType42 = post.maxMemType42 = post.minMemType1 = post.maxMemType1 = 0
            base["post"] = post
        base["post"].formatType = 3.0
        table = newTable("cmap")
        table.tableVersion = 0
        table.tables = []
        for platform, encoding in ((3, 1), (0, 3)):
            sub = cmap_format_4(4)
            sub.platformID, sub.platEncID, sub.language, sub.cmap = platform, encoding, 0, cmap
            table.tables.append(sub)
        base["cmap"] = table
        for rec in base["name"].names:
            if rec.nameID in (1, 3, 4, 6, 16):
                rec.string = family
        base.save(FONTS / out_name)
        print(f"font {out_name}: {len(cmap)} chars")


def find_chrome() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("Chrome/Chromium not found")


def render(variant: str, out: Path, guides: bool = False) -> None:
    query = urllib.parse.urlencode({"v": variant, **({"guides": "1"} if guides else {})})
    url = (SRC / "feature-graphic.html").as_uri() + "?" + query
    with tempfile.TemporaryDirectory() as tmp:
        shot = Path(tmp) / "shot.png"
        cmd = [
            find_chrome(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--hide-scrollbars", "--allow-file-access-from-files", f"--user-data-dir={tmp}/profile",
            f"--force-device-scale-factor={SCALE}", f"--window-size={WIDTH},{HEIGHT}",
            "--virtual-time-budget=4000", f"--screenshot={shot}", url,
        ]
        # Headless Chrome sometimes lingers after writing the screenshot; stop it once the file is complete.
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline, last_size = time.monotonic() + 60, -1
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.5)
            size = shot.stat().st_size if shot.exists() else -1
            if size > 0 and size == last_size:
                break
            last_size = size
        proc.kill()
        proc.wait()
        if not shot.exists():
            sys.exit(f"render failed for {variant}")
        img = Image.open(shot).convert("RGB")
    assert img.size == (WIDTH * SCALE, HEIGHT * SCALE), img.size
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out.relative_to(ROOT)} {img.size[0]}x{img.size[1]} {img.mode}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--guides", action="store_true", help="also render safe-zone overlay previews")
    ap.add_argument("--skip-fonts", action="store_true", help="reuse fonts already in src/.fonts")
    args = ap.parse_args()

    if not args.skip_fonts or not FONTS.exists():
        extract_fonts(download_brandbook())
    for variant, name in VARIANTS.items():
        render(variant, ROOT / name)
        if args.guides:
            render(variant, ROOT / "previews" / name.replace(".png", "_guides.png"), guides=True)


if __name__ == "__main__":
    (ROOT / "previews").mkdir(exist_ok=True)
    main()
