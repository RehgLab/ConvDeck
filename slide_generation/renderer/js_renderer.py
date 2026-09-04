import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List
from slide_generation.renderer.js_codegen import (
    THEMES,
    _js_header,
    _js_font_config,
    _js_configure_presentation,
    _js_parse_latex,
    _js_add_cover_slide,
    _js_add_title,
    _js_add_bullets,
    _js_add_thanks_slide,
)


# ---------------------------------------------------------------------------
# Visual path resolution  (no heavy deps)
# ---------------------------------------------------------------------------

def _norm_digits(s: str) -> str:
    m = re.search(r'(\d+)(?!.*\d)', str(s))
    if not m:
        return str(s)
    v = m.group(1).lstrip("0")
    return v if v != "" else "0"


def _stem_name(p: str) -> str:
    return Path(str(p)).name


def _extract_figure_numbers(caption: str):
    out = set()
    if not caption:
        return out
    caps = str(caption)
    for m in re.finditer(r'(?:(?:fig(?:ure)?|图)\.?\s*)(\d+)\s*[–-]\s*(\d+)', caps, flags=re.I):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b:
            out.update(range(a, b + 1))
        else:
            out.update(range(b, a + 1))
    for m in re.finditer(r'(?:(?:fig(?:ure)?|图)\.?\s*)(\d+)', caps, flags=re.I):
        out.add(int(m.group(1)))
    return out


def _extract_table_numbers(caption: str):
    out = set()
    if not caption:
        return out
    caps = str(caption)
    for m in re.finditer(r'(?:(?:tab(?:le)?|表)\.?\s*)(\d+)\s*[–-]\s*(\d+)', caps, flags=re.I):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b:
            out.update(range(a, b + 1))
        else:
            out.update(range(b, a + 1))
    for m in re.finditer(r'(?:(?:tab(?:le)?|表)\.?\s*)(\d+)', caps, flags=re.I):
        out.add(int(m.group(1)))
    return out


def _build_caption_index(mapping: dict, extractor):
    idx = {}
    for k, meta in (mapping or {}).items():
        caps = (meta or {}).get("caption", "")
        for num in extractor(caps):
            idx.setdefault(num, set()).add(str(k))
    return idx


def _resolve_and_check(path1: Path, base_dir: Path) -> Path:
    if path1.exists():
        return path1
    p2 = base_dir / path1.name
    if p2.exists():
        return p2
    candidates = [p for p in base_dir.glob("*") if p.name.lower() == path1.name.lower()]
    if candidates:
        return candidates[0]
    existing = "\n".join(sorted(p.name for p in base_dir.glob("*.*"))[:80])
    raise FileNotFoundError(
        f"Missing visual file: {path1}\n"
        f"Tried: {path1}, {p2}. Base dir: {base_dir}\n"
        f"Existing (first 80):\n{existing}"
    )


def _match_by_id_or_caption(img_id, name, mapping, cap_index, kind):
    img_id_norm = _norm_digits(img_id)
    name_norm = _norm_digits(_stem_name(name))

    if img_id in mapping:
        return mapping[img_id], img_id
    if img_id_norm in mapping:
        return mapping[img_id_norm], img_id_norm
    if name_norm in mapping:
        return mapping[name_norm], name_norm

    for norm_val in (img_id_norm, name_norm):
        if norm_val.isdigit():
            num = int(norm_val)
            cand = sorted(
                cap_index.get(num, []),
                key=lambda x: int(_norm_digits(x)) if _norm_digits(x).isdigit() else 10**9,
            )
            if cand:
                return mapping[cand[0]], cand[0]

    avail = sorted(
        mapping.keys(),
        key=lambda x: int(_norm_digits(x)) if _norm_digits(x).isdigit() else 10**9,
    )
    raise KeyError(
        f"{kind.capitalize()} id not found. "
        f"given_id='{img_id}', file='{name}', "
        f"norm(id)={img_id_norm}, norm(file)={name_norm}. "
        f"Available keys (first 40): {avail[:40]}"
    )


def _resolve_formula_mode1_path(fname: str, args) -> Path:
    stem = Path(fname).stem
    match = re.search(r'(\d+)(?!.*\d)', stem)
    if not match:
        raise ValueError(f"Cannot extract index from formula filename: {fname}")
    i = match.group(1)
    return Path(
        f"<{args.model_name_t}_{args.model_name_v}>_images_and_tables"
        f"/{args.paper_name}/{args.paper_name}-formula-{i}.png"
    )


