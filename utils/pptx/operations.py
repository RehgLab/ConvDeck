"""
Real PPTX utility functions used at runtime (not string templates).

These are the actual callable functions for creating, styling, and
manipulating python-pptx Presentation objects.
"""

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.text import PP_ALIGN, MSO_AUTO_SIZE
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR, MSO_SHAPE_TYPE, MSO_AUTO_SHAPE_TYPE
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.oxml.xmlchemy import OxmlElement
from pptx.oxml.ns import qn
import pptx
import json


# ---------------------------------------------------------------------------
# Unit conversion helpers
# ---------------------------------------------------------------------------

def emu_to_inches(emu: int) -> float:
    return emu / 914400

def _px_to_pt(px):
    return px * 0.75

def _parse_font_size(font_size):
    if font_size is None:
        return None
    if isinstance(font_size, (int, float)):
        return Pt(font_size)
    return font_size

def _parse_alignment(alignment):
    if not isinstance(alignment, str):
        return PP_ALIGN.LEFT
    alignment = alignment.lower().strip()
    alignment_map = {
        "left": PP_ALIGN.LEFT,
        "center": PP_ALIGN.CENTER,
        "right": PP_ALIGN.RIGHT,
        "justify": PP_ALIGN.JUSTIFY,
    }
    return alignment_map.get(alignment, PP_ALIGN.LEFT)


# ---------------------------------------------------------------------------
# Presentation & slide creation
# ---------------------------------------------------------------------------

def create_poster(width_inch=48, height_inch=36):
    prs = Presentation()
    prs.slide_width = Inches(width_inch)
    prs.slide_height = Inches(height_inch)
    return prs

def add_blank_slide(prs):
    blank_layout = prs.slide_layouts[6]
    return prs.slides.add_slide(blank_layout)

def save_presentation(prs, file_name="poster.pptx"):
    prs.save(file_name)

def set_slide_background_color(slide, rgb=(255, 255, 255)):
    bg_fill = slide.background.fill
    bg_fill.solid()
    bg_fill.fore_color.rgb = RGBColor(*rgb)


# ---------------------------------------------------------------------------
# Shape operations
# ---------------------------------------------------------------------------

def shape_fill_color(shape, fill_color):
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(*fill_color)

def set_shape_position(shape, left_inch, top_inch, width_inch, height_inch):
    shape.left = Inches(left_inch)
    shape.top = Inches(top_inch)
    shape.width = Inches(width_inch)
    shape.height = Inches(height_inch)

def center_shape_horizontally(prs, shape):
    new_left = (prs.slide_width - shape.width) // 2
    shape.left = new_left

def center_shape_vertically(prs, shape):
    new_top = (prs.slide_height - shape.height) // 2
    shape.top = new_top

def add_image(slide, name, left_inch, top_inch, width_inch, height_inch, image_path):
    shape = slide.shapes.add_picture(
        image_path,
        Inches(left_inch), Inches(top_inch),
        width=Inches(width_inch), height=Inches(height_inch)
    )
    shape.name = name
    return shape

def add_line_simple(slide, name, left_inch, top_inch, length_inch, thickness=2, color=(0, 0, 0), orientation="horizontal"):
    x1 = Inches(left_inch)
    y1 = Inches(top_inch)
    if orientation.lower() == "horizontal":
        x2 = Inches(left_inch + length_inch)
        y2 = y1
    elif orientation.lower() == "vertical":
        x2 = x1
        y2 = Inches(top_inch + length_inch)
    else:
        raise ValueError("Orientation must be either 'horizontal' or 'vertical'")
    line_shape = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x1, y1, x2, y2)
    line_shape.name = name
    line_shape.line.width = Pt(thickness)
    line_shape.line.color.rgb = RGBColor(*color)
    return line_shape

