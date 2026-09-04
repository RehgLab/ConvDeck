"""Content metrics: sizing, estimation, and validation functions."""

import copy
import json
import math


def compute_bullet_length(textbox_content):
    total = 0
    for bullet in textbox_content:
        for run in bullet['runs']:
            total += len(run['text'])
    return total


def check_bounding_boxes(bboxes, overall_width, overall_height):
    """
    Given a dictionary 'bboxes' whose keys are bounding-box names and whose values are
    dictionaries with keys 'left', 'top', 'width', and 'height' (all floats),
    along with the overall canvas width and height, this function checks for:

      1) An overlap between any two bounding boxes (it returns a tuple of their names).
      2) A bounding box that extends beyond the overall width or height (it returns a tuple
         containing just that bounding box's name).

    It stops upon finding the first error:
      - If an overlap is found first, it returns (name1, name2).
      - Otherwise, if an overflow is found, it returns (name,).
      - If nothing is wrong, it returns ().

    Parameters:
        bboxes (dict): e.g. {
            "box1": {"left": 10.0, "top": 10.0, "width": 50.0, "height": 20.0},
            "box2": {"left": 55.0, "top": 15.0, "width": 10.0, "height": 10.0},
            ...
        }
        overall_width (float): The total width of the available space.
        overall_height (float): The total height of the available space.

    Returns:
        tuple: Either (box1, box2) if an overlap is found,
               (box,) if a bounding box overflows,
               or () if no problem is found.
    """

    # Convert bboxes into a list of (name, left, top, width, height) for easier iteration.
    box_list = []
    for name, coords in bboxes.items():
        left = coords["left"]
        top = coords["top"]
        width = coords["width"]
        height = coords["height"]
        box_list.append((name, left, top, width, height))

    # Helper function to check overlap between two boxes
    def boxes_overlap(box_a, box_b):
        # Unpack bounding-box data
        name_a, left_a, top_a, width_a, height_a = box_a
        name_b, left_b, top_b, width_b, height_b = box_b

        # Compute right and bottom coordinates
        right_a = left_a + width_a
        bottom_a = top_a + height_a
        right_b = left_b + width_b
        bottom_b = top_b + height_b

        # Rectangles overlap if not separated along either x or y axis
        # If one box is completely to the left or right or above or below the other,
        # there's no overlap.
        no_overlap = (right_a <= left_b or  # A is completely left of B
                      right_b <= left_a or  # B is completely left of A
                      bottom_a <= top_b or  # A is completely above B
                      bottom_b <= top_a)    # B is completely above A
        return not no_overlap

    # 1) Check for overlap first
    n = len(box_list)
    for i in range(n):
        for j in range(i + 1, n):
            if boxes_overlap(box_list[i], box_list[j]):
                return (box_list[i][0], box_list[j][0])  # Return names

    # 2) Check for overflow
    for name, left, top, width, height in box_list:
        right = left + width
        bottom = top + height

        # If boundary is outside [0, overall_width] or [0, overall_height], it's an overflow
        if (left < 0 or top < 0 or right > overall_width or bottom > overall_height):
            return (name,)

    # 3) If nothing is wrong, return empty tuple
    return ()


def is_poster_filled(
    bounding_boxes: dict,
    overall_width: float,
    overall_height: float,
    max_lr_margin: float,
    max_tb_margin: float
) -> bool:
    """
    Given a dictionary of bounding boxes (keys are box names and
    values are dicts with float keys: "left", "top", "width", "height"),
    along with the overall dimensions of the poster and maximum allowed
    margins, this function determines whether the boxes collectively
    fill the poster within those margin constraints.

    :param bounding_boxes: Dictionary of bounding boxes of the form:
                          {
                              "box1": {"left": float, "top": float, "width": float, "height": float},
                              "box2": {...},
                              ...
                          }
    :param overall_width: Total width of the poster
    :param overall_height: Total height of the poster
    :param max_lr_margin: Maximum allowed left and right margins
    :param max_tb_margin: Maximum allowed top and bottom margins
    :return: True if the bounding boxes fill the poster (with no big leftover spaces),
             False otherwise.
    """

    # If there are no bounding boxes, we consider the poster unfilled.
    if not bounding_boxes:
        return False

    # Extract the minimum left, maximum right, minimum top, and maximum bottom from all bounding boxes.
    min_left = min(b["left"] for b in bounding_boxes.values())
    max_right = max(b["left"] + b["width"] for b in bounding_boxes.values())
    min_top = min(b["top"] for b in bounding_boxes.values())
    max_bottom = max(b["top"] + b["height"] for b in bounding_boxes.values())

    # Calculate leftover margins.
    leftover_left = min_left
    leftover_right = overall_width - max_right
    leftover_top = min_top
    leftover_bottom = overall_height - max_bottom

    # Check if leftover margins exceed the allowed maxima.
    if (leftover_left > max_lr_margin or leftover_right > max_lr_margin or
        leftover_top > max_tb_margin or leftover_bottom > max_tb_margin):
        return False

    return True


