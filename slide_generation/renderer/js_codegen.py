import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List
# ---------------------------------------------------------------------------
# JS code-generation helpers  (each returns a string fragment)
# ---------------------------------------------------------------------------

def _js_header() -> str:
    return """\
const fs = require("fs");
const path = require("path");
const PptxGenJS = require("pptxgenjs");
"""


THEMES = {
    0: {  # Blue Professional (default)
        "primary":     "2e5f8c",  # lightened from 1a3c5e
        "primaryDk":   "1a3c5e",  # lightened from 0f2640
        "accent":      "2e86ab",
        "accentLight": "a8d8f0",  # more saturated from d4eef7
        "accentMist":  "cce8f5",  # more saturated from eaf4f9
        "panelBg":     "f2f5f9",
        "panelBorder": "dce2ea",
        "white":       "FFFFFF",
        "offWhite":    "f8f9fb",
        "textDark":    "1e293b",
        "textMid":     "475569",
        "textLight":   "94a3b8",
    },
    1: {  # Dark Charcoal + Teal
        "primary":     "2e2e48",  # lightened from 1e1e2e
        "primaryDk":   "1e1e2e",  # lightened from 14141f
        "accent":      "4ecdc4",
        "accentLight": "a0e8e2",  # more saturated from d0f4f1
        "accentMist":  "caf2ee",  # more saturated from e8faf8
        "panelBg":     "f0f5f4",
        "panelBorder": "d0dedd",
        "white":       "FFFFFF",
        "offWhite":    "f5f7f7",
        "textDark":    "1e293b",
        "textMid":     "475569",
        "textLight":   "94a3b8",
    },
    2: {  # Crimson Red
        "primary":     "8c2a42",  # lightened from 5e1a2a
        "primaryDk":   "5e1a2a",  # lightened from 3d0f1a
        "accent":      "c0392b",
        "accentLight": "e8a8a2",  # more saturated from f5d0cc
        "accentMist":  "f0ceca",  # more saturated from fbeae8
        "panelBg":     "fdf2f1",
        "panelBorder": "e8d0ce",
        "white":       "FFFFFF",
        "offWhite":    "fdf8f8",
        "textDark":    "1e293b",
        "textMid":     "475569",
        "textLight":   "94a3b8",
    },
    3: {  # Slate Rose
        "primary":     "5e4278",  # lightened from 3c2a4d
        "primaryDk":   "3c2a4d",  # lightened from 261a33
        "accent":      "e84393",
        "accentLight": "f4a8cc",  # more saturated from fad0e4
        "accentMist":  "f8d2e8",  # more saturated from fdeef5
        "panelBg":     "fdf2f7",
        "panelBorder": "eacfdd",
        "white":       "FFFFFF",
        "offWhite":    "fef8fb",
        "textDark":    "1e293b",
        "textMid":     "475569",
        "textLight":   "94a3b8",
    },
}


