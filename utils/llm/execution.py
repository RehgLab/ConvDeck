"""Code execution and agent workflow functions."""

import re
import io
import os
import contextlib
import traceback

from pptx import Presentation
from PIL import Image

from utils.pptx import *
from utils.pptx.code_templates import (
    utils_functions,
    documentation,
    add_border_label_function,
    create_id_map_function,
    save_helper_info_border_label,
    add_border_function,
    save_helper_info_border,
)
from utils.core.helpers import ppt_to_images
from utils.llm.chat import account_token


def match_response(response):
    response_text = response.msgs[0].content

    # This regular expression looks for text between ```python ... ```
    pattern = r'```python(.*?)```'
    match = re.search(pattern, response_text, flags=re.DOTALL)

    if not match:
        pattern = r'```(.*?)```'
        match = re.search(pattern, response_text, flags=re.DOTALL)

    if match:
        code_snippet = match.group(1).strip()
    else:
        # If there's no fenced code block, fallback to entire response or handle error
        code_snippet = response_text
    return code_snippet


def run_code_with_utils(code, utils_functions):
    return run_code(utils_functions + '\n' + code)


def run_code(code):
    """
    Execute Python code and capture stdout as well as the full stack trace on error.
    Forces __name__ = "__main__" so that if __name__ == "__main__": blocks will run.

    Returns:
        (output, error)
        - output: string containing everything that was printed to stdout
        - error: string containing the full traceback if an exception occurred; None otherwise
    """
    stdout_capture = io.StringIO()
    # Provide a globals dict specifying that __name__ is "__main__"
    exec_globals = {"__name__": "__main__"}

    with contextlib.redirect_stdout(stdout_capture):
        try:
            exec(code, exec_globals)
            error = None
        except Exception:
            # Capture the entire stack trace
            error = traceback.format_exc()

    output = stdout_capture.getvalue()
    return output, error


def run_code_from_agent(agent, msg, num_retries=1):
    agent.reset()
    log = []
    for attempt in range(num_retries + 1):  # +1 to include the initial attempt
        response = agent.step(msg)
        code = match_response(response)
        output, error = run_code(code)
        log.append((code, output, error))

        if error is None:
            return log

        if attempt < num_retries:
            print(f"Retrying... Attempt {attempt + 1} of {num_retries}")
            msg = error

    return log


def run_modular(all_code, file_name, with_border=True, with_label=True):
    concatenated_code = utils_functions
    concatenated_code += "\n".join(all_code.values())
    if with_border and with_label:
        concatenated_code += add_border_label_function
        concatenated_code += create_id_map_function
        concatenated_code += save_helper_info_border_label.format(file_name, file_name, file_name)
    elif with_border:
        concatenated_code += add_border_function
        concatenated_code += save_helper_info_border.format(file_name, file_name)
    else:
        concatenated_code += f'\nposter.save("{file_name}")'
    output, error = run_code(concatenated_code)
    return concatenated_code, output, error


def edit_modular(
        agent,
        edit_section_name,
        feedback,
        all_code,
        file_name,
        outline,
        content,
        images,
        actor_prompt,
        num_retries=1,
        prompt_type='initial'
    ):
    agent.reset()
    log = []
    if prompt_type == 'initial':
        msg = actor_prompt.format(
            outline['meta'],
            {edit_section_name: outline[edit_section_name]},
            content,
            images,
            documentation
        )
    elif prompt_type == 'edit':
        assert (edit_section_name == list(feedback.keys())[0])
        msg = actor_prompt.format(
            edit_section_name,
            all_code[edit_section_name],
            feedback,
            {edit_section_name: outline[edit_section_name]},
            content,
            images,
            documentation
        )
    elif prompt_type == 'new':
        assert (list(feedback.keys())[0] == 'all_good')
        msg = actor_prompt.format(
            {edit_section_name: outline[edit_section_name]},
            content,
            images,
            documentation
        )

    for attempt in range(num_retries + 1):
        response = agent.step(msg)
        new_code = match_response(response)
        all_code_changed = all_code.copy()
        all_code_changed[edit_section_name] = new_code
        concatenated_code, output, error = run_modular(all_code_changed, file_name, False, False)
        log.append({
            "code": new_code,
            "output": output,
            "error": error,
            "concatenated_code": concatenated_code
        })
        if error is None:
            return log

        if attempt < num_retries:
            print(f"Retrying... Attempt {attempt + 1} of {num_retries}")
            msg = error
            msg += '\nFix your code and try again. The poster is a single-page pptx.'
            if prompt_type != 'initial':
                msg += '\nAssume that you have had a Presentation object named "poster" and a slide named "slide".'

    return log


def fill_content(agent, prompt, num_retries, existing_code=''):
    if existing_code == '':
        existing_code = utils_functions
    agent.reset()
    log = []
    cumulative_input_token, cumulative_output_token = 0, 0
    for attempt in range(num_retries + 1):
        response = agent.step(prompt)
        input_token, output_token = account_token(response)
        cumulative_input_token += input_token
        cumulative_output_token += output_token
        new_code = match_response(response)
        all_code = existing_code + '\n' + new_code

        output, error = run_code(all_code)
        log.append({
            "code": new_code,
            "output": output,
            "error": error,
            "concatenated_code": all_code,
            'cumulative_tokens': (cumulative_input_token, cumulative_output_token)
        })

        if error is None:
            return log

        if attempt < num_retries:
            print(f"Retrying... Attempt {attempt + 1} of {num_retries}")
            prompt = error
    return log