def resolve_visual_paths(slide_info: dict, args) -> List[Path]:
    """
    Resolve logical image/table/formula filenames from the slide plan
    to actual filesystem paths.
    """
    paper = args.paper_name
    prefix = f"<{args.model_name_t}_{args.model_name_v}>_images_and_tables"
    base_dir = Path(prefix) / paper

    images_json_path = Path(prefix) / f"{paper}_images.json"
    tables_json_path = Path(prefix) / f"{paper}_tables.json"
    images_json = (
        json.load(open(images_json_path, "r", encoding="utf-8"))
        if images_json_path.exists() else {}
    )
    tables_json = (
        json.load(open(tables_json_path, "r", encoding="utf-8"))
        if tables_json_path.exists() else {}
    )

    formula_mode = getattr(args, "formula_mode", 1)
    formulas_dir = (
        Path("contents") / paper / "formula_images"
        if formula_mode == 3
        else base_dir
    )

    fignum_idx = _build_caption_index(images_json, _extract_figure_numbers)
    tbnum_idx = _build_caption_index(tables_json, _extract_table_numbers)

    images_json = {str(k): v for k, v in images_json.items()}
    tables_json = {str(k): v for k, v in tables_json.items()}

    # --- images ---
    image_paths: List[Path] = []
    for img_str in slide_info.get("images", []):
        name = _stem_name(img_str)
        m = re.search(
            r'(?:picture|image|fig|table|formula)[-_](\d+)(?=\.[A-Za-z0-9]+$)',
            name, re.I,
        )
        img_id = m.group(1) if m else _norm_digits(name)
        rec, _ = _match_by_id_or_caption(img_id, name, images_json, fignum_idx, "image")
        target = rec.get("image_path") or rec.get("path") or rec.get("file")
        if not target:
            raise KeyError(f"Image record has no path field: {rec}")
        image_paths.append(_resolve_and_check(Path(target), base_dir))

    # --- tables ---
    table_paths: List[Path] = []
    for tb_str in slide_info.get("tables", []):
        name = _stem_name(tb_str)
        m = re.search(
            r'(?:table|tab|picture|image|fig|formula)[-_](\d+)(?=\.[A-Za-z0-9]+$)',
            name, re.I,
        )
        tb_id = m.group(1) if m else _norm_digits(name)
        rec, _ = _match_by_id_or_caption(tb_id, name, tables_json, tbnum_idx, "table")
        target = rec.get("table_path") or rec.get("image_path") or rec.get("path") or rec.get("file")
        if not target:
            raise KeyError(f"Table record has no path field: {rec}")
        table_paths.append(_resolve_and_check(Path(target), base_dir))

    # --- formulas ---
    formula_paths: List[Path] = []
    for fname in slide_info.get("formulas", []):
        if formula_mode == 3:
            final_path = formulas_dir / fname
        else:
            final_path = _resolve_formula_mode1_path(fname, args)
        formula_paths.append(_resolve_and_check(Path(final_path), base_dir))

    return image_paths + table_paths + formula_paths


def _resolve_all_visuals(plan: Dict, args: Any) -> Dict:
    """
    Walk every slide in *plan*, resolve all image/table/formula paths,
    and store the absolute paths under ``_resolved_visuals``.
    """
    resolved_plan = dict(plan)
    resolved_slides: List[Dict] = []

    for slide_info in plan.get("slides", []):
        slide_copy = dict(slide_info)
        try:
            visual_paths = resolve_visual_paths(slide_info, args)
            abs_paths = [str(Path(p).resolve()) for p in visual_paths]
        except (FileNotFoundError, KeyError) as exc:
            print(f"[pptxgenjs][warn] Could not resolve visuals for "
                  f"slide '{slide_info.get('subsection', '?')}': {exc}")
            abs_paths = []

        slide_copy["_resolved_visuals"] = abs_paths
        resolved_slides.append(slide_copy)

    resolved_plan["slides"] = resolved_slides
    return resolved_plan


def _js_add_paragraph() -> str:
    """Render a paragraph string into a positioned text box with LaTeX formatting."""
    return """\
function addParagraph(slide, text, opts) {
  if (!text) return;
  const segs = parseLatex(text.trim());
  const x = (opts && opts.x) || 0.6;
  const y = (opts && opts.y) || 1.4;
  const w = (opts && opts.w) || 11.5;
  const h = (opts && opts.h) || 5.0;
  const rows = segs.map((seg, idx) => ({
    text: seg.text,
    options: {
      fontSize: BULLET_FONT_SIZE, fontFace: "Calibri",
      color: seg.color || C.textDark,
      bold: seg.bold || false,
      lineSpacingMultiple: 1.2,
      breakLine: idx === segs.length - 1,
    },
  }));
  slide.addText(rows, {
    x, y, w, h, align: "left", valign: "top", wrap: true,
  });
}
"""


def _js_add_body_content() -> str:
    """Dispatch to bullets or paragraph; draws a soft panel behind the text."""
    return """\
function addTopAccentLine(slide, x, y, w) {
  if (DESIGN_ID === 2) {
    // Match addTextPanel (Sunset): coral top bar only — same coords as full panel.
    slide.addShape("roundRect", {
      x: x - 0.06, y: y - 0.04, w: w + 0.12, h: 0.06,
      fill: { color: C.accent },
      rectRadius: 0.04,
    });
  } else if (DESIGN_ID === 3) {
    // Aurora Neon: use neon cyan for the top edge line.
    slide.addShape("roundRect", {
      x: x - 0.06, y: y - 0.04, w: w + 0.12, h: 0.06,
      fill: { color: "00E5FF" },
      rectRadius: 0.04,
    });
  } else {
    slide.addShape("roundRect", {
      x: x - 0.08, y: y - 0.06, w: w + 0.16, h: 0.06,
      fill: { color: C.accent },
      rectRadius: 0.04,
    });
  }
}

function addBodyContent(slide, info, opts) {
  const bullets = info.bullets || [];
  const paragraph = (info.paragraph || "").trim();
  const hasContent = (paragraph && bullets.length === 0) || bullets.length > 0;
  if (!hasContent) return;

  // Light panel behind text area
  if (opts) {
    if (opts.panelStyle === "topLineOnly") addTopAccentLine(slide, opts.x, opts.y, opts.w);
    else addTextPanel(slide, opts.x, opts.y, opts.w, opts.h);
  }

  if (paragraph && bullets.length === 0) {
    addParagraph(slide, paragraph, opts);
  } else {
    addBullets(slide, bullets, opts);
  }
}

function bulletLineCount(bullets) {
  if (!bullets || bullets.length === 0) return 0;
  let n = 0;
  bullets.forEach(b => { n += 1 + (b.sub || []).length; });
  return n;
}

function bulletWordCount(bullets) {
  if (!bullets || bullets.length === 0) return 0;
  let n = 0;
  bullets.forEach(b => {
    n += (b.text || "").split(/\\s+/).filter(Boolean).length;
    (b.sub || []).forEach(s => { n += String(s).split(/\\s+/).filter(Boolean).length; });
  });
  return n;
}

function fillT4TwoCols(slide, info, colY, colH) {
  const bullets = info.bullets || [];
  const mid = Math.ceil(bullets.length / 2);
  const left = bullets.slice(0, mid);
  const right = bullets.slice(mid);

  const y = (typeof colY === "number") ? colY : 5.25;
  const h = (typeof colH === "number") ? colH : 1.9;
  const leftOpts  = { x: 0.5, y: y, w: 5.9, h: h };
  const rightOpts = { x: 6.7, y: y, w: 5.9, h: h };

  if (DESIGN_ID === 0 || DESIGN_ID === 2 || DESIGN_ID === 5) addTopAccentLine(slide, leftOpts.x, leftOpts.y, leftOpts.w);
  else addTextPanel(slide, leftOpts.x, leftOpts.y, leftOpts.w, leftOpts.h);
  addBullets(slide, left, leftOpts);

  if (right.length > 0) {
    if (DESIGN_ID === 0 || DESIGN_ID === 2 || DESIGN_ID === 5) addTopAccentLine(slide, rightOpts.x, rightOpts.y, rightOpts.w);
    else addTextPanel(slide, rightOpts.x, rightOpts.y, rightOpts.w, rightOpts.h);
    addBullets(slide, right, rightOpts);
  }
}

"""