def _js_configure_presentation(theme_id: int = 0, design_id: int = 0) -> str:
    """Color palette, layout, per-template backgrounds, and shared helpers."""
    theme = THEMES.get(theme_id, THEMES[0])
    c_entries = ",\n".join(
        f'  {k+":":<14s}"{v}"' for k, v in theme.items()
    )
    palette_block = f"const C = {{\n{c_entries}\n}};\n"
    design_block = f"const DESIGN_ID = {design_id};\n"
    rest = """\
function configurePres(pptx) {
  pptx.defineLayout({ name: "CUSTOM_16x9", width: 13.33, height: 7.5 });
  pptx.layout = "CUSTOM_16x9";
  pptx.author = "SlideGen";
  pptx.subject = "Auto-generated presentation";
}

function mkShadow(blur, offset, opacity, color) {
  return { type: "outer", blur: blur||6, offset: offset||2, angle: 135,
           opacity: opacity||0.12, color: color||"000000" };
}

// Design 1: navy + sage + light gray (corporate minimal references)
function addDesign1CoverLeftBars(slide) {
  var x = 0.48;
  var w = 0.26;
  var g = 0.1;
  var y0 = 1.42;
  var h1 = 1.82;
  var y2 = y0 + h1 + g;
  var h2 = 1.95;
  slide.addShape("rect", { x: x, y: y0, w: w, h: h1, fill: { color: C.primary } });
  slide.addShape("rect", { x: x, y: y2, w: w, h: h2, fill: { color: C.accent } });
}

function addDesign1EdgeBarSplit(slide, x, y, h, bw, topCol, botCol) {
  var mid = y + h * 0.5;
  var gap = 0.06;
  var hTop = mid - y - gap / 2;
  var hBot = y + h - mid - gap / 2;
  slide.addShape("rect", { x: x, y: y, w: bw, h: hTop, fill: { color: topCol } });
  slide.addShape("rect", { x: x, y: mid + gap / 2, w: bw, h: hBot, fill: { color: botCol } });
}

function addDesign1ThanksSideAccents(slide) {
  var bw = 0.055;
  var y0 = 1.05;
  var mid = 3.65;
  var gap = 0.08;
  var hBot = 7.42 - mid - gap / 2;
  slide.addShape("rect", { x: 0.08, y: y0, w: bw, h: mid - y0 - gap / 2,
    fill: { color: C.primary } });
  slide.addShape("rect", { x: 0.08, y: mid + gap / 2, w: bw, h: hBot,
    fill: { color: C.white } });
  var rx = 13.33 - 0.08 - bw;
  slide.addShape("rect", { x: rx, y: y0, w: bw, h: mid - y0 - gap / 2,
    fill: { color: C.primary } });
  slide.addShape("rect", { x: rx, y: mid + gap / 2, w: bw, h: hBot,
    fill: { color: C.white } });
}

// --- Design-aware decorative helpers ---

function addCornerDecor(slide, cx, cy, flip) {
  const dx = flip ? -1 : 1;
  if (DESIGN_ID === 1) {
    // Editorial: corner blocks + spine dot are drawn in addLayoutBg
    return;
  } else if (DESIGN_ID === 2) {
    // Sunset Gradient: warm diagonal streaks with coral/gold/purple tones
    slide.addShape("rect", { x: cx - 0.3, y: cy - 0.3, w: 4.5, h: 4.5,
      fill: { color: "E8725A", transparency: 75 }, rotate: 25 });
    slide.addShape("rect", { x: cx + dx * 0.4, y: cy + 0.2, w: 3.0, h: 3.0,
      fill: { color: "F5A623", transparency: 70 }, rotate: 35 });
    slide.addShape("ellipse", { x: cx + dx * 0.9, y: cy + 0.8, w: 2.0, h: 2.0,
      fill: { color: "9B59B6", transparency: 72 } });
    slide.addShape("ellipse", { x: cx + dx * 1.5, y: cy + 1.4, w: 0.8, h: 0.8,
      fill: { color: "F39C12", transparency: 50 } });
  } else if (DESIGN_ID === 3) {
    // Aurora Neon: glowing triangular shapes with electric colors
    const bx = flip ? cx + 0.2 : cx;
    slide.addShape("rect", { x: bx, y: cy, w: 3.5, h: 3.5,
      fill: { color: "00E5FF", transparency: 85 }, rotate: 45 });
    slide.addShape("rect", { x: bx + dx * 0.5, y: cy + 0.3, w: 2.2, h: 2.2,
      fill: { color: "E040FB", transparency: 80 }, rotate: 45 });
    slide.addShape("rect", { x: bx + dx * 1.0, y: cy + 0.8, w: 1.2, h: 1.2,
      fill: { color: "76FF03", transparency: 75 }, rotate: 45 });
    slide.addShape("ellipse", { x: bx + dx * 0.2, y: cy + 2.0, w: 0.35, h: 0.35,
      fill: { color: "00E5FF", transparency: 45 } });
  } else if (DESIGN_ID === 4) {
    // Design 4 corner pixels are drawn in addLayoutBg/addCoverSlide/addThanksSlide.
    // Keep this helper empty so we don't reintroduce extra decorations.
    return;
  } else if (DESIGN_ID === 5) {
    // Design 5: Glassmorphism soft gradient circles
    slide.addShape("ellipse", { x: cx, y: cy, w: 4.0, h: 4.0,
      fill: { color: "818CF8", transparency: 88 } });
    slide.addShape("ellipse", { x: cx + dx * 0.6, y: cy + 0.5, w: 2.5, h: 2.5,
      fill: { color: "A78BFA", transparency: 80 } });
  } else {
    // Design 0: Bokeh circles cluster
    slide.addShape("ellipse", { x: cx, y: cy, w: 3.8, h: 3.8,
      fill: { color: C.accent, transparency: 88 } });
    slide.addShape("ellipse", { x: cx + dx * 0.6, y: cy + 0.5, w: 2.4, h: 2.4,
      fill: { color: C.accentLight, transparency: 75 } });
    slide.addShape("ellipse", { x: cx + dx * 1.1, y: cy + 0.9, w: 1.2, h: 1.2,
      fill: { color: C.accentMist, transparency: 55 } });
  }
}

function addFooterBar(slide) {
  if (DESIGN_ID === 1) {
    // Minimal: hairline + navy + sage tick
    slide.addShape("rect", { x: 0, y: 7.47, w: 13.33, h: 0.03,
      fill: { color: C.panelBorder } });
    slide.addShape("rect", { x: 0, y: 7.47, w: 1.85, h: 0.03,
      fill: { color: C.primary } });
    slide.addShape("rect", { x: 1.85, y: 7.47, w: 1.1, h: 0.03,
      fill: { color: C.accent } });
  } else if (DESIGN_ID === 2) {
    // Sunset Gradient footer: warm multi-color layered strip
    slide.addShape("rect", { x: 0, y: 6.95, w: 13.33, h: 0.12,
      fill: { color: "F5A623", transparency: 45 } });
    slide.addShape("rect", { x: 0, y: 7.07, w: 13.33, h: 0.08,
      fill: { color: "E8725A", transparency: 35 } });
    slide.addShape("rect", { x: 0, y: 7.15, w: 13.33, h: 0.06,
      fill: { color: "9B59B6" } });
    slide.addShape("rect", { x: 0, y: 7.21, w: 13.33, h: 0.29,
      fill: { color: C.primary } });
    slide.addShape("ellipse", { x: 0.3, y: 6.85, w: 1.0, h: 1.0,
      fill: { color: "F39C12", transparency: 70 } });
    slide.addShape("ellipse", { x: 11.8, y: 6.90, w: 0.8, h: 0.8,
      fill: { color: "E8725A", transparency: 65 } });
  } else if (DESIGN_ID === 3) {
    // Aurora Neon footer: glowing neon edge bars
    slide.addShape("rect", { x: 0, y: 7.02, w: 13.33, h: 0.05,
      fill: { color: "00E5FF", transparency: 40 } });
    slide.addShape("rect", { x: 0, y: 7.07, w: 13.33, h: 0.04,
      fill: { color: "E040FB", transparency: 35 } });
    slide.addShape("rect", { x: 0, y: 7.11, w: 13.33, h: 0.03,
      fill: { color: "76FF03", transparency: 40 } });
    slide.addShape("rect", { x: 0, y: 7.14, w: 13.33, h: 0.36,
      fill: { color: "1A1A2E" } });
    slide.addShape("ellipse", { x: 10.5, y: 6.90, w: 0.6, h: 0.6,
      fill: { color: "00E5FF", transparency: 70 } });
    slide.addShape("ellipse", { x: 11.4, y: 6.95, w: 0.4, h: 0.4,
      fill: { color: "E040FB", transparency: 65 } });
    slide.addShape("ellipse", { x: 12.0, y: 7.00, w: 0.25, h: 0.25,
      fill: { color: "76FF03", transparency: 55 } });
  } else if (DESIGN_ID === 4) {
    // Teal/Gold footer: thin gold rule + soft pink hint
    slide.addShape("rect", { x: 0, y: 7.12, w: 13.33, h: 0.03,
      fill: { color: C.accent } });
    slide.addShape("rect", { x: 0, y: 7.15, w: 13.33, h: 0.02,
      fill: { color: C.accentMist, transparency: 35 } });
    slide.addShape("ellipse", { x: 12.55, y: 7.05, w: 0.25, h: 0.25,
      fill: { color: C.accent, transparency: 35 } });
  } else if (DESIGN_ID === 5) {
    // Design 5: Glassmorphism frosted footer bar
    slide.addShape("roundRect", { x: 0.5, y: 7.15, w: 12.33, h: 0.28,
      fill: { color: "4338CA", transparency: 75 }, rectRadius: 0.14 });
  } else {
    // Design 0: gradient layered footer with dots
    slide.addShape("rect", { x: 0, y: 7.12, w: 13.33, h: 0.06,
      fill: { color: C.accent, transparency: 50 } });
    slide.addShape("rect", { x: 0, y: 7.18, w: 13.33, h: 0.04,
      fill: { color: C.accent } });
    slide.addShape("rect", { x: 0, y: 7.22, w: 13.33, h: 0.28,
      fill: { color: C.primary } });
    slide.addShape("ellipse", { x: 11.6, y: 7.05, w: 0.35, h: 0.35,
      fill: { color: C.accent, transparency: 60 } });
    slide.addShape("ellipse", { x: 12.1, y: 7.12, w: 0.2, h: 0.2,
      fill: { color: C.accentLight, transparency: 50 } });
  }
}

function addFrameRect(slide, x, y, w, h) {
  if (DESIGN_ID === 1) {
    // Minimal modern: flat card, hairline border, no shadow
    slide.addShape("roundRect", { x: x, y: y, w: w, h: h,
      fill: { color: C.white },
      rectRadius: 0.06,
      line: { color: C.panelBorder, width: 0.35 } });
  } else if (DESIGN_ID === 2) {
    // Sunset Gradient: warm-toned frame with coral glow
    slide.addShape("roundRect", { x: x - 0.06, y: y - 0.06, w: w + 0.12, h: h + 0.12,
      fill: { color: "F5A623", transparency: 65 },
      rectRadius: 0.18 });
    slide.addShape("roundRect", { x: x, y: y, w: w, h: h,
      fill: { color: C.panelBg },
      rectRadius: 0.14,
      line: { color: "E8725A", width: 1.0 } });
  } else if (DESIGN_ID === 3) {
    // Aurora Neon: frosted frame with neon border glow
    slide.addShape("roundRect", { x: x - 0.05, y: y - 0.05, w: w + 0.10, h: h + 0.10,
      fill: { color: "00E5FF", transparency: 75 },
      rectRadius: 0.12 });
    slide.addShape("roundRect", { x: x, y: y, w: w, h: h,
      fill: { color: "F0F4F8" },
      rectRadius: 0.08,
      line: { color: "E040FB", width: 0.75 } });
  } else if (DESIGN_ID === 4) {
    // Teal/Gold: subtle rounded frame behind images (no neon colors)
    slide.addShape("roundRect", { x: x - 0.05, y: y - 0.05, w: w + 0.10, h: h + 0.10,
      fill: { color: C.accentMist, transparency: 85 },
      rectRadius: 0.12 });
    slide.addShape("roundRect", { x: x, y: y, w: w, h: h,
      fill: { color: "0F6A6D", transparency: 70 },
      rectRadius: 0.08,
      line: { color: C.accent, width: 0.75 } });
  } else if (DESIGN_ID === 5) {
    // Design 5: Glassmorphism frosted image card
    slide.addShape("roundRect", { x: x, y: y, w: w, h: h,
      fill: { color: C.white, transparency: 40 }, rectRadius: 0.15,
      line: { color: "C7D2FE", width: 0.5 },
      shadow: mkShadow(12, 4, 0.08) });
  } else {
    slide.addShape("roundRect", { x: x, y: y, w: w, h: h,
      fill: { color: C.accentMist, transparency: 35 },
      rectRadius: 0.12,
      line: { color: C.accentLight, width: 1.0 } });
  }
}

function addLayoutBg(slide, templateId) {
  slide.background = { color: C.offWhite };

  // Design-specific ambient background
  if (DESIGN_ID === 1) {
    // Content: white + small gray corners + thin L/R navy|sage split bars
    slide.background = { color: C.white };
    slide.addShape("rect", { x: 0.0, y: 0.0, w: 1.0, h: 0.38,
      fill: { color: C.accentMist } });
    slide.addShape("rect", { x: 12.2, y: 6.58, w: 1.13, h: 0.92,
      fill: { color: C.accentMist } });
    addDesign1EdgeBarSplit(slide, 0.07, 1.05, 5.45, 0.052, C.primary, C.accent);
    addDesign1EdgeBarSplit(slide, 13.33 - 0.122, 1.05, 5.45, 0.052, C.primary, C.accent);
  } else if (DESIGN_ID === 2) {
    // Sunset Gradient: warm diagonal color blocks + organic floating shapes
    slide.addShape("rect", { x: -4.0, y: -3.0, w: 12.0, h: 14.0,
      fill: { color: "F5A623", transparency: 92 }, rotate: 15 });
    slide.addShape("rect", { x: 6.0, y: -2.0, w: 10.0, h: 12.0,
      fill: { color: "E8725A", transparency: 93 }, rotate: -10 });
    slide.addShape("ellipse", { x: -2.5, y: 4.5, w: 6.0, h: 6.0,
      fill: { color: "9B59B6", transparency: 92 } });
    slide.addShape("ellipse", { x: 10.0, y: -1.5, w: 5.0, h: 5.0,
      fill: { color: "F39C12", transparency: 90 } });
    slide.addShape("rect", { x: 0.0, y: 0.985, w: 0.06, h: 6.2,
      fill: { color: "E8725A", transparency: 50 } });
    slide.addShape("rect", { x: 0.12, y: 0.985, w: 0.03, h: 6.2,
      fill: { color: "F5A623", transparency: 55 } });
  } else if (DESIGN_ID === 3) {
    // Aurora Neon: dark bg with neon geometric accents
    slide.background = { color: "0F0F23" };
    slide.addShape("rect", { x: -3.0, y: -2.0, w: 8.0, h: 12.0,
      fill: { color: "00E5FF", transparency: 95 }, rotate: 20 });
    slide.addShape("rect", { x: 7.0, y: -1.0, w: 9.0, h: 11.0,
      fill: { color: "E040FB", transparency: 95 }, rotate: -15 });
    slide.addShape("ellipse", { x: -1.5, y: 4.0, w: 4.0, h: 4.0,
      fill: { color: "76FF03", transparency: 93 } });
    slide.addShape("ellipse", { x: 10.5, y: -0.5, w: 3.5, h: 3.5,
      fill: { color: "00E5FF", transparency: 92 } });
    slide.addShape("rect", { x: 0.0, y: 0.985, w: 0.04, h: 6.2,
      fill: { color: "00E5FF", transparency: 55 } });
    slide.addShape("rect", { x: 0.08, y: 0.985, w: 0.02, h: 6.2,
      fill: { color: "E040FB", transparency: 60 } });
  } else if (DESIGN_ID === 4) {
    // Screenshot-like content background:
    // - green outer frame
    // - teal inner field
    // - checkered corner pixels
    var FRAME = 0.24;
    var OUTER = "C7F2D0"; // light green border
    var TEAL  = "0F6A6D"; // interior teal
    var SQ_L  = OUTER;     // merge with border
    var SQ_D  = "A8E6BF"; // subtle darker green for checker
    var SQ_T_L = 90;       // very light visibility
    var SQ_T_D = 82;       // slightly less transparent for contrast

    slide.background = { color: OUTER };
    slide.addShape("rect", {
      x: FRAME, y: FRAME,
      w: 13.33 - 2 * FRAME,
      h: 7.5 - 2 * FRAME,
      fill: { color: TEAL },
    });

    // Corner "pixel" checker: 4x4 blocks, each 3x bigger.
    var sq = 0.36;
    var grid = 4;
    var off = 0.0;
    function drawCorner(x0, y0) {
      for (var gx = 0; gx < grid; gx++) {
        for (var gy = 0; gy < grid; gy++) {
          const useLight = ((gx + gy) % 2 === 0);
          slide.addShape("rect", {
            x: x0 + gx * sq,
            y: y0 + gy * sq,
            w: sq, h: sq,
            fill: { color: useLight ? SQ_L : SQ_D, transparency: useLight ? SQ_T_L : SQ_T_D },
          });
        }
      }
    }
    drawCorner(off, off); // TL
    drawCorner(13.33 - off - grid * sq, off); // TR
    drawCorner(off, 7.5 - off - grid * sq); // BL
    drawCorner(13.33 - off - grid * sq, 7.5 - off - grid * sq); // BR
  } else if (DESIGN_ID === 5) {
    // Design 5: Glassmorphism gradient-like bg with soft blobs
    slide.background = { color: "EDE9FE" };
    slide.addShape("ellipse", { x: -4, y: -3, w: 10, h: 10,
      fill: { color: "C4B5FD", transparency: 70 } });
    slide.addShape("ellipse", { x: 8, y: 3, w: 8, h: 8,
      fill: { color: "A78BFA", transparency: 75 } });
    slide.addShape("ellipse", { x: 3, y: -2, w: 6, h: 6,
      fill: { color: "818CF8", transparency: 82 } });
  } else {
    // Design 0: large circle + accent line
    slide.addShape("ellipse", { x: -3.0, y: -2.5, w: 8.0, h: 8.0,
      fill: { color: C.accentMist, transparency: 85 } });
    slide.addShape("rect", { x: 0.08, y: 0.985, w: 0.03, h: 6.2,
      fill: { color: C.accent, transparency: 65 } });
  }

  addFooterBar(slide);

  switch (templateId) {
    case "T1_TextOnly":
      addCornerDecor(slide, 10.5, -1.5, false);
      if (DESIGN_ID !== 1 && DESIGN_ID !== 5) {
        slide.addShape("ellipse", { x: -0.5, y: 5.5, w: 2.0, h: 2.0,
          fill: { color: C.accentMist, transparency: 70 } });
      }
      break;

    case "T2_ImageRight":
      addCornerDecor(slide, 11.0, 5.0, false);
      if (DESIGN_ID !== 3 && DESIGN_ID !== 4 && DESIGN_ID !== 5) {
        for (var i = 0; i < 8; i++) {
          slide.addShape("rect", { x: 6.85, y: 1.2 + i * 0.7, w: 0.04, h: 0.35,
            fill: { color: C.accent, transparency: 60 } });
        }
      }
      break;

    case "T3_ImageLeft":
      addCornerDecor(slide, -1.5, 5.0, true);
      if (DESIGN_ID !== 3 && DESIGN_ID !== 4 && DESIGN_ID !== 5) {
        for (var i = 0; i < 8; i++) {
          slide.addShape("rect", { x: 6.42, y: 1.2 + i * 0.7, w: 0.04, h: 0.35,
            fill: { color: C.accent, transparency: 60 } });
        }
      }
      break;

    case "T4_ImageTop":
      addCornerDecor(slide, 10.8, -1.8, false);
      break;

    case "T5_TwoImages2":
      addCornerDecor(slide, 11.5, -1.5, false);
      break;

    case "T6_2x2_TopImage":
    case "T7_2x2_BottomImage":
    case "T8_2x2_AltTextImg":
    case "T9_4Img_2x2Grid":
      break;

    case "T10_3Img_BottomTextTop":
      addCornerDecor(slide, 10.8, -1.5, false);
      break;

    case "T11_ImageRight2":
      if (DESIGN_ID !== 5) {
        slide.addShape("rect", { x: 7.4, y: 3.85, w: 5.4, h: 0.03,
          fill: { color: C.accent, transparency: 50 } });
      }
      addCornerDecor(slide, 11.0, 5.5, false);
      if (DESIGN_ID !== 3 && DESIGN_ID !== 4 && DESIGN_ID !== 5) {
        for (var i = 0; i < 8; i++) {
          slide.addShape("rect", { x: 6.85, y: 1.2 + i * 0.7, w: 0.04, h: 0.35,
            fill: { color: C.accent, transparency: 60 } });
        }
      }
      break;

    case "T12_ImageLeft2":
      if (DESIGN_ID !== 5) {
        slide.addShape("rect", { x: 0.5, y: 3.85, w: 5.4, h: 0.03,
          fill: { color: C.accent, transparency: 50 } });
      }
      addCornerDecor(slide, -1.5, 5.5, true);
      if (DESIGN_ID !== 3 && DESIGN_ID !== 4 && DESIGN_ID !== 5) {
        for (var i = 0; i < 8; i++) {
          slide.addShape("rect", { x: 6.42, y: 1.2 + i * 0.7, w: 0.04, h: 0.35,
            fill: { color: C.accent, transparency: 60 } });
        }
      }
      break;

    case "T14_2Text":
      addFrameRect(slide, 0.2, 0.985, 6.1, 6.07);
      addFrameRect(slide, 6.7, 0.985, 6.3, 6.07);
      slide.addShape("rect", { x: 6.55, y: 1.3, w: 0.03, h: 5.3,
        fill: { color: C.panelBorder } });
      addCornerDecor(slide, 11.0, -1.5, false);
      break;

    default:
      addCornerDecor(slide, 10.5, -1.5, false);
      break;
  }
}

function addTextPanel(slide, x, y, w, h) {
  if (DESIGN_ID === 1) {
    // White card + hairline navy|sage split left accent
    slide.addShape("roundRect", {
      x: x - 0.04, y: y - 0.04, w: w + 0.08, h: h + 0.08,
      fill: { color: C.white },
      rectRadius: 0.06,
      line: { color: C.panelBorder, width: 0.35 },
    });
    var ah = (h + 0.08) * 0.52;
    slide.addShape("rect", {
      x: x - 0.04, y: y - 0.04, w: 0.022, h: ah,
      fill: { color: C.primary },
    });
    slide.addShape("rect", {
      x: x - 0.04, y: y - 0.04 + ah + 0.03, w: 0.022, h: (h + 0.08) - ah - 0.03,
      fill: { color: C.accent },
    });
  } else if (DESIGN_ID === 2) {
    // Sunset Gradient: warm outer glow with coral/gold layered card
    slide.addShape("roundRect", {
      x: x - 0.20, y: y - 0.18, w: w + 0.40, h: h + 0.36,
      fill: { color: "F5A623", transparency: 72 },
      rectRadius: 0.22,
    });
    slide.addShape("roundRect", {
      x: x - 0.12, y: y - 0.10, w: w + 0.24, h: h + 0.20,
      fill: { color: "E8725A", transparency: 78 },
      rectRadius: 0.18,
    });
    slide.addShape("roundRect", {
      x: x - 0.06, y: y - 0.04, w: w + 0.12, h: h + 0.08,
      fill: { color: C.panelBg },
      rectRadius: 0.14,
      shadow: { type: "outer", blur: 10, offset: 3, opacity: 0.12, color: "9B59B6" },
    });
    slide.addShape("roundRect", {
      x: x - 0.06, y: y - 0.04, w: w + 0.12, h: 0.06,
      fill: { color: "E8725A" },
      rectRadius: 0.04,
    });
  } else if (DESIGN_ID === 3) {
    // Design 3: neon textbox = transparent (no background) with single edge line.
    // If the panel is nearly full-width => draw a top line (image above).
    // Otherwise => draw a left/right line depending on which side the panel is on.
    if (w >= 10.0) {
      slide.addShape("rect", {
        x: x,
        y: y - 0.045,
        w: w,
        h: 0.05,
        fill: { color: "00E5FF" },
      });
    } else {
      const thick = 0.02;
      // panel on left => line on right edge (towards image)
      const useRightEdge = x < 4.0;
      if (useRightEdge) {
        slide.addShape("rect", {
          x: x + w - thick,
          y: y,
          w: thick,
          h: h,
          fill: { color: "E040FB" },
        });
      } else {
        slide.addShape("rect", {
          x: x,
          y: y,
          w: thick,
          h: h,
          fill: { color: "E040FB" },
        });
      }
    }
  } else if (DESIGN_ID === 4) {
    // Design 4: no textbox background/border (screenshot vibe).
    // Only draw an accent line:
    // - if panel spans almost full width => draw a top line
    // - otherwise => draw a left/right line based on whether x is on left or right side
    if (w >= 10.0) {
      slide.addShape("rect", {
        x: x,
        y: y - 0.045,
        w: w,
        h: 0.05,
        fill: { color: C.accent },
      });
    } else {
      const thick = 0.02;
      const useRightEdge = x < 4.0;
      if (useRightEdge) {
        slide.addShape("rect", {
          x: x + w - thick,
          y: y,
          w: thick,
          h: h,
          fill: { color: C.accent },
        });
      } else {
        slide.addShape("rect", {
          x: x,
          y: y,
          w: thick,
          h: h,
          fill: { color: C.accent },
        });
      }
    }
  } else if (DESIGN_ID === 5) {
    // Design 5: Glassmorphism frosted text card
    slide.addShape("roundRect", {
      x: x - 0.06, y: y - 0.04, w: w + 0.12, h: h + 0.08,
      fill: { color: C.white, transparency: 35 }, rectRadius: 0.14,
      line: { color: "C7D2FE", width: 0.5 },
      shadow: mkShadow(10, 3, 0.08) });
  } else {
    // Design 0: card with glow, top bar and left accent
    slide.addShape("roundRect", {
      x: x - 0.14, y: y - 0.12, w: w + 0.28, h: h + 0.24,
      fill: { color: C.accentMist, transparency: 70 },
      rectRadius: 0.14,
    });
    slide.addShape("roundRect", {
      x: x - 0.08, y: y - 0.06, w: w + 0.16, h: h + 0.12,
      fill: { color: C.panelBg },
      rectRadius: 0.10,
      line: { color: C.panelBorder, width: 0.75 },
      shadow: { type: "outer", blur: 6, offset: 2, opacity: 0.12, color: "000000" },
    });
    slide.addShape("roundRect", {
      x: x - 0.08, y: y - 0.06, w: w + 0.16, h: 0.06,
      fill: { color: C.accent },
      rectRadius: 0.04,
    });
    slide.addShape("roundRect", {
      x: x - 0.08, y: y - 0.06, w: 0.07, h: h + 0.12,
      fill: { color: C.accent },
      rectRadius: 0.04,
    });
  }
}

"""
    sunset_override = """\
if (DESIGN_ID === 2) {
  C.primary     = "B5501E";
  C.primaryDk   = "7A3515";
  C.accent      = "E8725A";
  C.accentLight = "F5C0A8";
  C.accentMist  = "FDE8D5";
  C.panelBg     = "FFF8F0";
  C.panelBorder = "E8C8B0";
  C.offWhite    = "FEF5EC";
}
"""
    design3_override = """\
if (DESIGN_ID === 3) {
  // Keep neon content readable on the dark aurora background.
  C.textDark  = "FFFFFF";
  C.textMid   = "FFFFFF";
  C.textLight = "FFFFFF";
}
"""
    design4_override = """\
if (DESIGN_ID === 4) {
  // Screenshot-like Teal + Gold accents; keep text always readable.
  C.primary     = "F2B705"; // gold
  C.accent      = "F2B705";
  C.accentLight = "F6D37A";
  C.accentMist  = "F0B3AE"; // soft pink
  C.panelBg     = "0F6A6D"; // teal
  C.panelBorder = "E7C6B9";
  C.offWhite    = "0F6A6D";
  C.white       = "FFFFFF";
  C.textDark    = "FFFFFF";
  C.textMid     = "FFFFFF";
  C.textLight   = "FFFFFF";
  C.thanksBg    = "0F6A6D";
}
"""
    design1_override = """\
if (DESIGN_ID === 1) {
  // Navy + sage + light gray (cover/content ref: white canvas; thank-you: sage canvas)
  C.primary     = "2C4270";
  C.primaryDk   = "1E3558";
  C.accent      = "9BB8A9";
  C.accentLight = "B8C9C0";
  C.accentMist  = "E5E5E5";
  C.panelBg     = "FFFFFF";
  C.panelBorder = "E0E0E0";
  C.white       = "FFFFFF";
  C.offWhite    = "FFFFFF";
  C.textDark    = "1E293B";
  C.textMid     = "475569";
  C.textLight   = "64748B";
  C.thanksBg    = "B8C9B8";
}
"""
    return (
        palette_block
        + "\n"
        + design_block
        + "\n"
        + sunset_override
        + "\n"
        + design3_override
        + "\n"
        + design4_override
        + "\n"
        + design1_override
        + "\n"
        + rest
    )