def check_and_fix_subsections(section, subsections):
    """
    Given a 'section' bounding box and a dictionary of 'subsections',
    checks:

    1) That each subsection is within the main section and that
       no two subsections overlap.
       - If there is a problem, returns a tuple of the names of
         the offending subsections.

    2) That the subsections fully occupy the area of 'section'.
       - If not, greedily expand each subsection (in the order
         left->right->top->bottom), and return a dictionary of
         the updated bounding boxes for the subsections.

    3) Otherwise, returns an empty tuple if nothing is wrong.

    :param section: dict with keys "left", "top", "width", "height".
    :param subsections: dict mapping name -> dict with "left", "top", "width", "height".
    :return: Either
        - tuple of subsection names that are out of bounds or overlapping,
        - dict of expanded bounding boxes if they do not fully occupy 'section',
        - or an empty tuple if everything is correct.
    """

    # --- Utility functions ---
    def right(rect):
        return rect["left"] + rect["width"]

    def bottom(rect):
        return rect["top"] + rect["height"]

    def is_overlapping(r1, r2):
        """
        Returns True if rectangles r1 and r2 overlap (strictly),
        False otherwise.
        """
        return not (
            right(r1) <= r2["left"]
            or r1["left"] >= right(r2)
            or bottom(r1) <= r2["top"]
            or r1["top"] >= bottom(r2)
        )

    # 1) Check each subsection is within the main section
    names_violating = set()
    sec_left, sec_top = section["left"], section["top"]
    sec_right = section["left"] + section["width"]
    sec_bottom = section["top"] + section["height"]

    for name, sub in subsections.items():
        # Check boundary
        sub_left, sub_top = sub["left"], sub["top"]
        sub_right, sub_bottom = right(sub), bottom(sub)
        if (
            sub_left < sec_left
            or sub_top < sec_top
            or sub_right > sec_right
            or sub_bottom > sec_bottom
        ):
            # Out of bounds
            names_violating.add(name)

    # 2) Check pairwise overlaps
    sub_keys = list(subsections.keys())
    for i in range(len(sub_keys)):
        for j in range(i + 1, len(sub_keys)):
            n1, n2 = sub_keys[i], sub_keys[j]
            if is_overlapping(subsections[n1], subsections[n2]):
                # Mark both as violating
                names_violating.add(n1)
                names_violating.add(n2)

    # If anything violated boundaries or overlapped, return them as a tuple
    if names_violating:
        return tuple(sorted(names_violating))

    # 3) Check if subsections fully occupy the section by area.
    #    (Since we've checked there's no overlap, area-based check is safe for "full coverage".)
    area_section = section["width"] * section["height"]
    area_subs = sum(
        sub["width"] * sub["height"] for sub in subsections.values()
    )

    if area_subs < area_section:
        # -- We need to expand subsections greedily. --

        # Make a copy of the bounding boxes so as not to modify originals.
        expanded_subs = {
            name: {
                "left": sub["left"],
                "top": sub["top"],
                "width": sub["width"],
                "height": sub["height"],
            }
            for name, sub in subsections.items()
        }

        # Helper to see whether we are touching a boundary or another subsection
        def touching_left(sname, sbox):
            if abs(sbox["left"] - sec_left) < 1e-9:
                # touches main section left boundary
                return True
            # touches the right edge of another subsection
            for oname, obox in expanded_subs.items():
                if oname == sname:
                    continue
                if abs(right(obox) - sbox["left"]) < 1e-9:
                    return True
            return False

        def touching_right(sname, sbox):
            r = right(sbox)
            if abs(r - sec_right) < 1e-9:
                return True
            for oname, obox in expanded_subs.items():
                if oname == sname:
                    continue
                if abs(obox["left"] - r) < 1e-9:
                    return True
            return False

        def touching_top(sname, sbox):
            if abs(sbox["top"] - sec_top) < 1e-9:
                return True
            for oname, obox in expanded_subs.items():
                if oname == sname:
                    continue
                if abs(bottom(obox) - sbox["top"]) < 1e-9:
                    return True
            return False

        def touching_bottom(sname, sbox):
            b = bottom(sbox)
            if abs(b - sec_bottom) < 1e-9:
                return True
            for oname, obox in expanded_subs.items():
                if oname == sname:
                    continue
                if abs(obox["top"] - b) < 1e-9:
                    return True
            return False

        # Attempt a single pass of expansions, left->right->top->bottom
        for name in expanded_subs:
            sub = expanded_subs[name]

            # Expand left if not touching left boundary or another box
            if not touching_left(name, sub):
                # The "left boundary" is the maximum "right" of any subsection strictly to the left,
                # or the section's left boundary, whichever is larger.
                left_bound = sec_left
                for oname, obox in expanded_subs.items():
                    if oname == name:
                        continue
                    r_ = obox["left"] + obox["width"]
                    # only consider those that are strictly left of this sub
                    if r_ <= sub["left"] and r_ > left_bound:
                        left_bound = r_
                # Now expand
                delta = sub["left"] - left_bound
                if delta > 1e-9:  # If there's any real gap
                    sub["width"] += delta
                    sub["left"] = left_bound

            # Expand right if not touching right boundary or another box
            if not touching_right(name, sub):
                right_bound = sec_right
                sub_right = sub["left"] + sub["width"]
                for oname, obox in expanded_subs.items():
                    if oname == name:
                        continue
                    left_ = obox["left"]
                    # only consider those that are strictly to the right
                    if left_ >= sub_right and left_ < right_bound:
                        right_bound = left_
                delta = right_bound - (sub["left"] + sub["width"])
                if delta > 1e-9:
                    sub["width"] += delta

            # Expand top if not touching top boundary or another box
            if not touching_top(name, sub):
                top_bound = sec_top
                for oname, obox in expanded_subs.items():
                    if oname == name:
                        continue
                    b_ = obox["top"] + obox["height"]
                    if b_ <= sub["top"] and b_ > top_bound:
                        top_bound = b_
                delta = sub["top"] - top_bound
                if delta > 1e-9:
                    sub["height"] += delta
                    sub["top"] = top_bound

            # Expand bottom if not touching bottom boundary or another box
            if not touching_bottom(name, sub):
                bottom_bound = sec_bottom
                sub_bottom = sub["top"] + sub["height"]
                for oname, obox in expanded_subs.items():
                    if oname == name:
                        continue
                    other_top = obox["top"]
                    if other_top >= sub_bottom and other_top < bottom_bound:
                        bottom_bound = other_top
                delta = bottom_bound - (sub["top"] + sub["height"])
                if delta > 1e-9:
                    sub["height"] += delta

        # After expansion, return the expanded dictionary
        # per the spec: "If the second case happens, return a dictionary ...
        # containing the modified bounding box dictionaries."
        return expanded_subs

    # If we get here, then area_subs == area_section and there's no overlap => all good
    return ()