def style_shape_border(shape, color=(30, 144, 255), thickness=2, line_style="square_dot"):
    dash_style_map = {
        "solid": MSO_LINE_DASH_STYLE.SOLID,
        "round_dot": MSO_LINE_DASH_STYLE.ROUND_DOT,
        "square_dot": MSO_LINE_DASH_STYLE.SQUARE_DOT,
        "dash": MSO_LINE_DASH_STYLE.DASH,
        "dash_dot": MSO_LINE_DASH_STYLE.DASH_DOT,
        "dash_dot_dot": MSO_LINE_DASH_STYLE.DASH_DOT_DOT,
        "long_dash": MSO_LINE_DASH_STYLE.LONG_DASH,
        "long_dash_dot": MSO_LINE_DASH_STYLE.LONG_DASH_DOT,
    }
    line = shape.line
    line.width = Pt(thickness)
    line.color.rgb = RGBColor(*color)
    dash_style_enum = dash_style_map.get(line_style.lower(), MSO_LINE_DASH_STYLE.SOLID)
    line.dash_style = dash_style_enum


# ---------------------------------------------------------------------------
# Text operations
# ---------------------------------------------------------------------------

def _set_run_font_color(run, rgb_tuple):
    rPr = run.font._element
    for child in rPr.iterchildren():
        if child.tag == qn('a:solidFill'):
            rPr.remove(child)
    solid_fill = OxmlElement('a:solidFill')
    srgb_clr = OxmlElement('a:srgbClr')
    srgb_clr.set('val', '{:02X}{:02X}{:02X}'.format(*rgb_tuple))
    solid_fill.append(srgb_clr)
    rPr.append(solid_fill)

def add_textbox(slide, name, left_inch, top_inch, width_inch, height_inch,
                text="", word_wrap=True, font_size=40, bold=False, italic=False,
                alignment="left", fill_color=None, font_name="Arial"):
    shape = slide.shapes.add_textbox(
        Inches(left_inch), Inches(top_inch),
        Inches(width_inch), Inches(height_inch)
    )
    shape.name = name
    if fill_color is not None:
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(*fill_color)
    else:
        shape.fill.background()
    text_frame = shape.text_frame
    text_frame.auto_size = MSO_AUTO_SIZE.NONE
    text_frame.word_wrap = word_wrap
    text_frame.clear()
    p = text_frame.add_paragraph()
    run = p.add_run()
    run.text = text
    p.alignment = _parse_alignment(alignment)
    font = run.font
    font.size = _parse_font_size(font_size)
    font.bold = bold
    font.italic = italic
    font.name = font_name
    return shape

def edit_textbox(shape, text=None, word_wrap=None, font_size=None, bold=None,
                 italic=None, alignment=None, fill_color=None, font_name=None):
    text_frame = shape.text_frame
    text_frame.auto_size = MSO_AUTO_SIZE.NONE
    if fill_color is not None:
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(*fill_color)
    if word_wrap is not None:
        text_frame.word_wrap = word_wrap
    if text is not None:
        text_frame.clear()
        p = text_frame.add_paragraph()
        run = p.add_run()
        run.text = text
        if alignment is not None:
            p.alignment = _parse_alignment(alignment)
        font = run.font
        if font_size is not None:
            font.size = _parse_font_size(font_size)
        if bold is not None:
            font.bold = bold
        if italic is not None:
            font.italic = italic
    else:
        for p in text_frame.paragraphs:
            if alignment is not None:
                p.alignment = _parse_alignment(alignment)
            for run in p.runs:
                font = run.font
                if font_size is not None:
                    font.size = _parse_font_size(font_size)
                if bold is not None:
                    font.bold = bold
                if italic is not None:
                    font.italic = italic
                if font_name is not None:
                    font.name = font_name

def set_shape_text(shape, text, clear_first=True):
    text_frame = shape.text_frame
    text_frame.auto_size = MSO_AUTO_SIZE.NONE
    if clear_first:
        text_frame.clear()
    p = text_frame.add_paragraph()
    p.text = text

def set_text_style(shape, font_size=None, bold=None, italic=None, alignment=None, color=None, font_name=None):
    text_frame = shape.text_frame
    text_frame.auto_size = MSO_AUTO_SIZE.NONE
    parsed_alignment = _parse_alignment(alignment) if alignment else None
    parsed_font_size = _parse_font_size(font_size)
    for paragraph in text_frame.paragraphs:
        if parsed_alignment is not None:
            paragraph.alignment = parsed_alignment
        for run in paragraph.runs:
            if parsed_font_size is not None:
                run.font.size = parsed_font_size
            if bold is not None:
                run.font.bold = bold
            if italic is not None:
                run.font.italic = italic
            if font_name is not None:
                run.font.name = font_name
            if color is not None:
                if run.font.color is not None:
                    run.font.color.rgb = RGBColor(*color)
                else:
                    _set_run_font_color(run, color)