def _js_image_fit_helpers() -> str:
    """JS helpers to read image dimensions and compute aspect-ratio-preserving fit."""
    return """\
function getImageDimensions(filePath) {
  try {
    const fd = fs.openSync(filePath, "r");
    const hdr = Buffer.alloc(30);
    fs.readSync(fd, hdr, 0, 30, 0);
    fs.closeSync(fd);

    // PNG: signature 89 50 4E 47, width @ 16, height @ 20 (big-endian u32)
    if (hdr[0] === 0x89 && hdr[1] === 0x50 && hdr[2] === 0x4E && hdr[3] === 0x47) {
      return { w: hdr.readUInt32BE(16), h: hdr.readUInt32BE(20) };
    }

    // JPEG: scan for SOF0..SOF15 marker (0xC0-0xCF, skip 0xC4 DHT and 0xC8)
    if (hdr[0] === 0xFF && hdr[1] === 0xD8) {
      const buf = fs.readFileSync(filePath);
      let i = 2;
      while (i < buf.length - 9) {
        if (buf[i] !== 0xFF) { i++; continue; }
        const m = buf[i + 1];
        if (m >= 0xC0 && m <= 0xCF && m !== 0xC4 && m !== 0xC8) {
          return { w: buf.readUInt16BE(i + 7), h: buf.readUInt16BE(i + 5) };
        }
        i += 2 + buf.readUInt16BE(i + 2);
      }
    }
  } catch (_) { /* fall through */ }
  return null;
}

function fitImage(imgPath, boxX, boxY, boxW, boxH) {
  const dims = getImageDimensions(imgPath);
  if (!dims || dims.w === 0 || dims.h === 0) {
    return { x: boxX, y: boxY, w: boxW, h: boxH };
  }
  const imgRatio = dims.w / dims.h;
  const boxRatio = boxW / boxH;
  let fitW, fitH;
  if (imgRatio > boxRatio) {
    fitW = boxW;
    fitH = boxW / imgRatio;
  } else {
    fitH = boxH;
    fitW = boxH * imgRatio;
  }
  return {
    x: boxX + (boxW - fitW) / 2,
    y: boxY + (boxH - fitH) / 2,
    w: fitW,
    h: fitH,
  };
}

"""