def char_capacity(
    bbox,
    font_size_px=40 * (96 / 72),  # Default font size in px (40pt converted to px)
    *,
    # Average glyph width as fraction of font-size (approx 0.6 for monospace,
    # approx 0.52-0.55 for most proportional sans-serif faces)
    avg_width_ratio: float = 0.54,
    line_height_ratio: float = 1,
    # Optional inner padding in px that the renderer might reserve
    padding_px: int = 0,
) -> int:
    """
    Estimate the number of characters that will fit into a rectangular text box.

    Parameters
    ----------
    bbox : (x, y, height, width)  # all in pixels
    font_size_px : int           # font size in px
    avg_width_ratio : float      # average char width / fontSize
    line_height_ratio : float    # line height / fontSize
    padding_px : int             # optional inner padding on each side

    Returns
    -------
    int : estimated character capacity
    """
    CHAR_CONST = 10
    _, _, height_px, width_px = bbox

    usable_w = max(0, width_px - 2 * padding_px)
    usable_h = max(0, height_px - 2 * padding_px)

    if usable_w == 0 or usable_h == 0:
        return 0  # box is too small

    avg_char_w = font_size_px * avg_width_ratio
    line_height = font_size_px * line_height_ratio

    chars_per_line = max(1, math.floor(usable_w / avg_char_w))
    lines = max(1, math.floor(usable_h / line_height))

    return chars_per_line * lines * CHAR_CONST