def set_paragraph_line_spacing(shape, line_spacing=1.0):
    text_frame = shape.text_frame
    for paragraph in text_frame.paragraphs:
        paragraph.line_spacing = line_spacing

def set_shape_text_margins(shape, top_px=0, right_px=0, bottom_px=0, left_px=0):
    text_frame = shape.text_frame
    text_frame.auto_size = MSO_AUTO_SIZE.NONE
    text_frame.margin_top = Pt(_px_to_pt(top_px))
    text_frame.margin_right = Pt(_px_to_pt(right_px))
    text_frame.margin_bottom = Pt(_px_to_pt(bottom_px))
    text_frame.margin_left = Pt(_px_to_pt(left_px))

def adjust_font_size(shape, delta=2):
    text_frame = shape.text_frame
    text_frame.auto_size = MSO_AUTO_SIZE.NONE
    for paragraph in text_frame.paragraphs:
        for run in paragraph.runs:
            current_size = run.font.size
            if current_size is not None:
                new_size = current_size.pt + delta
                if new_size < 1:
                    new_size = 1
                run.font.size = Pt(new_size)

def fill_textframe(shape, paragraphs_spec):
    text_frame = shape.text_frame
    text_frame.auto_size = MSO_AUTO_SIZE.NONE
    text_frame.word_wrap = True
    text_frame.clear()
    for p_data in paragraphs_spec:
        p = text_frame.add_paragraph()
        p.level = p_data.get("level", 0)
        align_str = p_data.get("alignment", "left")
        p.alignment = _parse_alignment(align_str)
        default_font_size = p_data.get("font_size", 24)
        p.font.size = Pt(default_font_size)
        runs_spec = p_data.get("runs", [])
        for run_info in runs_spec:
            run = p.add_run()
            if p_data.get("bullet", False):
                if p.level == 0:
                    run.text = '\u2022' + run_info.get("text", "")
                elif p.level == 1:
                    run.text = '\u25E6' + run_info.get("text", "")
                else:
                    run.text = '\u25AA' + run_info.get("text", "")
            else:
                run.text = run_info.get("text", "")
            font = run.font
            font.bold = run_info.get("bold", False)
            font.italic = run_info.get("italic", False)
            color_tuple = run_info.get("color", None)
            if color_tuple and len(color_tuple) == 3 and all(isinstance(c, int) for c in color_tuple):
                if run.font.color is not None:
                    run.font.color.rgb = RGBColor(*color_tuple)
                else:
                    _set_run_font_color(run, color_tuple)
            if "font_size" in run_info:
                font.size = Pt(run_info["font_size"])
            fill_color_tuple = run_info.get("fill_color", None)
            if fill_color_tuple and len(fill_color_tuple) == 3 and all(isinstance(c, int) for c in fill_color_tuple):
                shape.fill.solid()
                shape.fill.fore_color.rgb = RGBColor(*fill_color_tuple)


# ---------------------------------------------------------------------------
# Border / hierarchy operations
# ---------------------------------------------------------------------------

def add_border(prs, border_color=RGBColor(255, 0, 0), border_width=Pt(2)):
    labeled_elements = {}
    for slide in prs.slides:
        for shape in slide.shapes:
            try:
                shape.line.fill.solid()
                shape.line.fill.fore_color.rgb = border_color
                shape.line.width = border_width
                if hasattr(shape, 'name'):
                    labeled_elements[shape.name] = {
                        'left': f'{emu_to_inches(shape.left)} Inches',
                        'top': f'{emu_to_inches(shape.top)} Inches',
                        'width': f'{emu_to_inches(shape.width)} Inches',
                        'height': f'{emu_to_inches(shape.height)} Inches',
                    }
            except Exception as e:
                print(f"Could not add border to shape (type={shape.shape_type}): {e}")
    return labeled_elements