def _js_add_images() -> str:
    """Place resolved images according to template_id, preserving aspect ratio."""
    return """\
function imageBoxes(templateId, visualsLen) {
  // Must stay in sync with addLayoutBg() image-frame intent.
  if (!visualsLen || visualsLen <= 0) return [];
  if (templateId === "T1_TextOnly" || templateId === "T14_2Text") return [];

  if (templateId === "T2_ImageRight") return [{ x: 7.5, y: 1.44, w: 5.0, h: 4.66 }];
  if (templateId === "T3_ImageLeft")  return [{ x: 0.5, y: 1.44, w: 5.0, h: 4.66 }];
  // T4 image band: 47.5% of slide height (7.5" → 3.5625")
  if (templateId === "T4_ImageTop")   return [{ x: 0.5, y: 1.14, w: 12.0, h: 3.5625 }];

  if (templateId === "T5_TwoImages2") {
    const out = [{ x: 0.5, y: 1.14, w: 5.8, h: 3.16 }];
    if (visualsLen > 1) out.push({ x: 6.8, y: 1.14, w: 5.8, h: 3.16 });
    return out;
  }

  if (templateId === "T11_ImageRight2") {
    const out = [{ x: 7.5, y: 1.14, w: 5.0, h: 2.58 }];
    if (visualsLen > 1) out.push({ x: 7.5, y: 4.04, w: 5.0, h: 2.58 });
    return out;
  }

  if (templateId === "T12_ImageLeft2") {
    const out = [{ x: 0.5, y: 1.14, w: 5.0, h: 2.58 }];
    if (visualsLen > 1) out.push({ x: 0.5, y: 4.04, w: 5.0, h: 2.58 });
    return out;
  }

  if (["T6_2x2_TopImage", "T7_2x2_BottomImage", "T8_2x2_AltTextImg", "T9_4Img_2x2Grid"].includes(templateId)) {
    const slots = [
      { x: 0.5, y: 1.14 }, { x: 6.8, y: 1.14 },
      { x: 0.5, y: 4.04 }, { x: 6.8, y: 4.04 },
    ];
    const iw = 5.5, ih = 2.58;
    const out = [];
    for (let i = 0; i < Math.min(visualsLen, 4); i++) out.push({ x: slots[i].x, y: slots[i].y, w: iw, h: ih });
    return out;
  }

  if (templateId === "T10_3Img_BottomTextTop") {
    const iw = 3.8, ih = 3.16;
    const out = [];
    for (let i = 0; i < Math.min(3, visualsLen); i++) out.push({ x: 0.5 + i * (iw + 0.35), y: 3.64, w: iw, h: ih });
    return out;
  }

  return [{ x: 7.0, y: 1.64, w: 5.0, h: 4.16 }];
}

function _intersect(a, b) {
  const x1 = Math.max(a.x, b.x);
  const y1 = Math.max(a.y, b.y);
  const x2 = Math.min(a.x + a.w, b.x + b.w);
  const y2 = Math.min(a.y + a.h, b.y + b.h);
  const w = x2 - x1;
  const h = y2 - y1;
  return (w > 0 && h > 0) ? { x: x1, y: y1, w, h } : null;
}

function _clipRectToAvoid(r, avoid, margin) {
  if (!avoid) return r;
  const inter = _intersect(r, avoid);
  if (!inter) return r;

  const avoidL = avoid.x;
  const avoidR = avoid.x + avoid.w;
  const avoidT = avoid.y;
  const avoidB = avoid.y + avoid.h;
  const rR = r.x + r.w;
  const rB = r.y + r.h;

  // Choose axis by overlap span.
  if (inter.w >= inter.h) {
    if (r.x >= avoidL) {
      const newX = avoidR + margin;
      r.x = Math.min(newX, rR - 0.2);
      r.w = Math.max(0.2, rR - r.x);
    } else {
      r.w = Math.max(0.2, (avoidL - margin) - r.x);
    }
  } else {
    if (r.y >= avoidT) {
      const newY = avoidB + margin;
      r.y = Math.min(newY, rB - 0.2);
      r.h = Math.max(0.2, rB - r.y);
    } else {
      r.h = Math.max(0.2, (avoidT - margin) - r.y);
    }
  }
  return r;
}

function fittedImageRects(templateId, visuals) {
  if (!visuals || visuals.length === 0) return [];
  const boxes = imageBoxes(templateId, visuals.length);
  const out = [];
  for (let i = 0; i < Math.min(visuals.length, boxes.length); i++) {
    const p = visuals[i];
    const b = boxes[i];
    if (p && fs.existsSync(p)) out.push(fitImage(p, b.x, b.y, b.w, b.h));
    else out.push({ x: b.x, y: b.y, w: b.w, h: b.h });
  }
  return out;
}

function addImageFrames(slide, visuals, templateId, avoidRect, imageRects) {
  // Draw the *blue background design boxes* around the actual fitted image rects,
  // and strictly ensure they do not overlap the text panel. If imageRects is
  // provided, it is used verbatim (lets per-slide image_xywh overrides drag
  // the frame along with the image); otherwise the default fittedImageRects
  // for this template are used.
  if (!visuals || visuals.length === 0) return;
  if (templateId === "T1_TextOnly" || templateId === "T14_2Text") return;

  const rects = (Array.isArray(imageRects) && imageRects.length > 0)
    ? imageRects
    : fittedImageRects(templateId, visuals);
  if (!rects || rects.length === 0) return;

  const avoid = avoidRect ? {
    x: avoidRect.x - 0.18,
    y: avoidRect.y - 0.16,
    w: avoidRect.w + 0.36,
    h: avoidRect.h + 0.32,
  } : null;

  const pad = 0.18;
  const margin = 0.06;

  rects.forEach((fr) => {
    let r = { x: fr.x - pad, y: fr.y - pad, w: fr.w + 2 * pad, h: fr.h + 2 * pad };
    if (avoid) {
      for (let k = 0; k < 8; k++) {
        if (!_intersect(r, avoid)) break;
        r = _clipRectToAvoid(r, avoid, margin);
      }
    }
    if (r.w >= 0.35 && r.h >= 0.35) addFrameRect(slide, r.x, r.y, r.w, r.h);
  });
}

function addImages(slide, visuals, templateId) {
  if (!visuals || visuals.length === 0) return;

  function img(p, bx, by, bw, bh) {
    if (!fs.existsSync(p)) { console.warn("  [warn] image not found:", p); return; }
    const f = fitImage(p, bx, by, bw, bh);
    slide.addImage({ path: p, x: f.x, y: f.y, w: f.w, h: f.h });
  }

  const boxes = imageBoxes(templateId, visuals.length);
  for (let i = 0; i < Math.min(visuals.length, boxes.length); i++) {
    const b = boxes[i];
    img(visuals[i], b.x, b.y, b.w, b.h);
  }
}
"""


def _js_add_reference() -> str:
    """Small reference line at bottom-right with accent bracket."""
    return """\
function addReference(slide, ref) {
  if (!ref) return;
  const short = ref.length > 120 ? ref.substring(0, 117) + "..." : ref;
  let refY, refH;
  let refColor = "FFFFFF";
  if (DESIGN_ID === 2) {
    refY = 7.22; refH = 0.26;
  } else if (DESIGN_ID === 3) {
    refY = 7.16; refH = 0.30;
  } else if (DESIGN_ID === 4) {
    // Design 4 has a light reference strip; use dark text for contrast.
    refY = 7.22; refH = 0.26;
    refColor = "000000";
  } else if (DESIGN_ID === 1) {
    refY = 7.22; refH = 0.26;
    refColor = C.textMid;
  } else if (DESIGN_ID === 5) {
    // Design 5 uses a light frosted footer; black improves readability.
    refY = 7.22; refH = 0.26;
    refColor = "000000";
  } else {
    refY = 7.22; refH = 0.26;
  }
  slide.addText(short, {
    x: 4.0, y: refY, w: 8.5, h: refH,
    fontSize: 9, color: refColor, align: "right", italic: true,
    fontFace: "Calibri",
  });
}
"""