def _js_font_config(bullet_font_size: int = 20, title_font_size: int = 28) -> str:
    """Emit JS constants for configurable font sizes."""
    sub_bullet = bullet_font_size - 2
    return f"""\
let TITLE_FONT_SIZE = {title_font_size};
let BULLET_FONT_SIZE = {bullet_font_size};
let SUB_BULLET_FONT_SIZE = {sub_bullet};
const _TITLE_FONT_SIZE_BASE = TITLE_FONT_SIZE;
const _BULLET_FONT_SIZE_BASE = BULLET_FONT_SIZE;
const _SUB_BULLET_FONT_SIZE_BASE = SUB_BULLET_FONT_SIZE;
"""


def _js_parse_latex() -> str:
    """JS helpers to parse \\textbf{} and \\textcolor{color}{} into rich-text segments."""
    return """\
const LATEX_COLORS = {
  red: "FF0000", blue: "0000FF", green: "008000", black: "000000",
  white: "FFFFFF", orange: "FFA500", yellow: "FFFF00", purple: "800080",
  brown: "A52A2A", gray: "808080", grey: "808080",
};

function findMatchingBrace(s, start) {
  let depth = 1, i = start + 1;
  while (i < s.length && depth > 0) {
    if (s[i] === "{") depth++;
    else if (s[i] === "}") depth--;
    i++;
  }
  return depth === 0 ? i - 1 : -1;
}

function parseLatex(s) {
  if (!s) return [{ text: s || "", bold: false, color: null }];
  const PATS_BF = ["\\\\textbf{", "textbf{", "extbf{"];
  const PATS_TC = ["\\\\textcolor{", "textcolor{", "extcolor{"];
  const out = [];
  let i = 0;
  while (i < s.length) {
    let idxBf = s.length, lenBf = 0;
    for (const pat of PATS_BF) {
      const pos = s.indexOf(pat, i);
      if (pos !== -1 && pos < idxBf) { idxBf = pos; lenBf = pat.length; }
    }
    let idxTc = s.length, lenTc = 0;
    for (const pat of PATS_TC) {
      const pos = s.indexOf(pat, i);
      if (pos !== -1 && pos < idxTc) { idxTc = pos; lenTc = pat.length; }
    }
    const nextIdx = Math.min(idxBf, idxTc);
    if (nextIdx > i) {
      const plain = s.substring(i, nextIdx).replace(/\\t+$/, "");
      if (plain.trim()) out.push({ text: plain, bold: false, color: null });
      i = nextIdx;
      continue;
    }
    if (nextIdx === s.length) break;
    if (idxBf <= idxTc) {
      const openBr = nextIdx + lenBf - 1;
      const closeBr = findMatchingBrace(s, openBr);
      if (closeBr === -1) { i = nextIdx + 1; continue; }
      const content = s.substring(openBr + 1, closeBr);
      for (const seg of parseLatex(content)) {
        out.push({ text: seg.text, bold: true, color: seg.color });
      }
      i = closeBr + 1;
    } else {
      const openBr = nextIdx + lenTc - 1;
      const closeBr = findMatchingBrace(s, openBr);
      if (closeBr === -1) { i = nextIdx + 1; continue; }
      const colorName = s.substring(openBr + 1, closeBr).trim().toLowerCase();
      const nextOpen = closeBr + 1;
      if (nextOpen < s.length && s[nextOpen] === "{") {
        const contentClose = findMatchingBrace(s, nextOpen);
        if (contentClose !== -1) {
          const content = s.substring(nextOpen + 1, contentClose);
          const useColor = LATEX_COLORS[colorName] || null;
          for (const seg of parseLatex(content)) {
            out.push({ text: seg.text, bold: seg.bold, color: useColor || seg.color });
          }
          i = contentClose + 1;
        } else {
          out.push({ text: s.substring(nextIdx, nextOpen + 1), bold: false, color: null });
          i = nextOpen + 1;
        }
      } else {
        out.push({ text: s.substring(nextIdx, closeBr + 1), bold: false, color: null });
        i = closeBr + 1;
      }
    }
  }
  if (i < s.length) {
    const rest = s.substring(i);
    if (rest.trim()) out.push({ text: rest, bold: false, color: null });
  }
  if (out.length === 0) out.push({ text: s, bold: false, color: null });
  return out;
}

"""