def apply_theme(agent, prompt, num_retries, existing_code=''):
    return fill_content(agent, prompt, num_retries, existing_code)


def edit_code(agent, prompt, num_retries, existing_code=''):
    return fill_content(agent, prompt, num_retries, existing_code)


def stylize(agent, prompt, num_retries, existing_code=''):
    return fill_content(agent, prompt, num_retries, existing_code)


def gen_layout(agent, prompt, num_retries, name_to_hierarchy, visual_identifier='', existing_code=''):
    if existing_code == '':
        existing_code = utils_functions
    agent.reset()
    log = []
    cumulative_input_token, cumulative_output_token = 0, 0
    for attempt in range(num_retries + 1):
        response = agent.step(prompt)
        input_token, output_token = account_token(response)
        cumulative_input_token += input_token
        cumulative_output_token += output_token
        new_code = match_response(response)
        all_code = existing_code + '\n' + new_code

        # Save visualizations
        all_code += f'''
name_to_hierarchy = {name_to_hierarchy}
identifier = "{visual_identifier}"
get_visual_cues(name_to_hierarchy, identifier)
'''

        output, error = run_code(all_code)
        log.append({
            "code": new_code,
            "output": output,
            "error": error,
            "concatenated_code": all_code,
            'num_tokens': (input_token, output_token),
            'cumulative_tokens': (cumulative_input_token, cumulative_output_token)
        })

        if error is None:
            return log

        if attempt < num_retries:
            print(f"Retrying... Attempt {attempt + 1} of {num_retries}")
            prompt = error
    return log


def gen_layout_parallel(agent, prompt, num_retries, existing_code='', slide_width=0, slide_height=0, tmp_name='tmp'):
    if existing_code == '':
        existing_code = utils_functions

    existing_code += f'''
poster = create_poster(width_inch={slide_width}, height_inch={slide_height})
slide = add_blank_slide(poster)
save_presentation(poster, file_name="poster_{tmp_name}.pptx")
'''
    agent.reset()
    log = []
    cumulative_input_token, cumulative_output_token = 0, 0
    for attempt in range(num_retries + 1):
        response = agent.step(prompt)
        input_token, output_token = account_token(response)
        cumulative_input_token += input_token
        cumulative_output_token += output_token
        new_code = match_response(response)
        all_code = existing_code + '\n' + new_code

        output, error = run_code(all_code)
        log.append({
            "code": new_code,
            "output": output,
            "error": error,
            "concatenated_code": all_code,
            'num_tokens': (input_token, output_token),
            'cumulative_tokens': (cumulative_input_token, cumulative_output_token)
        })
        if output is None or output == '':
            prompt = 'No object name printed.'
            continue

        if error is None:
            return log

        if attempt < num_retries:
            prompt = error
    return log


def style_bullet_content(bullet_content_item, color, fill_color):
    for i in range(len(bullet_content_item)):
        bullet_content_item[i]['runs'][0]['color'] = color
        bullet_content_item[i]['runs'][0]['fill_color'] = fill_color


def get_snapshot_from_section(leaf_section, section_name, name_to_hierarchy, leaf_name, section_code, empty_paper_path='poster.pptx'):
    hierarchy = name_to_hierarchy[leaf_name]
    hierarchy_overflow_name = f'tmp/overflow_check_<{section_name}>_<{leaf_section}>_hierarchy_{hierarchy}'
    run_code_with_utils(section_code, utils_functions)
    poster = Presentation(empty_paper_path)
    # add border regardless of the hierarchy
    curr_location = add_border_hierarchy(
        poster,
        name_to_hierarchy,
        hierarchy,
        border_width=10,
    )
    if not leaf_section in curr_location:
        leaf_section = section_name
    save_presentation(poster, file_name=f"{hierarchy_overflow_name}.pptx")
    ppt_to_images(
        f"{hierarchy_overflow_name}.pptx",
        hierarchy_overflow_name,
        dpi=200
    )
    poster_image_path = os.path.join(f"{hierarchy_overflow_name}", "slide_0001.jpg")
    poster_image = Image.open(poster_image_path)

    slides_width = emu_to_inches(poster.slide_width)
    slides_height = emu_to_inches(poster.slide_height)
    locations = convert_pptx_bboxes_json_to_image_json(
        curr_location,
        slides_width,
        slides_height
    )
    zoomed_in_img = zoom_in_image_by_bbox(
        poster_image,
        locations[leaf_name],
        padding=0.01
    )
    # save the zoomed_in_img
    zoomed_in_img.save(f"{hierarchy_overflow_name}_zoomed_in.jpg")
    return curr_location, zoomed_in_img, f"{hierarchy_overflow_name}_zoomed_in.jpg"