def _js_layout_body_opts() -> str:
    """Return body-content text-box coordinates per template_id."""
    return """\
function bodyOpts(templateId) {
  switch (templateId) {
    case "T1_TextOnly":
      return { x: 0.6, y: 1.24, w: 12.0, h: 5.36 };
    case "T2_ImageRight":
      return { x: 0.6, y: 1.24, w: 6.5, h: 5.36 };
    case "T3_ImageLeft":
      return { x: 6.0, y: 1.24, w: 6.5, h: 5.36 };
    case "T4_ImageTop":
      return { x: 0.5, y: 5.25, w: 12.0, h: 1.9 };
    case "T5_TwoImages2":
      return { x: 0.6, y: 4.34, w: 12.0, h: 2.66 };
    case "T6_2x2_TopImage":
    case "T7_2x2_BottomImage":
    case "T8_2x2_AltTextImg":
    case "T9_4Img_2x2Grid":
      return null;
    case "T10_3Img_BottomTextTop":
      return { x: 0.6, y: 1.24, w: 12.0, h: 2.0 };
    case "T11_ImageRight2":
      return { x: 0.6, y: 1.24, w: 6.5, h: 5.36 };
    case "T12_ImageLeft2":
      return { x: 6.0, y: 1.24, w: 6.5, h: 5.36 };
    case "T14_2Text":
      return null;
    default:
      return { x: 0.6, y: 1.24, w: 6.5, h: 5.16 };
  }
}
"""


def _js_fill_T14() -> str:
    """Handle the two-column text layout (T14_2Text) with panels."""
    return """\
function fillT14(slide, info) {
  const cols = info.columns || [];
  const left = cols[0] || {};
  const right = cols[1] || {};

  // Left column sub-heading
  if (left.subsection) {
    slide.addShape("rect", {
      x: 0.4, y: 1.09, w: 0.06, h: 0.5,
      fill: { color: C.accent },
    });
    slide.addText(left.subsection, {
      x: 0.6, y: 1.09, w: 5.6, h: 0.55,
      fontSize: BULLET_FONT_SIZE, bold: true, color: C.primary, fontFace: "Calibri",
    });
  }
  addBodyContent(slide, left, { x: 0.5, y: 1.84, w: 5.7, h: 4.66 });

  // Vertical divider
  slide.addShape("rect", {
    x: 6.55, y: 1.24, w: 0.02, h: 5.16,
    fill: { color: C.panelBorder },
  });

  // Right column sub-heading
  if (right.subsection) {
    slide.addShape("rect", {
      x: 6.8, y: 1.09, w: 0.06, h: 0.5,
      fill: { color: C.accent },
    });
    slide.addText(right.subsection, {
      x: 7.0, y: 1.09, w: 5.6, h: 0.55,
      fontSize: BULLET_FONT_SIZE, bold: true, color: C.primary, fontFace: "Calibri",
    });
  }
  addBodyContent(slide, right, { x: 6.9, y: 1.84, w: 5.7, h: 4.66 });
}
"""


