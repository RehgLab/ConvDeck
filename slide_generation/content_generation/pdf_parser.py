import json
import re
from pathlib import Path
from pprint import pprint

from docling_core.types.doc import ImageRefMode, PictureItem, TableItem
from docling_core.types.doc.document import BoundingBox
from PIL import Image as PILImage

from utils.llm import *
from utils.metrics import *
from utils.styling import *
from utils.pptx import *
from utils.helpers import *


def safe_print_element_fields(element):
    print(f"[Type] {type(element)}")
    safe_dict = {}
    for k, v in vars(element).items():
        if isinstance(v, (str, int, float, tuple, list, dict, type(None))):
            safe_dict[k] = v
        else:
            safe_dict[k] = f"<{type(v).__name__}>"
    pprint(safe_dict)
 
def convert_bbox_to_pil_coords(bbox, page_width, page_height, pad=10):
    """
    将 BOTTOMLEFT 坐标的 bbox 转为 PIL 坐标（左上角原点） 
    """
    x0 = bbox.l
    x1 = bbox.r 
    y0 = page_height - bbox.b  # PIL y0: corresponds to the bottom of the PDF bbox
    y1 = page_height - bbox.t  # PIL y1: corresponds to the top of the PDF bbox

    # Add padding (and clip to image boundaries)
    return (
        max(0, x0 - pad),
        max(0, y1 - pad),
        min(page_width, x1 + pad),
        min(page_height, y0 + pad),
    )
  