def _js_add_title() -> str:
    """Title bar with slide-number badge."""
    return """\
function addTitle(slide, text, slideNum) {
  if (!text) return;
  const numStr = String(slideNum).padStart(2, "0");

  if (DESIGN_ID === 1) {
    // Content: sage title bar + navy title / index (same sage as accent palette)
    slide.addShape("rect", {
      x: 0.0, y: 0.0, w: 13.33, h: 0.82,
      fill: { color: C.accent },
    });
    slide.addShape("rect", {
      x: 0.0, y: 0.802, w: 13.33, h: 0.02,
      fill: { color: C.primary },
    });
    slide.addText(numStr, {
      x: 11.35, y: 0.22, w: 1.75, h: 0.42,
      fontSize: 12, bold: true, color: C.primary,
      align: "right", valign: "middle", fontFace: "Calibri",
    });
  } else if (DESIGN_ID === 2) {
    // Sunset Gradient: warm multi-color title bar
    slide.addShape("rect", {
      x: 0.0, y: 0.0, w: 13.33, h: 0.935,
      fill: { color: C.primaryDk },
    });
    slide.addShape("rect", {
      x: 0.0, y: 0.0, w: 13.33, h: 0.80,
      fill: { color: C.primary },
    });
    // Multi-color bottom edge
    slide.addShape("rect", {
      x: 0.0, y: 0.88, w: 4.44, h: 0.06,
      fill: { color: "E8725A" },
    });
    slide.addShape("rect", {
      x: 4.44, y: 0.88, w: 4.44, h: 0.06,
      fill: { color: "F5A623" },
    });
    slide.addShape("rect", {
      x: 8.88, y: 0.88, w: 4.45, h: 0.06,
      fill: { color: "9B59B6" },
    });
    slide.addShape("rect", {
      x: 0.0, y: 0.935, w: 13.33, h: 0.05,
      fill: { color: C.accent },
    });
    // Warm circular badge
    slide.addShape("ellipse", {
      x: 0.22, y: 0.07, w: 0.79, h: 0.79,
      fill: { color: "E8725A", transparency: 50 },
    });
    slide.addShape("ellipse", {
      x: 0.30, y: 0.15, w: 0.63, h: 0.63,
      fill: { color: "E8725A" },
    });
    slide.addText(numStr, {
      x: 0.30, y: 0.15, w: 0.63, h: 0.63,
      fontSize: 18, bold: true, color: "FFFFFF",
      align: "center", valign: "middle", fontFace: "Calibri",
    });
  } else if (DESIGN_ID === 3) {
    // Aurora Neon: dark title bar with vivid neon accents
    slide.addShape("rect", {
      x: 0.0, y: 0.0, w: 13.33, h: 0.935,
      fill: { color: "1A1A2E" },
    });
    slide.addShape("rect", {
      x: 0.0, y: 0.0, w: 13.33, h: 0.80,
      fill: { color: "16213E" },
    });
    // Neon glow accents in the header
    slide.addShape("ellipse", {
      x: 8.5, y: -1.0, w: 5.0, h: 2.8,
      fill: { color: "00E5FF", transparency: 90 },
    });
    slide.addShape("ellipse", {
      x: -1.5, y: -0.8, w: 4.0, h: 2.5,
      fill: { color: "E040FB", transparency: 90 },
    });
    // Triple neon bottom edge
    slide.addShape("rect", {
      x: 0.0, y: 0.87, w: 4.44, h: 0.04,
      fill: { color: "00E5FF" },
    });
    slide.addShape("rect", {
      x: 4.44, y: 0.87, w: 4.44, h: 0.04,
      fill: { color: "E040FB" },
    });
    slide.addShape("rect", {
      x: 8.88, y: 0.87, w: 4.45, h: 0.04,
      fill: { color: "76FF03" },
    });
    slide.addShape("rect", {
      x: 0.0, y: 0.935, w: 13.33, h: 0.05,
      fill: { color: "00E5FF" },
    });
    // Neon badge
    slide.addShape("ellipse", {
      x: 0.22, y: 0.07, w: 0.79, h: 0.79,
      fill: { color: "E040FB", transparency: 55 },
    });
    slide.addShape("ellipse", {
      x: 0.30, y: 0.15, w: 0.63, h: 0.63,
      fill: { color: "E040FB" },
    });
    slide.addText(numStr, {
      x: 0.30, y: 0.15, w: 0.63, h: 0.63,
      fontSize: 18, bold: true, color: "FFFFFF",
      align: "center", valign: "middle", fontFace: "Calibri",
    });
  } else if (DESIGN_ID === 4) {
    // Teal title header: keep it light so background shows through.
    slide.addShape("rect", {
      x: 0.0, y: 0.87, w: 13.33, h: 0.04,
      fill: { color: C.accent },
    });
    // Gold badge
    slide.addShape("ellipse", {
      x: 0.22, y: 0.07, w: 0.79, h: 0.79,
      fill: { color: C.accent, transparency: 45 },
    });
    slide.addShape("ellipse", {
      x: 0.30, y: 0.15, w: 0.63, h: 0.63,
      fill: { color: C.accent },
    });
    slide.addText(numStr, {
      x: 0.30, y: 0.15, w: 0.63, h: 0.63,
      fontSize: 18, bold: true, color: "FFFFFF",
      align: "center", valign: "middle", fontFace: "Calibri",
    });
  } else if (DESIGN_ID === 5) {
    // Design 5: Glassmorphism frosted title bar
    slide.addShape("roundRect", { x: 0.3, y: 0.15, w: 12.73, h: 0.7,
      fill: { color: "FFFFFF", transparency: 55 }, rectRadius: 0.12,
      line: { color: "C7D2FE", width: 0.5 } });
    slide.addShape("ellipse", { x: 0.45, y: 0.22, w: 0.52, h: 0.52,
      fill: { color: "818CF8" } });
    slide.addText(numStr, { x: 0.45, y: 0.22, w: 0.52, h: 0.52,
      fontSize: 14, bold: true, color: "FFFFFF",
      align: "center", valign: "middle", fontFace: "Calibri" });
  } else {
    // Design 0: layered gradient bar with circles and diamonds
    slide.addShape("rect", {
      x: 0.0, y: 0.0, w: 13.33, h: 0.935,
      fill: { color: C.primaryDk },
    });
    slide.addShape("rect", {
      x: 0.0, y: 0.0, w: 13.33, h: 0.80,
      fill: { color: C.primary },
    });
    slide.addShape("ellipse", {
      x: 9.5, y: -1.2, w: 4.5, h: 3.0,
      fill: { color: C.accent, transparency: 85 },
    });
    slide.addShape("ellipse", {
      x: -1.0, y: -0.8, w: 3.0, h: 2.2,
      fill: { color: C.accent, transparency: 90 },
    });
    slide.addShape("rect", {
      x: 0.0, y: 0.92, w: 13.33, h: 0.06,
      fill: { color: C.accent, transparency: 40 },
    });
    slide.addShape("rect", {
      x: 0.0, y: 0.935, w: 13.33, h: 0.05,
      fill: { color: C.accent },
    });
    slide.addShape("ellipse", {
      x: 0.24, y: 0.09, w: 0.75, h: 0.75,
      fill: { color: C.accent, transparency: 50 },
    });
    slide.addShape("ellipse", {
      x: 0.30, y: 0.15, w: 0.63, h: 0.63,
      fill: { color: C.accent },
      shadow: { type: "outer", blur: 4, offset: 1, opacity: 0.25, color: "000000" },
    });
    slide.addText(numStr, {
      x: 0.30, y: 0.15, w: 0.63, h: 0.63,
      fontSize: 18, bold: true, color: C.white,
      align: "center", valign: "middle", fontFace: "Calibri",
    });
    slide.addShape("rect", {
      x: 12.55, y: 0.25, w: 0.35, h: 0.35,
      fill: { color: C.accent, transparency: 55 }, rotate: 45,
    });
    slide.addShape("rect", {
      x: 12.15, y: 0.33, w: 0.22, h: 0.22,
      fill: { color: C.accentLight, transparency: 60 }, rotate: 45,
    });
  }

  // Title text (shared across all designs)
  if (DESIGN_ID === 1) {
    slide.addText(text, {
      x: 0.9, y: 0.14, w: 9.2, h: 0.52,
      fontSize: TITLE_FONT_SIZE, bold: true, color: C.primary,
      fontFace: "Calibri",
    });
  } else if (DESIGN_ID === 5) {
    slide.addText(text, {
      x: 1.15, y: 0.20, w: 11.5, h: 0.58,
      fontSize: TITLE_FONT_SIZE, bold: true, color: "4338CA",
      fontFace: "Trebuchet MS",
    });
  } else {
    slide.addText(text, {
      x: 1.15, y: 0.13, w: 10.8, h: 0.68,
      fontSize: TITLE_FONT_SIZE, bold: true, color: C.white,
      fontFace: "Calibri",
    });
  }
}
"""