def _js_build_main(out_filename: str, use_animation: bool, dynamically_adjust_layout: bool) -> str:
    """The main async function that builds the presentation."""
    out_name_escaped = out_filename.replace("\\", "\\\\").replace('"', '\\"')
    use_animation_js = "true" if use_animation else "false"
    dyn_adj_js = "true" if dynamically_adjust_layout else "false"
    return f"""\
async function buildPresentation() {{
  const pptx = new PptxGenJS();
  configurePres(pptx);

  const USE_ANIMATION = {use_animation_js};
  const DYNAMICALLY_ADJUST_LAYOUT = {dyn_adj_js};

  function _noOverlapBodyOpts(templateId, infoObj, visuals) {{
    const raw = bodyOpts(templateId);
    if (!raw || !visuals || visuals.length === 0) return raw;

    // Strict: body rect must not intersect fitted images at all.
    const margin = 0.08;
    const minW = 2.0;
    const minH = 1.4;
    const original = {{ x: raw.x, y: raw.y, w: raw.w, h: raw.h }};
    let cur = {{ x: raw.x, y: raw.y, w: raw.w, h: raw.h }};

    const rects = fittedImageRects(templateId, visuals);
    if (!rects || rects.length === 0) return raw;

    function clampToOriginal() {{
      const right = original.x + original.w;
      const bottom = original.y + original.h;
      cur.x = Math.max(original.x, cur.x);
      cur.y = Math.max(original.y, cur.y);
      cur.w = Math.min(right - cur.x, cur.w);
      cur.h = Math.min(bottom - cur.y, cur.h);
    }}

    function intersectsAny() {{
      for (let i = 0; i < rects.length; i++) if (_intersect(cur, rects[i])) return rects[i];
      return null;
    }}

    for (let iter = 0; iter < 10; iter++) {{
      const b = intersectsAny();
      if (!b) break;
      const inter = _intersect(cur, b);
      if (!inter) break;

      // Resolve along axis with larger overlap span.
      if (inter.w >= inter.h) {{
        // Horizontal
        const bMid = b.x + b.w / 2;
        const curMid = cur.x + cur.w / 2;
        if (bMid >= curMid) {{
          cur.w = Math.min(cur.w, (b.x - margin) - cur.x);
        }} else {{
          const right = cur.x + cur.w;
          cur.x = Math.min(b.x + b.w + margin, right - minW);
          cur.w = right - cur.x;
        }}
      }} else {{
        // Vertical
        const bMidY = b.y + b.h / 2;
        const curMidY = cur.y + cur.h / 2;
        if (bMidY >= curMidY) {{
          cur.h = Math.min(cur.h, (b.y - margin) - cur.y);
        }} else {{
          const bottom = cur.y + cur.h;
          cur.y = Math.min(b.y + b.h + margin, bottom - minH);
          cur.h = bottom - cur.y;
        }}
      }}
      clampToOriginal();
    }}

    if (cur.w < minW || cur.h < minH) return original;
    return cur;
  }}

  function t4DynamicBodyRect(visuals) {{
    // Body under the image: clearedY = fitted bottom + panel bleed + gap.
    // When DYNAMICALLY_ADJUST_LAYOUT: newY = min(base.y, clearedY) → text moves UP if the image is
    // short (extra space below image). If clearedY > base.y, clamp forces text DOWN to avoid overlap.
    const base = {{ x: 0.5, y: 5.25, w: 12.0, h: 1.9 }};
    const bottom = base.y + base.h;
    const panelTopBleed = 0.22;
    const gap = 0.03;
    if (!visuals || visuals.length === 0) return base;
    const rects = fittedImageRects("T4_ImageTop", visuals);
    if (!rects || rects.length === 0) return base;
    let imgBottom = 0;
    rects.forEach(r => {{ imgBottom = Math.max(imgBottom, r.y + r.h); }});
    const clearedY = imgBottom + panelTopBleed + gap;
    let newY;
    if (DYNAMICALLY_ADJUST_LAYOUT) {{
      newY = Math.min(base.y, Math.max(clearedY, 1.20));
      if (newY < clearedY) newY = clearedY;
    }} else {{
      newY = Math.max(base.y, clearedY);
    }}
    const newH = Math.max(0.5, bottom - newY);
    return {{ x: base.x, y: newY, w: base.w, h: newH }};
  }}

  function t5DynamicBodyRect(visuals) {{
    // Same spacing as t4DynamicBodyRect: fitted image bottom + panelTopBleed + 0.03 gap.
    const base = {{ x: 0.6, y: 4.34, w: 12.0, h: 2.66 }};
    const bottom = base.y + base.h;
    const panelTopBleed = 0.22;
    const gap = 0.03;
    if (!visuals || visuals.length === 0) return base;
    const rects = fittedImageRects("T5_TwoImages2", visuals);
    if (!rects || rects.length === 0) return base;
    let imgBottom = 0;
    rects.forEach(r => {{ imgBottom = Math.max(imgBottom, r.y + r.h); }});
    const clearedY = imgBottom + panelTopBleed + gap;
    let newY;
    if (DYNAMICALLY_ADJUST_LAYOUT) {{
      newY = Math.min(base.y, Math.max(clearedY, 1.20));
      if (newY < clearedY) newY = clearedY;
    }} else {{
      newY = Math.max(base.y, clearedY);
    }}
    const newH = Math.max(0.5, bottom - newY);
    return {{ x: base.x, y: newY, w: base.w, h: newH }};
  }}

  const meta = SLIDE_PLAN.metadata || {{}};
  addCoverSlide(pptx, meta);

  const slides = SLIDE_PLAN.slides || [];

  slides.forEach((info, idx) => {{
    const templateId = info.template_id || "T1_TextOnly";
    // Per-slide overrides keyed by 1-based slide number.
    const _ovr = (SLIDE_OVERRIDES && SLIDE_OVERRIDES[String(idx + 1)]) || {{}};
    const visuals = _ovr.hide_figure ? [] : (info._resolved_visuals || []);
    // Apply per-slide font overrides (mutating module-level let-bindings,
    // restored at end of this iteration).
    const _savedBulletFS = BULLET_FONT_SIZE;
    const _savedTitleFS = TITLE_FONT_SIZE;
    const _savedSubBulletFS = SUB_BULLET_FONT_SIZE;
    if (typeof _ovr.bullet_font_size === "number") {{
      BULLET_FONT_SIZE = _ovr.bullet_font_size;
      SUB_BULLET_FONT_SIZE = _ovr.bullet_font_size - 2;
    }}
    if (typeof _ovr.title_font_size === "number") {{
      TITLE_FONT_SIZE = _ovr.title_font_size;
    }}

    function hasBodyContent(infoObj) {{
      if (!infoObj) return false;
      if (infoObj.template_id === "T14_2Text") {{
        const cols = infoObj.columns || [];
        return cols.some(c => c && (((c.paragraph || "").trim().length > 0) || ((c.bullets || []).length > 0)));
      }}
      const paragraph = (infoObj.paragraph || "").trim();
      const bullets = infoObj.bullets || [];
      return (paragraph.length > 0 && bullets.length === 0) || bullets.length > 0;
    }}

    const doAnimate = USE_ANIMATION && visuals.length > 0 && hasBodyContent(info);

    function renderSlide(infoObj, slideNum, includeBody, includeReference) {{
      const slide = pptx.addSlide();

      // Per-layout background decoration
      addLayoutBg(slide, templateId);

      // Title bar with slide number badge
      addTitle(slide, infoObj.subsection || infoObj.section || "Slide " + slideNum, slideNum);

      function _effectiveImageRects(visuals, templateId) {{
        // Compute the final per-visual rects after applying _ovr.image_xywh.
        // Each entry is the actual rect the image will be drawn into (post
        // fitImage). Used to keep addImageFrames in sync with overrides.
        if (!visuals || visuals.length === 0) return [];
        const base = fittedImageRects(templateId, visuals);
        const ov = _ovr.image_xywh;
        const out = [];
        for (let i = 0; i < visuals.length; i++) {{
          const r = Array.isArray(ov) ? ov[i] : null;
          if (Array.isArray(r) && r.length === 4) {{
            const p = visuals[i];
            if (p && fs.existsSync(p)) {{
              out.push(fitImage(p, r[0], r[1], r[2], r[3]));
            }} else {{
              out.push({{ x: r[0], y: r[1], w: r[2], h: r[3] }});
            }}
          }} else {{
            out.push(base[i] || null);
          }}
        }}
        return out.filter(r => r);
      }}
      function _drawImagesWithOverride(slide, visuals, templateId) {{
        const ov = _ovr.image_xywh;
        if (!Array.isArray(ov)) {{ addImages(slide, visuals, templateId); return; }}
        for (let i = 0; i < visuals.length; i++) {{
          const p = visuals[i];
          if (!p || !fs.existsSync(p)) continue;
          const r = ov[i];
          if (Array.isArray(r) && r.length === 4) {{
            const f = fitImage(p, r[0], r[1], r[2], r[3]);
            slide.addImage({{ path: p, x: f.x, y: f.y, w: f.w, h: f.h }});
          }} else {{
            const boxes = imageBoxes(templateId, visuals.length);
            const b = boxes[i];
            if (b) {{
              const f = fitImage(p, b.x, b.y, b.w, b.h);
              slide.addImage({{ path: p, x: f.x, y: f.y, w: f.w, h: f.h }});
            }}
          }}
        }}
      }}

      if (!includeBody) {{
        // Reveal slide: frames + images only
        if (visuals.length > 0) {{
          addImageFrames(slide, visuals, templateId, null, _effectiveImageRects(visuals, templateId));
          _drawImagesWithOverride(slide, visuals, templateId);
        }}
      }}

      if (includeBody) {{
        // Body content depends on template
        if (templateId === "T14_2Text") {{
          fillT14(slide, infoObj);
        }} else if (templateId === "T4_ImageTop"
            && (bulletWordCount(infoObj.bullets) > 60 || bulletLineCount(infoObj.bullets) > 4)) {{
          // Two-column text for dense T4 slides
          const t4 = t4DynamicBodyRect(visuals);
          fillT4TwoCols(slide, infoObj, t4.y, t4.h);

          // Ensure T4 always has image frames/images too.
          if (visuals.length > 0) {{
            const t4Avoid = {{ x: 0.5, y: t4.y, w: 12.0, h: t4.h }};
            addImageFrames(slide, visuals, templateId, t4Avoid, _effectiveImageRects(visuals, templateId));
            _drawImagesWithOverride(slide, visuals, templateId);
          }}
        }} else {{
          let safeOpts = _noOverlapBodyOpts(templateId, infoObj, visuals);
          if (templateId === "T4_ImageTop") {{
            safeOpts = t4DynamicBodyRect(visuals);
          }} else if (templateId === "T5_TwoImages2") {{
            safeOpts = t5DynamicBodyRect(visuals);
          }}
          // Per-slide body_xywh override fully replaces the computed body rect.
          if (Array.isArray(_ovr.body_xywh) && _ovr.body_xywh.length === 4) {{
            safeOpts = {{
              x: _ovr.body_xywh[0], y: _ovr.body_xywh[1],
              w: _ovr.body_xywh[2], h: _ovr.body_xywh[3],
            }};
          }}
          if (safeOpts && (DESIGN_ID === 0 || DESIGN_ID === 2 || DESIGN_ID === 5)
              && (templateId === "T4_ImageTop" || templateId === "T5_TwoImages2")) {{
            safeOpts = {{ ...safeOpts, panelStyle: "topLineOnly" }};
          }}
          if (visuals.length > 0) {{
            // Draw the image background design boxes sized to the fitted images,
            // clipped so they never touch the text panel.
            addImageFrames(slide, visuals, templateId, safeOpts, _effectiveImageRects(visuals, templateId));
            _drawImagesWithOverride(slide, visuals, templateId);
          }}
          if (safeOpts) addBodyContent(slide, infoObj, safeOpts);
        }}
        // (Images already added above for non-T14/T4TwoCols path.)
      }}

      if (includeReference) addReference(slide, infoObj.reference || "");
    }}

    if (doAnimate) {{
      // "Animation" effect: split one logical slide into two sequential slides.
      // 1) title + images, then 2) full content (title + images + text).
      const slideNum = idx + 1;
      renderSlide(info, slideNum, false, false);
      renderSlide(info, slideNum, true, true);
    }} else {{
      renderSlide(info, idx + 1, true, true);
    }}
    // Restore module-level font sizes after this slide.
    BULLET_FONT_SIZE = _savedBulletFS;
    TITLE_FONT_SIZE = _savedTitleFS;
    SUB_BULLET_FONT_SIZE = _savedSubBulletFS;
  }});

  addThanksSlide(pptx);

  const outPath = "{out_name_escaped}";
  console.log("Writing presentation:", outPath);
  await pptx.writeFile({{ fileName: outPath }});
}}
"""