def gen_image_and_table(args, conv_res):
    input_token, output_token = 0, 0
    raw_source = args.paper_path

    output_dir = Path(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}')

    output_dir.mkdir(parents=True, exist_ok=True)
    doc_filename = args.paper_name

    # Save page images
    for page_no, page in conv_res.document.pages.items():
        page_no = page.page_no
        page_image_filename = output_dir / f"{doc_filename}-{page_no}.png"
        with page_image_filename.open("wb") as fp:
            page.image.pil_image.save(fp, format="PNG")
 
    # 修改裁剪框（扩大上下左右）  
    from PIL import Image
    
    for i, picture in enumerate(conv_res.document.pictures):
        page_no = picture.prov[0].page_no
        page = conv_res.document.pages[page_no]  
        full_img = page.image.pil_image   
        page_size_in_points = page.size  #  PDF 中的尺寸（单位 pt）
        page_width_pt, page_height_pt = page_size_in_points.width, page_size_in_points.height

        scale = full_img.width / page_width_pt
         
        bbox = picture.prov[0].bbox
        pad = 1   
        padded_bbox = BoundingBox(
            l= bbox.l - pad ,
            r=  bbox.r + pad ,
            b=   bbox.b - pad ,
            t=   bbox.t + pad ,
            coord_origin=bbox.coord_origin,
        )
        tl_bbox = padded_bbox.to_top_left_origin(page_height=page_height_pt)
        pil_box = tl_bbox.scaled(scale=scale).as_tuple() 
        left, top, right, bottom = pil_box
        cropped = full_img.crop((left, top, right, bottom))  
        cropped.save( f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/{args.paper_name}-picture-{i+1}.png')
    
    table_counter = 0 
    for element, _level in conv_res.document.iterate_items():
        if isinstance(element, TableItem):
            table_counter += 1
            element_image_filename = (
                output_dir / f"{doc_filename}-table-{table_counter}.png"
            )
            with element_image_filename.open("wb") as fp:
                element.get_image(conv_res.document).save(fp, "PNG")
   

    # Save markdown with embedded pictures
    md_filename = output_dir / f"{doc_filename}-with-images.md"
    conv_res.document.save_as_markdown(md_filename, image_mode=ImageRefMode.EMBEDDED)

    # Save markdown with externally referenced pictures
    md_filename = output_dir / f"{doc_filename}-with-image-refs.md"
    conv_res.document.save_as_markdown(md_filename, image_mode=ImageRefMode.REFERENCED)

    # Save HTML with externally referenced pictures
    html_filename = output_dir / f"{doc_filename}-with-image-refs.html"
    conv_res.document.save_as_html(html_filename, image_mode=ImageRefMode.REFERENCED)

    tables = {}

    table_index = 1
    for table in conv_res.document.tables:
        caption = table.caption_text(conv_res.document)
        if len(caption) > 0:
            table_img_path = f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/{args.paper_name}-table-{table_index}.png'
            table_img = PILImage.open(table_img_path)
            tables[str(table_index)] = {
                'caption': caption,
                'page_no': table.prov[0].page_no,
                'table_path': table_img_path,
                'width': table_img.width,
                'height': table_img.height,
                'figure_size': table_img.width * table_img.height,
                'figure_aspect': table_img.width / table_img.height,
            }

        table_index += 1

    images = {}
    image_index = 1
    for image in conv_res.document.pictures:
        caption = image.caption_text(conv_res.document) 
        print(f"[{i}] caption: {caption}")
        if len(caption) > 0:
            image_img_path = f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/{args.paper_name}-picture-{image_index}.png'
            image_img = PILImage.open(image_img_path)
            images[str(image_index)] = {
                'caption': caption,
                'page_no': image.prov[0].page_no,
                'image_path': image_img_path,
                'width': image_img.width,
                'height': image_img.height,
                'figure_size': image_img.width * image_img.height,
                'figure_aspect': image_img.width / image_img.height,
            }
        image_index += 1

    json.dump(images, open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}_images.json', 'w'), indent=4)
    json.dump(tables, open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}_tables.json', 'w'), indent=4)

    return input_token, output_token, images, tables

def get_reference_from_markdown(markdown_content: str) -> str:
    """
    Get References section from markdown content.
    Stops at Appendix or any other major section below References.
    """
    references_section_patterns = [
        re.compile(r'^#+\s*References', re.IGNORECASE),
        re.compile(r'^\d+\.?\s*References', re.IGNORECASE),
        re.compile(r'^#+\s*1\.?\s*References', re.IGNORECASE),
    ]
    
    # Patterns for sections that come after References
    end_section_patterns = [
        re.compile(r'^#+\s*Appendix', re.IGNORECASE),
        re.compile(r'^#+\s*(?:Acknowledgments?|Acknowledgements?)', re.IGNORECASE),
        re.compile(r'^#+\s*Author\s+Contributions', re.IGNORECASE),
        re.compile(r'^#+\s*Declaration\s+of\s+Competing\s+Interest', re.IGNORECASE),
        re.compile(r'^#+\s*Data\s+Availability', re.IGNORECASE),
        re.compile(r'^#+\s*Supplementary\s+Material', re.IGNORECASE),
        re.compile(r'^#+\s*Bibliography', re.IGNORECASE),  # In case Bibliography comes after References
    ]
    
    lines = markdown_content.split('\n')
    ref_start_index = None
    ref_end_index = len(lines)
    
    # Find where References section starts
    for i, line in enumerate(lines):
        for pattern in references_section_patterns:
            if pattern.match(line.strip()):
                ref_start_index = i
                break
        if ref_start_index is not None:
            break
    
    if ref_start_index is None:
        # No References section found
        return ""
    
    # Find where References section ends (next major section)
    for i in range(ref_start_index + 1, len(lines)):
        line = lines[i].strip()
        
        # Check if this is a new major section heading
        if line.startswith('#'):
            # Check if it's a top-level heading (single # or ##)
            if line.startswith('# ') or line.startswith('## '):
                # Check if it matches any end section pattern
                for pattern in end_section_patterns:
                    if pattern.match(line):
                        ref_end_index = i
                        break
                if ref_end_index < len(lines):
                    break
                # Also check if it's another major section (not References)
                if not any(ref_pattern.match(line) for ref_pattern in references_section_patterns):
                    # If it's a major heading and we've had some content, it might be the end
                    if i > ref_start_index + 5:  # Allow some content before checking
                        ref_end_index = i
                        break
    
    # Extract only the References section
    reference_content = '\n'.join(lines[ref_start_index:ref_end_index])
    return reference_content

def create_reference_dict(reference_content: str) -> dict:
    """
    Parse reference section and create two dictionaries:
    1. Numbered dict: {1: "reference text", 2: "reference text", ...}  (keys as strings for JSON)
    2. Citation dict: {"Author et al. YEAR": "reference text", ...}
    
    Supports both author-year format and numbered in-text format like "[15]", "[15, 42]".
    When the reference section uses numbered format (e.g. "- [1] Author... - [2] Author..."),
    references are split by that pattern so in-text [15] maps to the 15th reference.
    
    Args:
        reference_content: Markdown content of the References section
        
    Returns:
        Dictionary with two keys:
        - "numbered": dict mapping numbers to references
        - "citations": dict mapping citation patterns to references
    """
    if not reference_content or not reference_content.strip():
        return {"numbered": {}, "citations": {}}
    
    lines = reference_content.split('\n')
    
    # Remove the "## References" header if present
    start_idx = 0
    for i, line in enumerate(lines):
        if re.match(r'^#+\s*References', line, re.IGNORECASE):
            start_idx = i + 1
            break
    content_from_header = '\n'.join(lines[start_idx:])
    # Normalize: single line of text for regex (collapse newlines to space so " - [2]" is findable)
    content_flat = re.sub(r'\s+', ' ', content_from_header).strip()

    # Detect numbered reference format: " - [1] ", " - [2] " (e.g. "- [15] Author...")
    numbered_ref_pattern = re.compile(r'\s-\s*\[(\d+)\]\s+')
    references_list = []
    numbered_dict = {}

    if numbered_ref_pattern.search(content_flat):
        # Split by " - [N] " so we get one reference per number; keys match in-text [N]
        parts = numbered_ref_pattern.split(content_flat)
        # parts[0] = text before first " - [1] ", parts[1]="1", parts[2]=ref1 text, parts[3]="2", ...
        for i in range(1, len(parts) - 1, 2):
            num_str = parts[i]
            ref_text = parts[i + 1].strip()
            # Drop trailing in-text citation numbers (e.g. ". 1, 2, 5, 6" at end of ref line)
            # Only strip when preceded by a period to avoid removing page numbers like "pages 1, 2, 3"
            ref_text = re.sub(r'\.\s+\d+(?:\s*,\s*\d+)*\s*$', '.', ref_text)
            if ref_text:
                numbered_dict[num_str] = ref_text
                references_list.append((int(num_str), ref_text))
        # Sort by number so references_list order matches document order
        references_list.sort(key=lambda x: x[0])
        references_list = [ref_text for _, ref_text in references_list]
    else:
        # Original logic: parse by blank lines and "new reference" heuristics
        references_list = []
        current_ref_lines = []

        for line in lines[start_idx:]:
            line = line.strip()

            if not line:
                if current_ref_lines:
                    ref_text = ' '.join(current_ref_lines).strip()
                    if ref_text:
                        references_list.append(ref_text)
                    current_ref_lines = []
            else:
                if current_ref_lines:
                    prev_text = ' '.join(current_ref_lines)
                    if prev_text.rstrip().endswith('.') and re.match(r'^[A-Z][a-z]+', line):
                        ref_text = prev_text.strip()
                        if ref_text:
                            references_list.append(ref_text)
                        current_ref_lines = [line]
                    else:
                        current_ref_lines.append(line)
                else:
                    current_ref_lines = [line]

        if current_ref_lines:
            ref_text = ' '.join(current_ref_lines).strip()
            if ref_text:
                references_list.append(ref_text)

        # Create numbered dictionary: 1-based index (keys as strings for JSON)
        for idx, ref_text in enumerate(references_list, start=1):
            numbered_dict[str(idx)] = ref_text
    # ###################
    # Create citation dictionary: {"Author et al. YEAR": ref_text, ...}
    citations_dict = {}
    
    for ref_text in references_list:
        # Extract first author and year from reference
        # Pattern 1: "FirstAuthor, SecondAuthor, ..., and X others. YEAR."
        # Pattern 2: "FirstAuthor, SecondAuthor, ..., and LastAuthor. YEAR."
        # Pattern 3: "FirstAuthor SecondAuthor ... LastAuthor. YEAR."
        
        # Try to find year first (4-digit year, typically 19xx or 20xx)
        year_match = re.search(r'\b(19|20)\d{2}\b', ref_text)
        if not year_match:
            continue
        
        year = year_match.group(0)
        year_pos = year_match.start()
        
        # Extract text before year (should contain authors)
        author_text = ref_text[:year_pos].strip()
        
        # Remove trailing punctuation
        author_text = author_text.rstrip('.,;:')
        
        # Extract first author's last name
        # Format: "FirstName LastName, SecondAuthor, ..." - last name is before comma
        if ',' in author_text:
            # Format: "FirstName LastName, SecondAuthor, ..."
            first_part = author_text.split(',')[0].strip()
            # Extract last name (last word before comma)
            first_author_parts = first_part.split()
            if len(first_author_parts) > 0:
                # Last name is typically the last word (e.g., "Josh Achiam" -> "Achiam")
                first_author_lastname = first_author_parts[-1]
            else:
                first_author_lastname = first_part
        else:
            # No comma - format might be "FirstName LastName ..."
            first_author_parts = author_text.split()
            if len(first_author_parts) > 0:
                # Take first word as last name (less common format)
                first_author_lastname = first_author_parts[0]
            else:
                continue
        
        # Check if there are multiple authors (look for "and" or "and X others")
        has_multiple = re.search(r',\s+and\s+', author_text, re.IGNORECASE) or \
                      re.search(r',\s+and\s+\d+\s+others', author_text, re.IGNORECASE) or \
                      (',' in author_text and len([x for x in author_text.split(',') if x.strip()]) > 1)
        
        # Create citation patterns
        if has_multiple:
            # Multiple authors - use "et al." format
            citation_patterns = [
                f"{first_author_lastname} et al. {year}",
                f"{first_author_lastname} et al., {year}",
                f"{first_author_lastname} et al {year}",
                f"{first_author_lastname}, et al. {year}",
            ]
        else:
            # Single author
            citation_patterns = [
                f"{first_author_lastname} {year}",
                f"{first_author_lastname}, {year}",
            ]
        
        # Add patterns to citations dict
        for pattern in citation_patterns:
            if pattern not in citations_dict:
                citations_dict[pattern] = ref_text
    
    return {
        "numbered": numbered_dict,
        "citations": citations_dict
    }

def reformat_slides(input_json):
    """
    Reformats a list of slide dictionaries into the required nested JSON structure.

    Parameters:
        input_json (list): List of dictionaries with keys "title" and "content"

    Returns:
        dict: Reformatted JSON structure
    """
    
    # Ensure input is a list
    if not isinstance(input_json, list):
        raise ValueError("Input must be a list of dictionaries.")

    subsections = []

    for item in input_json:
        # Validate required keys
        if "title" not in item or "content" not in item:
            raise ValueError("Each item must contain 'title' and 'content' keys.")
        
        subsections.append({
            "title": item["title"],
            "content": item["content"]
        })

    output = {
        "sections": [
            {
                "title": "Title",   # As requested, outer title fixed
                "subsections": subsections
            }
        ]
    }

    return output
    