def _js_add_bullets() -> str:
    """Render bullet objects into a positioned text box with LaTeX formatting."""
    return """\
function addBullets(slide, bullets, opts) {
  if (!bullets || bullets.length === 0) return;
  const x = (opts && opts.x) || 0.6;
  const y = (opts && opts.y) || 1.4;
  const w = (opts && opts.w) || 6.5;
  const h = (opts && opts.h) || 5.0;

  const rows = [];
  bullets.forEach((b) => {
    if (!b) return;
    const mainText = (b.text || "").trim();
    if (!mainText) return;
    const segs = parseLatex(mainText);
    segs.forEach((seg, si) => {
      const isLast = si === segs.length - 1;
      const runOpts = {
        fontSize: BULLET_FONT_SIZE, fontFace: "Calibri",
        color: seg.color || C.textDark,
        bold: seg.bold || false,
        lineSpacingMultiple: 1.15,
        bullet: { code: "2022", color: C.accent },
        indentLevel: 0,
      };
      if (isLast) runOpts.breakLine = true;
      rows.push({ text: seg.text, options: runOpts });
    });
    (b.sub || []).forEach((s) => {
      const subText = (typeof s === "string") ? s : ((s && typeof s.text === "string") ? s.text : "");
      const sub = subText.trim();
      if (!sub) return;
      const subSegs = parseLatex(sub);
      subSegs.forEach((seg, si) => {
        const isLast = si === subSegs.length - 1;
        const runOpts = {
          fontSize: SUB_BULLET_FONT_SIZE, fontFace: "Calibri",
          color: seg.color || C.textMid,
          bold: seg.bold || false,
          lineSpacingMultiple: 1.1,
          bullet: { code: "2013", color: C.textLight },
          indentLevel: 1,
        };
        if (isLast) runOpts.breakLine = true;
        rows.push({ text: seg.text, options: runOpts });
      });
    });
  });
  if (rows.length === 0) return;

  slide.addText(rows, { x, y, w, h, valign: "top", paraSpaceAfter: 6, align: "left" });
}
"""