def _js_entry() -> str:
    return """\
buildPresentation().then(
  () => console.log("Done – PptxGenJS presentation created."),
  (err) => { console.error("Error:", err); process.exit(1); }
);
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_pptx_from_plan_using_pptxgenjs(
    args: Any,
    bullet_font_size: int = 20,
    title_font_size: int = 24,
    theme_id: int = 0,
    design_id: int = 0,
    use_animation: bool = False,
    dynamically_adjust_layout: bool = False,
) -> None:
    """
    Read the slide_plan.json, resolve all image/table paths, then write a
    self-contained Node.js script that uses PptxGenJS to produce the PPTX.

    Usage after running the Python pipeline:
        npm install pptxgenjs          # one-time
        node <generated_js_path>       # creates the .pptx
    """

    plan_path = Path(
        f"contents/{args.paper_name}/"
        f"<{args.model_name_t}_{args.model_name_v}>_slide_plan.json"
    )
    if not plan_path.exists():
        raise FileNotFoundError(f"Slide plan not found: {plan_path}")

    with plan_path.open("r", encoding="utf-8") as f:
        plan: Dict[str, Any] = json.load(f)

    # Resolve visual paths in Python
    resolved_plan = _resolve_all_visuals(plan, args)

    # Repair LaTeX commands (\textcolor / \textbf / ...) whose leading
    # backslash was eaten by JSON escape handling upstream (e.g. \t → tab).
    from slide_generation.edit_ops import repair_slide_plan_latex
    repair_slide_plan_latex(resolved_plan)

    if theme_id not in THEMES:
        print(f"[pptxgenjs][warn] Unknown theme {theme_id}, falling back to 0.")
        theme_id = 0
    if design_id not in (0, 1, 2, 3, 4, 5):
        print(f"[pptxgenjs][warn] Unknown design {design_id}, falling back to 0.")
        design_id = 0

    # Output file names (include theme and design index)
    out_pptx = (
        f"contents/{args.paper_name}/"
        f"{args.model_name_t}_{args.model_name_v}_output_slides_pptxgenjs_theme{theme_id}_design{design_id}.pptx"
    )
    out_js = Path(
        f"contents/{args.paper_name}/"
        f"{args.model_name_t}_{args.model_name_v}_pptxgenjs_theme{theme_id}_design{design_id}.js"
    )

    # Embed the resolved slide plan as JSON
    plan_json_str = json.dumps(resolved_plan, indent=2, ensure_ascii=False)

    # Per-slide overrides keyed by 1-based slide number. Populated by the
    # JS feedback reviser (see edit_ops_js.set_slide_override). Empty by
    # default so a fresh render is a no-op. Supported keys per slide:
    #   bullet_font_size, title_font_size, body_xywh: [x,y,w,h],
    #   image_xywh: [[x,y,w,h], ...], hide_figure: bool
    slide_overrides_str = "{}"

    # Assemble the JS from modular pieces
    parts = [
        f"// Auto-generated by generate_pptx_from_plan_using_pptxgenjs",
        f"// Run:  node {out_js.name}",
        "",
        _js_header(),
        f"const SLIDE_PLAN = {plan_json_str};\n",
        "// --- Per-slide overrides (populated by feedback_js reviser) ---",
        f"const SLIDE_OVERRIDES = {slide_overrides_str};\n",
        "// --- Font size configuration ---",
        _js_font_config(bullet_font_size, title_font_size),
        "// --- Presentation configuration ---",
        _js_configure_presentation(theme_id, design_id),
        "// --- LaTeX formatting parser ---",
        _js_parse_latex(),
        "// --- Cover slide ---",
        _js_add_cover_slide(),
        "// --- Title bar ---",
        _js_add_title(),
        "// --- Bullet points ---",
        _js_add_bullets(),
        "// --- Paragraph text ---",
        _js_add_paragraph(),
        "// --- Body content dispatcher ---",
        _js_add_body_content(),
        "// --- Body position per layout ---",
        _js_layout_body_opts(),
        "// --- Image dimension reading & aspect-ratio fitting ---",
        _js_image_fit_helpers(),
        "// --- Image placement per layout ---",
        _js_add_images(),
        "// --- T14 two-column text ---",
        _js_fill_T14(),
        "// --- Reference footnote ---",
        _js_add_reference(),
        "// --- Thanks slide ---",
        _js_add_thanks_slide(),
        "// --- Main build ---",
        _js_build_main(
            out_pptx,
            use_animation=use_animation,
            dynamically_adjust_layout=dynamically_adjust_layout,
        ),
        "// --- Entry point ---",
        _js_entry(),
    ]

    js_code = "\n".join(parts)

    out_js.parent.mkdir(parents=True, exist_ok=True)
    out_js.write_text(js_code, encoding="utf-8")

    print(
        f"[pptxgenjs] Wrote Node script: {out_js}\n"
        f"[pptxgenjs] Output PPTX will be: {out_pptx}\n"
        f"[pptxgenjs] Run with:  node {out_js}"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate PPTX from slide plan using PptxGenJS."
    )
    parser.add_argument("--paper_name", type=str, default="Video-of-Thought_-_Step-by-Step_Video_Reasoning_from_Perception_to_Cognition_ICML_2024", help="Name of the paper.")
    parser.add_argument("--model_name_t", type=str, default="gpt-5", help="Name of the model.")
    parser.add_argument("--model_name_v", type=str, default="gpt-5", help="Name of the model.")
    parser.add_argument("--formula_mode", type=int, default=1)
    parser.add_argument("--bullet_font_size", type=int, default=16)
    parser.add_argument("--title_font_size", type=int, default=24)
    parser.add_argument(
        "--theme", type=int, default=0,
        help=f"Theme index (0-{max(THEMES.keys())}). Available: "
             + ", ".join(f"{k}" for k in sorted(THEMES.keys())),
    )
    parser.add_argument(
        "--design", type=int, default=0,
        help="Design index (0-5). 0=Circles & Bokeh, 1=Geometric Angular, 2=Sunset Gradient, 3=Aurora Neon, 4=Teal Screenshot, 5=Glassmorphism",
    )
    args = parser.parse_args()
    for design_id in [5]:
      generate_pptx_from_plan_using_pptxgenjs(
          args,
          bullet_font_size=args.bullet_font_size,
          title_font_size=args.title_font_size,
          theme_id=0,
          design_id=design_id,
          use_animation=False,
          dynamically_adjust_layout=True,
      )
      print(f"Generated PPTX for design {design_id}")
      os.system(f"node contents/{args.paper_name}/"
              f"{args.model_name_t}_{args.model_name_v}_pptxgenjs_theme{0}_design{design_id}.js")
      print(f"PPTX file saved at contents/{args.paper_name}/"
            f"{args.model_name_t}_{args.model_name_v}_output_slides_pptxgenjs_theme{0}_design{design_id}.pptx")
      print("-"*100)