def scale_to_target_area(width, height, target_width=900, target_height=1200):
    """
    Scale the given width and height by the same factor to achieve a new area equal
    to target_width * target_height while preserving the aspect ratio.

    Parameters:
      width (float or int): The original width.
      height (float or int): The original height.
      target_width (int, optional): The target width for area calculation. Default is 900.
      target_height (int, optional): The target height for area calculation. Default is 1200.

    Returns:
      tuple: (new_width, new_height) after scaling such that the area is target_width * target_height.
    """
    # Calculate target area from provided dimensions.
    target_area = target_width * target_height

    # Calculate original area
    current_area = width * height

    # Compute scale factor required: s^2 * (width * height) = target_area => s = sqrt(target_area / (width * height))
    scale_factor = math.sqrt(target_area / current_area)

    # Calculate new dimensions
    new_width = width * scale_factor
    new_height = height * scale_factor

    # Optional: Round the dimensions to integers.
    return int(round(new_width)), int(round(new_height))


def estimate_characters(width_in_inches, height_in_inches, font_size_points, line_spacing_points=None):
    """
    Estimate the number of characters that can fit into a bounding box.

    :param width_in_inches:  The width of the bounding box, in inches.
    :param height_in_inches: The height of the bounding box, in inches.
    :param font_size_points: The font size, in points.
    :param line_spacing_points: (Optional) The line spacing, in points.
                                Defaults to 1.5 * font_size_points if not provided.
    :return: Estimated number of characters that fit in the bounding box.
    """
    if line_spacing_points is None:
        # Default line spacing is 1.5 times the font size
        line_spacing_points = 1.5 * font_size_points

    # 1 inch = 72 points
    width_in_points = width_in_inches * 72
    height_in_points = height_in_inches * 72

    # Rough approximation of the average width of a character: half of the font size
    avg_char_width = 0.5 * font_size_points

    # Number of characters that can fit per line
    chars_per_line = int(width_in_points // avg_char_width)

    # Number of lines that can fit in the bounding box
    lines_count = int(height_in_points // line_spacing_points)

    # Total number of characters
    total_characters = chars_per_line * lines_count

    return total_characters


def equivalent_length_with_forced_breaks(text, width_in_inches, font_size_points):
    """
    Returns the "width-equivalent length" of the text when forced newlines
    are respected. Each physical line (including partial) is counted as if it
    had 'max_chars_per_line' characters.

    This number can exceed len(text), because forced newlines waste leftover
    space on the line.
    """
    # 1 inch = 72 points
    width_in_points = width_in_inches * 72
    avg_char_width = 0.5 * font_size_points

    # How many characters fit in one fully occupied line?
    max_chars_per_line = int(width_in_points // avg_char_width)

    # Split on explicit newlines
    logical_lines = text.split('\n')

    total_equiv_length = 0

    for line in logical_lines:
        # If the line is empty, we still "use" one line (which is max_chars_per_line slots).
        if not line:
            total_equiv_length += max_chars_per_line
            continue

        line_length = len(line)
        # How many sub-lines (wraps) does it need?
        sub_lines = math.ceil(line_length / max_chars_per_line)

        # Each sub-line is effectively counted as if it were fully used
        total_equiv_length += sub_lines * max_chars_per_line

    return total_equiv_length


def actual_rendered_length(
    text,
    width_in_inches,
    height_in_inches,
    font_size_points,
    line_spacing_points=None
):
    """
    Estimate how many characters from `text` will actually fit in the bounding
    box, taking into account explicit newlines.
    """
    if line_spacing_points is None:
        line_spacing_points = 1.5 * font_size_points

    # 1 inch = 72 points
    width_in_points = width_in_inches * 72
    height_in_points = height_in_inches * 72

    # Estimate average character width
    avg_char_width = 0.5 * font_size_points

    # Maximum chars per line (approx)
    max_chars_per_line = int(width_in_points // avg_char_width)

    # Maximum number of lines that can fit
    max_lines = int(height_in_points // line_spacing_points)

    # Split on newline chars to get individual "logical" lines
    logical_lines = text.split('\n')

    used_lines = 0
    displayed_chars = 0

    for line in logical_lines:
        # If the line is empty, it still takes one printed line
        if not line:
            used_lines += 1
            # Stop if we exceed available lines
            if used_lines >= max_lines:
                break
            continue

        # Number of sub-lines the text will occupy if it wraps
        sub_lines = math.ceil(len(line) / max_chars_per_line)

        # If we don't exceed the bounding box's vertical capacity
        if used_lines + sub_lines <= max_lines:
            # All chars fit within the bounding box
            displayed_chars += len(line)
            used_lines += sub_lines
        else:
            # Only part of this line will fit
            lines_left = max_lines - used_lines
            if lines_left <= 0:
                # No space left at all
                break

            # We can render only `lines_left` sub-lines of this line
            # That means we can render up to:
            chars_that_fit = lines_left * max_chars_per_line

            # Clip to the actual number of characters
            chars_that_fit = min(chars_that_fit, len(line))

            displayed_chars += chars_that_fit
            used_lines += lines_left  # We've used up all remaining lines
            break  # No more space in the bounding box

    return displayed_chars


def outline_estimate_num_chars(outline):
    for k, v in outline.items():
        if k == 'meta':
            continue
        if 'title' in k.lower() or 'author' in k.lower() or 'reference' in k.lower():
            continue
        if not 'subsections' in v:
            num_chars = estimate_characters(
                v['location']['width'],
                v['location']['height'],
                60, line_spacing_points=None
            )
            v['num_chars'] = num_chars
        else:
            for k_sub, v_sub in v['subsections'].items():
                if 'title' in k_sub.lower():
                    continue
                if 'path' in v_sub:
                    continue
                num_chars = estimate_characters(
                    v_sub['location']['width'],
                    v_sub['location']['height'],
                    60, line_spacing_points=None
                )
                v_sub['num_chars'] = num_chars


def generate_length_suggestions(result_json, original_section_outline, raw_section_outline):
    NOT_CHANGE = 'Do not change text.'
    original_section_outline = json.loads(original_section_outline)
    suggestion_flag = False
    new_section_outline = copy.deepcopy(result_json)
    def check_length(text, target, width, height):
        text_length = equivalent_length_with_forced_breaks(
            text,
            width,
            font_size_points=40,
        )
        if text_length - target > 100:
            return f'Text too long, shrink by {text_length - target} characters.'
        elif target - text_length > 100:
            return f'Text too short, expand by {target - text_length} characters.'
        else:
            return NOT_CHANGE

    if 'num_chars' in original_section_outline:
        new_section_outline['suggestions'] = check_length(
            result_json['description'],
            original_section_outline['num_chars'],
            raw_section_outline['location']['width'],
            raw_section_outline['location']['height']
        )
        if new_section_outline['suggestions'] != NOT_CHANGE:
            suggestion_flag = True
    if 'subsections' in original_section_outline:
        for k, v in original_section_outline['subsections'].items():
            if 'num_chars' in v:
                new_section_outline['subsections'][k]['suggestion'] = check_length(
                    result_json['subsections'][k]['description'],
                    v['num_chars'],
                    raw_section_outline['subsections'][k]['location']['width'],
                    raw_section_outline['subsections'][k]['location']['height']
                )
                if new_section_outline['subsections'][k]['suggestion'] != NOT_CHANGE:
                    suggestion_flag = True

    return new_section_outline, suggestion_flag