def _js_add_cover_slide() -> str:
    return """\
function addCoverSlide(pptx, meta) {
  const slide = pptx.addSlide();

  if (DESIGN_ID === 1) {
    // Cover: white + stacked navy/sage left bar + large BR gray + small TL gray
    slide.background = { color: C.white };
    slide.addShape("rect", { x: 0.0, y: 0.0, w: 2.0, h: 1.0,
      fill: { color: C.accentMist } });
    slide.addShape("rect", { x: 7.85, y: 2.85, w: 5.48, h: 4.65,
      fill: { color: C.accentMist } });
    addDesign1CoverLeftBars(slide);
  } else if (DESIGN_ID === 2) {
    // ---- Sunset Gradient cover: dramatic warm multi-color ----
    slide.background = { color: C.primaryDk };
    slide.addShape("rect", { x: 0, y: 0, w: 13.33, h: 7.5,
      fill: { color: C.primary, transparency: 20 } });
    // Large diagonal warm color blocks
    slide.addShape("rect", { x: -5, y: -4, w: 14, h: 16,
      fill: { color: "E8725A", transparency: 88 }, rotate: 20 });
    slide.addShape("rect", { x: 4, y: -3, w: 14, h: 14,
      fill: { color: "F5A623", transparency: 90 }, rotate: -15 });
    slide.addShape("rect", { x: 8, y: 2, w: 10, h: 10,
      fill: { color: "9B59B6", transparency: 88 }, rotate: 30 });
    // Floating warm orbs
    slide.addShape("ellipse", { x: -2.0, y: -2.0, w: 7.0, h: 7.0,
      fill: { color: "F39C12", transparency: 88 } });
    slide.addShape("ellipse", { x: 9.0, y: 4.0, w: 6.0, h: 6.0,
      fill: { color: "E8725A", transparency: 85 } });
    slide.addShape("ellipse", { x: 10.5, y: -1.5, w: 4.5, h: 4.5,
      fill: { color: "9B59B6", transparency: 82 } });
    slide.addShape("ellipse", { x: -1.0, y: 5.0, w: 5.0, h: 5.0,
      fill: { color: "F5A623", transparency: 87 } });
    // Diagonal accent lines
    slide.addShape("rect", { x: -1.0, y: 3.0, w: 16.0, h: 0.08,
      fill: { color: "F5A623", transparency: 55 }, rotate: -6 });
    slide.addShape("rect", { x: -1.0, y: 3.3, w: 16.0, h: 0.05,
      fill: { color: "E8725A", transparency: 50 }, rotate: -6 });
    // Frosted glass panel
    slide.addShape("roundRect", {
      x: 1.8, y: 1.6, w: 9.7, h: 4.5,
      fill: { color: C.primary, transparency: 30 },
      rectRadius: 0.3,
      line: { color: "E8725A", width: 3.0 },
      shadow: { type: "outer", blur: 18, offset: 5, opacity: 0.18, color: "9B59B6" },
    });
    // Multi-color bottom strip
    slide.addShape("rect", { x: 0.0, y: 7.0, w: 4.44, h: 0.08,
      fill: { color: "E8725A" } });
    slide.addShape("rect", { x: 4.44, y: 7.0, w: 4.44, h: 0.08,
      fill: { color: "F5A623" } });
    slide.addShape("rect", { x: 8.88, y: 7.0, w: 4.45, h: 0.08,
      fill: { color: "9B59B6" } });
    slide.addShape("rect", { x: 0.0, y: 7.08, w: 13.33, h: 0.42,
      fill: { color: C.primaryDk } });
  } else if (DESIGN_ID === 3) {
    // ---- Aurora Neon cover: dark bg with vivid neon geometry ----
    slide.background = { color: "0F0F23" };
    // Large neon glow fields
    slide.addShape("ellipse", { x: -4.0, y: -3.0, w: 12.0, h: 12.0,
      fill: { color: "00E5FF", transparency: 92 } });
    slide.addShape("ellipse", { x: 6.0, y: 3.0, w: 11.0, h: 11.0,
      fill: { color: "E040FB", transparency: 92 } });
    slide.addShape("ellipse", { x: 2.0, y: -4.0, w: 8.0, h: 8.0,
      fill: { color: "76FF03", transparency: 94 } });
    // Neon diamond shapes
    slide.addShape("rect", { x: 1.0, y: 0.5, w: 2.5, h: 2.5,
      fill: { color: "00E5FF", transparency: 82 }, rotate: 45 });
    slide.addShape("rect", { x: 10.0, y: 4.5, w: 3.0, h: 3.0,
      fill: { color: "E040FB", transparency: 80 }, rotate: 45 });
    slide.addShape("rect", { x: 11.0, y: 0.5, w: 1.5, h: 1.5,
      fill: { color: "76FF03", transparency: 78 }, rotate: 45 });
    slide.addShape("rect", { x: 0.5, y: 5.5, w: 2.0, h: 2.0,
      fill: { color: "E040FB", transparency: 82 }, rotate: 45 });
    // Neon line accents
    slide.addShape("rect", { x: -1.0, y: 3.0, w: 16.0, h: 0.06,
      fill: { color: "00E5FF", transparency: 50 }, rotate: -5 });
    slide.addShape("rect", { x: -1.0, y: 3.3, w: 16.0, h: 0.04,
      fill: { color: "E040FB", transparency: 55 }, rotate: -5 });
    slide.addShape("rect", { x: -1.0, y: 3.55, w: 16.0, h: 0.03,
      fill: { color: "76FF03", transparency: 60 }, rotate: -5 });
    // Glowing dot constellations
    var neonDots = [[2.2,1.0],[2.5,0.6],[2.8,1.2],[3.2,0.8],[10.8,5.8],[11.2,6.2],[11.5,5.6],[12.0,6.0]];
    neonDots.forEach(function(d) {
      slide.addShape("ellipse", { x: d[0], y: d[1], w: 0.15, h: 0.15,
        fill: { color: "00E5FF", transparency: 40 } });
    });
    // Dark frosted panel with neon border
    slide.addShape("roundRect", {
      x: 1.8, y: 1.6, w: 9.7, h: 4.5,
      fill: { color: "16213E", transparency: 20 },
      rectRadius: 0.25,
      line: { color: "00E5FF", width: 2.5 },
      shadow: { type: "outer", blur: 20, offset: 0, opacity: 0.25, color: "00E5FF" },
    });
    // Triple neon bottom strip
    slide.addShape("rect", { x: 0.0, y: 7.0, w: 4.44, h: 0.06,
      fill: { color: "00E5FF" } });
    slide.addShape("rect", { x: 4.44, y: 7.0, w: 4.44, h: 0.06,
      fill: { color: "E040FB" } });
    slide.addShape("rect", { x: 8.88, y: 7.0, w: 4.45, h: 0.06,
      fill: { color: "76FF03" } });
    slide.addShape("rect", { x: 0.0, y: 7.06, w: 13.33, h: 0.44,
      fill: { color: "1A1A2E" } });
  } else if (DESIGN_ID === 4) {
    // ---- Teal cover with checkered corner pixels (screenshot vibe) ----
    var FRAME = 0.24;
    var OUTER = "C7F2D0"; // light green border
    var TEAL = "0F6A6D";
    var SQ_L = OUTER;     // merge with border
    var SQ_D = "A8E6BF"; // subtle darker green for checker
    var SQ_T_L = 90;     // very light visibility
    var SQ_T_D = 82;     // slightly less transparent for contrast
    slide.background = { color: OUTER };
    slide.addShape("rect", {
      x: FRAME, y: FRAME,
      w: 13.33 - 2 * FRAME,
      h: 7.5 - 2 * FRAME,
      fill: { color: TEAL },
    });

    // Corner "pixel" checker: 4x4 blocks, each 3x bigger.
    var sq = 0.36;
    var grid = 4;
    var off = 0.0;
    function drawCorner(x0, y0) {
      for (var gx = 0; gx < grid; gx++) {
        for (var gy = 0; gy < grid; gy++) {
          const useLight = ((gx + gy) % 2 === 0);
          slide.addShape("rect", {
            x: x0 + gx * sq,
            y: y0 + gy * sq,
            w: sq, h: sq,
            fill: { color: useLight ? SQ_L : SQ_D, transparency: useLight ? SQ_T_L : SQ_T_D },
          });
        }
      }
    }
    drawCorner(off, off); // TL
    drawCorner(13.33 - off - grid * sq, off); // TR
    drawCorner(off, 7.5 - off - grid * sq); // BL
    drawCorner(13.33 - off - grid * sq, 7.5 - off - grid * sq); // BR
  } else if (DESIGN_ID === 5) {
    // Design 5: Glassmorphism gradient bg + frosted title card
    slide.background = { color: "312E81" };
    slide.addShape("ellipse", { x: -4, y: -4, w: 12, h: 12,
      fill: { color: "4338CA", transparency: 40 } });
    slide.addShape("ellipse", { x: 6, y: 2, w: 10, h: 10,
      fill: { color: "6366F1", transparency: 50 } });
    slide.addShape("ellipse", { x: 2, y: -3, w: 7, h: 7,
      fill: { color: "818CF8", transparency: 55 } });
    slide.addShape("roundRect", {
      x: 1.5, y: 1.5, w: 10.3, h: 4.8,
      fill: { color: "FFFFFF", transparency: 70 }, rectRadius: 0.3,
      line: { color: "C4B5FD", width: 1.5 },
      shadow: mkShadow(20, 6, 0.12, "312E81") });
  } else {
    // ---- Design 0: Circles & Bokeh cover ----
    slide.background = { color: C.primaryDk };
    // Multi-layered circle clusters
    slide.addShape("ellipse", { x: 8.0, y: -3.0, w: 9.0, h: 9.0,
      fill: { color: C.accent, transparency: 92 } });
    slide.addShape("ellipse", { x: 9.5, y: -2.0, w: 6.5, h: 6.5,
      fill: { color: C.accent, transparency: 85 } });
    slide.addShape("ellipse", { x: 10.5, y: -1.0, w: 4.0, h: 4.0,
      fill: { color: C.accentLight, transparency: 80 } });
    slide.addShape("ellipse", { x: -3.0, y: 4.0, w: 7.0, h: 7.0,
      fill: { color: C.accent, transparency: 92 } });
    slide.addShape("ellipse", { x: -1.5, y: 5.0, w: 4.5, h: 4.5,
      fill: { color: C.accent, transparency: 85 } });
    slide.addShape("ellipse", { x: -0.5, y: 5.8, w: 2.0, h: 2.0,
      fill: { color: C.accentLight, transparency: 78 } });
    // Floating diamonds
    slide.addShape("rect", { x: 1.5, y: 1.2, w: 0.35, h: 0.35,
      fill: { color: C.accent, transparency: 70 }, rotate: 45 });
    slide.addShape("rect", { x: 11.5, y: 5.8, w: 0.5, h: 0.5,
      fill: { color: C.accent, transparency: 65 }, rotate: 45 });
    slide.addShape("rect", { x: 12.2, y: 5.3, w: 0.25, h: 0.25,
      fill: { color: C.accentLight, transparency: 55 }, rotate: 45 });
    // Dot constellations
    var dots = [[2.0,1.0],[2.3,0.7],[2.6,1.1],[11.0,6.0],[11.3,6.3],[11.6,5.9]];
    dots.forEach(function(d) {
      slide.addShape("ellipse", { x: d[0], y: d[1], w: 0.12, h: 0.12,
        fill: { color: C.accentLight, transparency: 50 } });
    });
    // Accent lines
    slide.addShape("rect", { x: 3.5, y: 1.9, w: 6.0, h: 0.04,
      fill: { color: C.accent, transparency: 40 } });
    slide.addShape("rect", { x: 4.2, y: 1.95, w: 4.8, h: 0.05,
      fill: { color: C.accent } });
    // Bottom strip
    slide.addShape("rect", { x: 0.0, y: 7.05, w: 13.33, h: 0.06,
      fill: { color: C.accent, transparency: 60 } });
    slide.addShape("rect", { x: 0.0, y: 7.12, w: 13.33, h: 0.38,
      fill: { color: C.accent } });
  }

  function _cleanMetaValue(v) {
    if (v === null || v === undefined) return "";
    var s = String(v).trim();
    return s.toLowerCase() === "unknown" ? "" : s;
  }

  // ---- Shared text content across all designs ----
  if (DESIGN_ID === 1) {
    var rawT = meta.title || "Presentation";
    slide.addText(rawT, {
      x: 1.12, y: 2.28, w: 7.0, h: 1.45,
      fontSize: 28, bold: true, color: C.primary, align: "left",
      fontFace: "Calibri", lineSpacingMultiple: 1.08,
    });
    slide.addShape("rect", {
      x: 1.12, y: 3.88, w: 5.0, h: 0.028,
      fill: { color: C.accent },
    });
    var authorLine = (meta.author || "").trim();
    var subLine = authorLine || "Clean • Clear • Focused";
    slide.addText(subLine, {
      x: 1.12, y: 4.05, w: 9.0, h: 0.55,
      fontSize: 14, color: C.textDark, align: "left",
      fontFace: "Calibri",
    });
    var orgTop = _cleanMetaValue(meta.organization);
    if (orgTop) {
      slide.addText(orgTop + " | 01", {
        x: 8.0, y: 0.26, w: 4.85, h: 0.45,
        fontSize: 11, bold: true, color: C.primary, align: "right",
        fontFace: "Calibri",
      });
    }
    var dateBL = _cleanMetaValue(meta["publish date"]);
    var blParts = [];
    if (authorLine) blParts.push(authorLine);
    if (dateBL) blParts.push(dateBL);
    if (blParts.length) {
      slide.addText(blParts.join(" | "), {
        x: 1.12, y: 6.48, w: 7.8, h: 0.45,
        fontSize: 11, color: C.textMid, align: "left",
        fontFace: "Calibri",
      });
    }
  } else {
    // Title
    slide.addText(meta.title || "Presentation", {
      x: 2.2, y: 2.2, w: 8.9, h: 1.6,
      fontSize: 34, bold: true, color: C.white, align: "center",
      fontFace: "Calibri", lineSpacingMultiple: 1.15,
    });

    // Accent line below title
    slide.addShape("rect", { x: 4.2, y: 3.95, w: 4.8, h: 0.05,
      fill: { color: C.accent } });

    // Authors
    slide.addText(meta.author || "", {
      x: 2.2, y: 4.2, w: 8.9, h: 0.8,
      fontSize: 18, color: C.accentLight, align: "center",
      fontFace: "Calibri",
    });

    var org = _cleanMetaValue(meta.organization);
    var date = _cleanMetaValue(meta["publish date"]);
    var sub = [org, date].filter(Boolean).join("  \\u00B7  ");
    if (sub) {
      slide.addText(sub, {
        x: 2.2, y: 5.2, w: 8.9, h: 0.5,
        fontSize: 14, color: C.textLight, align: "center",
        fontFace: "Calibri",
      });
    }
  }
}
"""