def add_border_hierarchy(prs, name_to_hierarchy, hierarchy,
                         border_color=RGBColor(255, 0, 0), border_width=2,
                         fill_boxes=False, fill_color=RGBColor(255, 0, 0), regardless=False):
    border_width = Pt(border_width)
    labeled_elements = {}
    for slide_idx, slide in enumerate(prs.slides):
        for shape_idx, shape in enumerate(slide.shapes):
            shape_name = shape.name if hasattr(shape, 'name') else f"Shape_{slide_idx}_{shape_idx}"
            labeled_elements[shape_name] = {
                'left': f"{emu_to_inches(shape.left):.2f} Inches",
                'top': f"{emu_to_inches(shape.top):.2f} Inches",
                'width': f"{emu_to_inches(shape.width):.2f} Inches",
                'height': f"{emu_to_inches(shape.height):.2f} Inches",
            }
            current_hierarchy = name_to_hierarchy.get(shape_name, None)
            if current_hierarchy is None:
                print(f"Warning: shape '{shape_name}' not found in name_to_hierarchy.")
            try:
                if current_hierarchy == hierarchy or regardless:
                    shape.line.fill.solid()
                    shape.line.fill.fore_color.rgb = border_color
                    shape.line.width = border_width
                    if fill_boxes:
                        shape.fill.solid()
                        shape.fill.fore_color.rgb = fill_color
                else:
                    shape.line.width = Pt(0)
                    shape.line.fill.background()
                    if shape.has_text_frame:
                        shape.text_frame.text = ""
            except Exception as e:
                print(f"Could not process shape '{shape_name}' (type={shape.shape_type}): {e}")
    return labeled_elements

def get_visual_cues(name_to_hierarchy, identifier, paper_path='poster.pptx'):
    prs = pptx.Presentation(paper_path)
    position_dict_1 = add_border_hierarchy(prs, name_to_hierarchy, 1, border_width=10)
    json.dump(position_dict_1, open(f"tmp/position_dict_1_<{identifier}>.json", "w"))
    save_presentation(prs, file_name=f"tmp/poster_<{identifier}>_hierarchy_1.pptx")

    prs = pptx.Presentation(paper_path)
    add_border_hierarchy(prs, name_to_hierarchy, 1, border_width=10, fill_boxes=True)
    save_presentation(prs, file_name=f"tmp/poster_<{identifier}>_hierarchy_1_filled.pptx")

    prs = pptx.Presentation(paper_path)
    position_dict_2 = add_border_hierarchy(prs, name_to_hierarchy, 2, border_width=10)
    json.dump(position_dict_2, open(f"tmp/position_dict_2_<{identifier}>.json", "w"))
    save_presentation(prs, file_name=f"tmp/poster_<{identifier}>_hierarchy_2.pptx")

    prs = pptx.Presentation(paper_path)
    add_border_hierarchy(prs, name_to_hierarchy, 2, border_width=10, fill_boxes=True)
    save_presentation(prs, file_name=f"tmp/poster_<{identifier}>_hierarchy_2_filled.pptx")


# ---------------------------------------------------------------------------
# Hierarchy / outline parsing
# ---------------------------------------------------------------------------

def get_hierarchy(outline, hierarchy=1):
    name_to_hierarchy = {}
    for key, section in outline.items():
        if key == "meta":
            continue
        name_to_hierarchy[section['name']] = hierarchy
        if 'subsections' in section:
            name_to_hierarchy.update(get_hierarchy(section['subsections'], hierarchy+1))
    return name_to_hierarchy

def get_hierarchy_by_keys(outline, hierarchy=1):
    name_to_hierarchy = {}
    for key, section in outline.items():
        if key == "meta":
            continue
        name_to_hierarchy[key] = hierarchy
        if 'subsections' in section:
            name_to_hierarchy.update(get_hierarchy_by_keys(section['subsections'], hierarchy+1))
    return name_to_hierarchy

def rename_keys_with_name(data):
    if not isinstance(data, dict):
        return data
    new_dict = {}
    for key, value in data.items():
        if isinstance(value, dict) and "name" in value:
            new_key = value["name"]
            new_dict[new_key] = rename_keys_with_name(value)
        else:
            new_dict[key] = rename_keys_with_name(value)
    return new_dict