def _js_add_thanks_slide() -> str:
    return """\
function addThanksSlide(pptx) {
  const slide = pptx.addSlide();

  if (DESIGN_ID === 1) {
    // Thank you: sage canvas + gray corners + navy/white side accents
    slide.background = { color: C.thanksBg };
    slide.addShape("rect", { x: 0.0, y: 0.0, w: 1.95, h: 0.98,
      fill: { color: C.accentMist } });
    slide.addShape("rect", { x: 11.05, y: 6.42, w: 2.28, h: 1.08,
      fill: { color: C.accentMist } });
    addDesign1ThanksSideAccents(slide);
  } else if (DESIGN_ID === 2) {
    // ---- Sunset Gradient thanks ----
    slide.background = { color: C.primaryDk };
    slide.addShape("rect", { x: 0, y: 0, w: 13.33, h: 7.5,
      fill: { color: C.primary, transparency: 20 } });
    // Warm diagonal blocks
    slide.addShape("rect", { x: -5, y: -4, w: 14, h: 16,
      fill: { color: "E8725A", transparency: 88 }, rotate: 20 });
    slide.addShape("rect", { x: 4, y: -3, w: 14, h: 14,
      fill: { color: "F5A623", transparency: 90 }, rotate: -15 });
    slide.addShape("rect", { x: 8, y: 2, w: 10, h: 10,
      fill: { color: "9B59B6", transparency: 88 }, rotate: 30 });
    // Floating warm orbs
    slide.addShape("ellipse", { x: -2.0, y: -2.0, w: 7.0, h: 7.0,
      fill: { color: "F39C12", transparency: 88 } });
    slide.addShape("ellipse", { x: 9.0, y: 4.0, w: 6.0, h: 6.0,
      fill: { color: "E8725A", transparency: 85 } });
    slide.addShape("ellipse", { x: -1.0, y: 5.0, w: 5.0, h: 5.0,
      fill: { color: "F5A623", transparency: 87 } });
    // Frosted panel
    slide.addShape("roundRect", {
      x: 2.5, y: 2.0, w: 8.3, h: 3.8,
      fill: { color: C.primary, transparency: 30 },
      rectRadius: 0.3,
      line: { color: "E8725A", width: 3.0 },
      shadow: { type: "outer", blur: 18, offset: 5, opacity: 0.18, color: "9B59B6" },
    });
    // Multi-color bottom strip
    slide.addShape("rect", { x: 0.0, y: 7.0, w: 4.44, h: 0.08,
      fill: { color: "E8725A" } });
    slide.addShape("rect", { x: 4.44, y: 7.0, w: 4.44, h: 0.08,
      fill: { color: "F5A623" } });
    slide.addShape("rect", { x: 8.88, y: 7.0, w: 4.45, h: 0.08,
      fill: { color: "9B59B6" } });
    slide.addShape("rect", { x: 0.0, y: 7.08, w: 13.33, h: 0.42,
      fill: { color: C.primaryDk } });
  } else if (DESIGN_ID === 3) {
    // ---- Aurora Neon thanks ----
    slide.background = { color: "0F0F23" };
    // Neon glow fields
    slide.addShape("ellipse", { x: -4.0, y: -3.0, w: 12.0, h: 12.0,
      fill: { color: "00E5FF", transparency: 92 } });
    slide.addShape("ellipse", { x: 6.0, y: 3.0, w: 11.0, h: 11.0,
      fill: { color: "E040FB", transparency: 92 } });
    slide.addShape("ellipse", { x: 2.0, y: -4.0, w: 8.0, h: 8.0,
      fill: { color: "76FF03", transparency: 94 } });
    // Neon diamonds
    slide.addShape("rect", { x: 1.0, y: 0.5, w: 2.5, h: 2.5,
      fill: { color: "00E5FF", transparency: 82 }, rotate: 45 });
    slide.addShape("rect", { x: 10.0, y: 4.5, w: 3.0, h: 3.0,
      fill: { color: "E040FB", transparency: 80 }, rotate: 45 });
    slide.addShape("rect", { x: 0.5, y: 5.5, w: 2.0, h: 2.0,
      fill: { color: "76FF03", transparency: 82 }, rotate: 45 });
    // Neon line accents
    slide.addShape("rect", { x: -1.0, y: 3.0, w: 16.0, h: 0.06,
      fill: { color: "00E5FF", transparency: 50 }, rotate: -5 });
    slide.addShape("rect", { x: -1.0, y: 3.3, w: 16.0, h: 0.04,
      fill: { color: "E040FB", transparency: 55 }, rotate: -5 });
    // Glowing dots
    var neonDots2 = [[2.2,1.0],[2.5,0.6],[11.0,6.0],[11.5,5.6]];
    neonDots2.forEach(function(d) {
      slide.addShape("ellipse", { x: d[0], y: d[1], w: 0.15, h: 0.15,
        fill: { color: "00E5FF", transparency: 40 } });
    });
    // Dark frosted panel with neon border
    slide.addShape("roundRect", {
      x: 2.5, y: 2.0, w: 8.3, h: 3.8,
      fill: { color: "16213E", transparency: 20 },
      rectRadius: 0.25,
      line: { color: "00E5FF", width: 2.5 },
      shadow: { type: "outer", blur: 20, offset: 0, opacity: 0.25, color: "00E5FF" },
    });
    // Triple neon bottom strip
    slide.addShape("rect", { x: 0.0, y: 7.0, w: 4.44, h: 0.06,
      fill: { color: "00E5FF" } });
    slide.addShape("rect", { x: 4.44, y: 7.0, w: 4.44, h: 0.06,
      fill: { color: "E040FB" } });
    slide.addShape("rect", { x: 8.88, y: 7.0, w: 4.45, h: 0.06,
      fill: { color: "76FF03" } });
    slide.addShape("rect", { x: 0.0, y: 7.06, w: 13.33, h: 0.44,
      fill: { color: "1A1A2E" } });
  } else if (DESIGN_ID === 4) {
    // ---- Thanks background: teal framed with green + checkered corners ----
    var FRAME = 0.24;
    var OUTER = "C7F2D0"; // light green border (less contrasting than violet)
    var TEAL = "0F6A6D";
    var SQ_L = OUTER;     // merge with border
    var SQ_D = "A8E6BF"; // subtle darker green for checker
    var SQ_T_L = 90;     // very light visibility
    var SQ_T_D = 82;     // slightly less transparent for contrast

    slide.background = { color: OUTER };
    slide.addShape("rect", {
      x: FRAME, y: FRAME,
      w: 13.33 - 2 * FRAME,
      h: 7.5 - 2 * FRAME,
      fill: { color: TEAL },
    });

    // Corner "pixel" checker: 4x4 blocks, each 3x bigger.
    var sq = 0.36;
    var grid = 4;
    var off = 0.0;
    function drawCorner(x0, y0) {
      for (var gx = 0; gx < grid; gx++) {
        for (var gy = 0; gy < grid; gy++) {
          const useLight = ((gx + gy) % 2 === 0);
          slide.addShape("rect", {
            x: x0 + gx * sq,
            y: y0 + gy * sq,
            w: sq, h: sq,
            fill: { color: useLight ? SQ_L : SQ_D, transparency: useLight ? SQ_T_L : SQ_T_D },
          });
        }
      }
    }
    drawCorner(off, off); // TL
    drawCorner(13.33 - off - grid * sq, off); // TR
    drawCorner(off, 7.5 - off - grid * sq); // BL
    drawCorner(13.33 - off - grid * sq, 7.5 - off - grid * sq); // BR

    // Subtle bottom accent
    slide.addShape("rect", {
      x: 0.0, y: 7.08, w: 13.33, h: 0.05,
      fill: { color: C.accent, transparency: 35 },
    });
  } else if (DESIGN_ID === 5) {
    // Design 5: Glassmorphism
    slide.background = { color: "312E81" };
    slide.addShape("ellipse", { x: -4, y: -4, w: 12, h: 12,
      fill: { color: "4338CA", transparency: 40 } });
    slide.addShape("ellipse", { x: 6, y: 2, w: 10, h: 10,
      fill: { color: "6366F1", transparency: 50 } });
    slide.addShape("roundRect", {
      x: 2.5, y: 2.0, w: 8.3, h: 3.5,
      fill: { color: "FFFFFF", transparency: 70 }, rectRadius: 0.3,
      line: { color: "C4B5FD", width: 1.5 } });
    slide.addText("Thank You", { x: 3.0, y: 2.4, w: 7.3, h: 1.4,
      fontSize: 48, bold: true, color: "1E1B4B", align: "center",
      fontFace: "Trebuchet MS" });
    slide.addText("Questions & Discussion", { x: 3.0, y: 4.0, w: 7.3, h: 0.7,
      fontSize: 18, color: "4338CA", align: "center", fontFace: "Calibri" });
  } else {
    // ---- Design 0: Circles & Bokeh thanks ----
    slide.background = { color: C.primaryDk };
    slide.addShape("ellipse", { x: 8.0, y: -3.0, w: 9.0, h: 9.0,
      fill: { color: C.accent, transparency: 92 } });
    slide.addShape("ellipse", { x: 9.5, y: -2.0, w: 6.5, h: 6.5,
      fill: { color: C.accent, transparency: 85 } });
    slide.addShape("ellipse", { x: 10.5, y: -1.0, w: 4.0, h: 4.0,
      fill: { color: C.accentLight, transparency: 80 } });
    slide.addShape("ellipse", { x: -3.0, y: 4.0, w: 7.0, h: 7.0,
      fill: { color: C.accent, transparency: 92 } });
    slide.addShape("ellipse", { x: -1.5, y: 5.0, w: 4.5, h: 4.5,
      fill: { color: C.accent, transparency: 85 } });
    slide.addShape("ellipse", { x: -0.5, y: 5.8, w: 2.0, h: 2.0,
      fill: { color: C.accentLight, transparency: 78 } });
    slide.addShape("rect", { x: 1.5, y: 1.2, w: 0.35, h: 0.35,
      fill: { color: C.accent, transparency: 70 }, rotate: 45 });
    slide.addShape("rect", { x: 11.5, y: 5.8, w: 0.5, h: 0.5,
      fill: { color: C.accent, transparency: 65 }, rotate: 45 });
    slide.addShape("rect", { x: 12.2, y: 5.3, w: 0.25, h: 0.25,
      fill: { color: C.accentLight, transparency: 55 }, rotate: 45 });
    var dots = [[2.0,1.0],[2.3,0.7],[2.6,1.1],[11.0,6.0],[11.3,6.3],[11.6,5.9]];
    dots.forEach(function(d) {
      slide.addShape("ellipse", { x: d[0], y: d[1], w: 0.12, h: 0.12,
        fill: { color: C.accentLight, transparency: 50 } });
    });
    slide.addShape("rect", { x: 0.0, y: 7.05, w: 13.33, h: 0.06,
      fill: { color: C.accent, transparency: 60 } });
    slide.addShape("rect", { x: 0.0, y: 7.12, w: 13.33, h: 0.38,
      fill: { color: C.accent } });
  }

  // ---- Shared text across all designs ----
  if (DESIGN_ID === 5) {
    // Text already fully rendered in the Design 5 branch above.
    return;
  } else if (DESIGN_ID === 1) {
    slide.addShape("rect", { x: 4.8, y: 2.55, w: 3.6, h: 0.02,
      fill: { color: C.accent } });
    slide.addText("THANK YOU", {
      x: 3.0, y: 2.78, w: 7.3, h: 1.35,
      fontSize: 44, bold: true, color: C.primary, align: "center",
      fontFace: "Calibri",
    });
    slide.addShape("rect", { x: 4.8, y: 4.28, w: 3.6, h: 0.02,
      fill: { color: C.accent } });
    slide.addText("Questions & Discussion", {
      x: 3.0, y: 4.55, w: 7.3, h: 0.75,
      fontSize: 18, color: C.primaryDk, align: "center",
      fontFace: "Calibri",
    });
  } else {
    slide.addShape("rect", { x: 4.8, y: 2.55, w: 3.6, h: 0.05,
      fill: { color: C.accent } });

    slide.addText("Thank You", {
      x: 3.0, y: 2.8, w: 7.3, h: 1.4,
      fontSize: 48, bold: true, color: C.white, align: "center",
      fontFace: "Calibri",
    });

    slide.addShape("rect", { x: 4.8, y: 4.3, w: 3.6, h: 0.05,
      fill: { color: C.accent } });

    slide.addText("Questions & Discussion", {
      x: 3.0, y: 4.6, w: 7.3, h: 0.7,
      fontSize: 20, color: C.accentLight, align: "center",
      fontFace: "Calibri",
    });
  }
}
"""
