from dotenv import load_dotenv
import os
import json
import copy
from io import BytesIO
import yaml
from jinja2 import Environment, StrictUndefined
import base64
from utils.llm.chat import extract_text_from_responses
from utils.core.helpers import get_json_from_response
from camel.models import ModelFactory
from camel.agents import ChatAgent
from openai import OpenAI
from utils.pptx import *
from utils.llm import *
from utils.metrics import *
from utils.styling import *
import time

load_dotenv()

IMAGE_SCALE_RATIO_MIN = 50
IMAGE_SCALE_RATIO_MAX = 40
TABLE_SCALE_RATIO_MIN = 100
TABLE_SCALE_RATIO_MAX = 80

def compute_tp(raw_content_json): 
    total_length = 0
 
    section_lengths = []
    for section in raw_content_json['sections']:
        section_len = sum(len(sub['content']) for sub in section.get('subsections', []))
        section_lengths.append(section_len)
        total_length += section_len
 
    for i, section in enumerate(raw_content_json['sections']):
        section['tp'] = section_lengths[i] / total_length if total_length > 0 else 0
        section['text_len'] = section_lengths[i]

def compute_gp(table_info, image_info):
    total_area = 0
    for k, v in table_info.items():
        total_area += v['figure_size']

    for k, v in image_info.items():
        total_area += v['figure_size']

    for k, v in table_info.items():
        v['gp'] = v['figure_size'] / total_area

    for k, v in image_info.items():
        v['gp'] = v['figure_size'] / total_area

def compress_image_bytes(img_bytes: bytes, max_side: int = 768, quality: int = 60) -> bytes:
    
    im = Image.open(BytesIO(img_bytes)).convert("RGB")
    w, h = im.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()

def hard_truncate(text: str, limit: int) -> str:
     
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # 尝试在最后一个句点/换行处截断
    m = re.search(r'(?s)^.*([。．\.!\?]\s|\n)', cut)
    return m.group(0).strip() if m else cut.strip()

def filter_image_table(args, filter_config):
    images = json.load(open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}_images.json', 'r'))
    tables = json.load(open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}_tables.json', 'r'))
    doc_json = json.load(open(f'contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_raw_content.json', 'r'))
    agent_filter = 'image_table_filter_agent'
    with open(f"prompts/pipeline/{agent_filter}.yaml", "r") as f:
        config_filter = yaml.safe_load(f)

    image_information = {}
    for k, v in images.items():
        image_information[k] = copy.deepcopy(v)
        image_information[k]['min_width'] = v['width'] // IMAGE_SCALE_RATIO_MIN
        image_information[k]['min_height'] = v['height'] // IMAGE_SCALE_RATIO_MIN
        image_information[k]['max_width'] = v['width'] // IMAGE_SCALE_RATIO_MAX
        image_information[k]['max_height'] = v['height'] // IMAGE_SCALE_RATIO_MAX

    table_information = {}
    for k, v in tables.items():
        table_information[k] = copy.deepcopy(v)
        table_information[k]['min_width'] = v['width'] // TABLE_SCALE_RATIO_MIN
        table_information[k]['min_height'] = v['height'] // TABLE_SCALE_RATIO_MIN
        table_information[k]['max_width'] = v['width'] // TABLE_SCALE_RATIO_MAX
        table_information[k]['max_height'] = v['height'] // TABLE_SCALE_RATIO_MAX

    filter_actor_sys_msg = config_filter['system_prompt']

    use_gpt5_responses = False

    if "gpt-5" in args.model_name_t.lower():  
        client = OpenAI()  
        use_gpt5_responses = True
    else: 
        if "qwen" in str(args.model_name_t).lower():
            filter_model = ModelFactory.create(
                model_platform=filter_config['model_platform'],
                model_type=filter_config['model_type'],
                model_config_dict=filter_config['model_config'],
                url=filter_config['url'],
            )
        else:
            filter_model = ModelFactory.create(
                model_platform=filter_config['model_platform'],
                model_type=filter_config['model_type'],
                model_config_dict=filter_config['model_config'],
            )

        filter_actor_agent = ChatAgent(
            system_message=filter_actor_sys_msg,
            model=filter_model,
            message_window_size=10,
        )

    filter_jinja_args = {
        'json_content': doc_json,
        'table_information': json.dumps(table_information, indent=4),
        'image_information': json.dumps(image_information, indent=4),
    }
    jinja_env = Environment(undefined=StrictUndefined)
    filter_prompt = jinja_env.from_string(config_filter["template"])
    user_prompt = filter_prompt.render(**filter_jinja_args)
     
    if use_gpt5_responses:
         
        response = client.responses.create(
            model=args.model_name_v,               
            input=user_prompt,
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"}, 
        )
        raw_text = extract_text_from_responses(response)
        input_token = getattr(getattr(response, "usage", None), "input_tokens", None)
        output_token = getattr(getattr(response, "usage", None), "output_tokens", None)
    else:
        if "qwen" in str(args.model_name_t).lower():
            response = chat_via_vllm(user_prompt,filter_config,filter_model,filter_actor_sys_msg)
            raw_text = response.choices[0].message.content
            print("raw_output by qwen : ")
            print(raw_text) 
            input_token = response.usage.prompt_tokens
            output_token = response.usage.completion_tokens
            print("input_token: ",input_token)
        else: 
            filter_actor_agent.reset()
            response = filter_actor_agent.step(user_prompt)
            input_token, output_token = account_token(response)
            raw_text = response.msgs[0].content
    
    response_json = get_json_from_response(raw_text)
    table_information = response_json['table_information']
    image_information = response_json['image_information']
    json.dump(images, open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/images_filtered.json', 'w'), indent=4)
    json.dump(tables, open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/tables_filtered.json', 'w'), indent=4)

    return input_token, output_token


import re
CAPTION_OK_PATTERN = re.compile(r'^\s*(Figure|Fig\.?)\s*\d+', re.I)
 
def fix_image_captions(args, raw_result, actor_config, filtered_image_information):
    """
    Iterate through all images. If a caption is invalid, call the LLM to regenerate it.
    Returns the updated filtered_image_information and token statistics.
    """
 
    total_in, total_out = 0, 0
    fixer_config = yaml.safe_load(open("prompts/pipeline/caption_fixer.yaml"))
    
    
    use_gpt5_responses = False

    if "gpt-5" in args.model_name_t.lower():  
        client = OpenAI()  
        use_gpt5_responses = True
    else:
    
        fixer_model = ModelFactory.create(
            model_platform=actor_config['model_platform'],
            model_type=actor_config['model_type'],
            model_config_dict=actor_config['model_config'],
            url=actor_config.get('url'),
        )
        fixer_agent = ChatAgent(
            system_message=fixer_config['system_prompt'],
            model=fixer_model,
            message_window_size=5,
        )
 
    for img_id, meta in filtered_image_information.items():
        caption = meta['caption']
        if CAPTION_OK_PATTERN.match(caption):
            print("Caption looks good.")
            continue  
        print("❌Caption Needs fixing.")
        img_path =  meta['image_path']
        with open(img_path, "rb") as f:
            img_bytes = f.read() 
        
        
        thumb_bytes = compress_image_bytes(img_bytes, max_side=768, quality=60)
        fig_b64_small = base64.b64encode(thumb_bytes).decode()

        # 2)   page_content
        
        page_content = get_page_text(raw_result, meta['page_no'])
        PAGE_LIMIT = 8000  
        page_text_small = hard_truncate(page_content or "", PAGE_LIMIT)

        jinja_env = Environment(undefined=StrictUndefined)
        tmpl = jinja_env.from_string(fixer_config["template"])
        prompt = tmpl.render(
            fig_b64=fig_b64_small,
            page_content=page_text_small,
        )
        
        if use_gpt5_responses:
            
            response = client.responses.create(
                model=args.model_name_v,               
                input=prompt,
                reasoning={"effort": "minimal"},
                text={"verbosity": "low"}, 
            )
            new_caption = extract_text_from_responses(response) 
            
            u = getattr(response, "usage", None) or {}
            in_tok  = getattr(u, "input_tokens",  getattr(u, "input_token_count", 0)) or 0
            out_tok = getattr(u, "output_tokens", getattr(u, "output_token_count", 0)) or 0
        else:
            fixer_agent.reset()
            response = fixer_agent.step(prompt)
            new_caption = response.msgs[0].content.strip() 
            in_tok, out_tok = account_token(response)
        total_in += in_tok; total_out += out_tok

        meta['caption'] = new_caption

    return filtered_image_information, total_in, total_out
 

def gen_figure_match(args, actor_config, raw_result):
    total_input_token, total_output_token = 0, 0
    agent_name = 'figure_match'
    doc_json = json.load(open(f'contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_raw_content.json', 'r'))
    filtered_table_information = json.load(open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/tables_filtered.json', 'r'))
    filtered_image_information = json.load(open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/images_filtered.json', 'r'))
 
    filtered_image_information, fix_in, fix_out = fix_image_captions(
        args, raw_result, actor_config, filtered_image_information
    ) 
    json.dump(filtered_image_information, open(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/images_filtered.json', 'w'), indent=4)

    total_input_token, total_output_token = fix_in, fix_out

    filtered_table_information_captions = {}
    filtered_image_information_captions = {}

    for k, v in filtered_table_information.items():
        filtered_table_information_captions[k] = {
            v['caption']
        }

    for k, v in filtered_image_information.items():
        filtered_image_information_captions[k] = {
            v['caption']
        }
    print("agent_name: ",agent_name)
    start_time = time.time()
    with open(f"prompts/pipeline/{agent_name}.yaml", "r") as f:
        planner_config = yaml.safe_load(f)

    compute_tp(doc_json)

    jinja_env = Environment(undefined=StrictUndefined)
    outline_template = jinja_env.from_string(planner_config["template"])
    planner_jinja_args = {
        'json_content': doc_json,
        'table_information': filtered_table_information_captions,
        'image_information': filtered_image_information_captions,
    }

    use_gpt5_responses = False
    if "gpt-5" in args.model_name_t.lower():  
        client = OpenAI()  
        use_gpt5_responses = True
    else:
        if "qwen" in str(args.model_name_t).lower():
            planner_model = ModelFactory.create(
                model_platform=actor_config['model_platform'],
                model_type=actor_config['model_type'],
                model_config_dict=actor_config['model_config'],
                url=actor_config['url'],
            )
        else:
            planner_model = ModelFactory.create(
                model_platform=actor_config['model_platform'],
                model_type=actor_config['model_type'],
                model_config_dict=actor_config['model_config'],
            )
        planner_agent = ChatAgent(
            system_message=planner_config['system_prompt'],
            model=planner_model,
            message_window_size=10,
        )
        print("planner_config['system_prompt']")
        print(planner_config['system_prompt'])
    print(f'Generating outline...')
    planner_prompt = outline_template.render(**planner_jinja_args)

    if use_gpt5_responses:
        response = client.responses.create(
            model=args.model_name_v,               
            input=planner_prompt,
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"}, 
        )
        res_result = extract_text_from_responses(response)
        
        u = getattr(response, "usage", None) or {}
        input_token  = getattr(u, "input_tokens",  getattr(u, "input_token_count", None))
        output_token = getattr(u, "output_tokens", getattr(u, "output_token_count", None)) 
    else:
        if "qwen" in str(args.model_name_t).lower():
            response = chat_via_vllm(planner_prompt,actor_config,planner_model,planner_config['system_prompt'])
            print("raw_output by qwen : ")
            res_result = response.choices[0].message.content   
            input_token = response.usage.prompt_tokens
            output_token = response.usage.completion_tokens
            print("input_token: ",input_token)
            print(res_result)
        else:
            planner_agent.reset()
            response = planner_agent.step(planner_prompt)
            res_result=response.msgs[0].content
            input_token, output_token = account_token(response)

    total_input_token += input_token
    total_output_token += output_token 
    end_time = time.time()
    time_taken = end_time - start_time
    print("time_taken:",time_taken)
    figure_arrangement = get_json_from_response(res_result)
    # Adding the caption to the figure_arrangement
    for figure in figure_arrangement["sections"]:
        for subsection in figure["subsections"]:
            if 'image1' in subsection:
                image_id = str(subsection['image1'])
                if image_id in filtered_image_information:
                    subsection['caption'] = filtered_image_information[image_id]['caption']
            if 'table1' in subsection:
                table_id = str(subsection['table1'])
                if table_id in filtered_table_information:
                    subsection['caption'] = filtered_table_information[table_id]['caption']
    figures_save_path = f"contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_figures.json" 
    os.makedirs(os.path.dirname(figures_save_path), exist_ok=True)
    with open(figures_save_path, "w") as f:
        json.dump(figure_arrangement, f, indent=4)
    print(f'Figure arrangement: {json.dumps(figure_arrangement, indent=4)}')

    arranged_images = {}
    arranged_tables = {}
    assigned_images = set()
    assigned_tables = set()
    
    for section_name, figure in figure_arrangement.items():
        if 'image' in figure:
            image_id = str(figure['image'])
            if image_id in assigned_images:
                continue
            if image_id in filtered_image_information:
                arranged_images[image_id] = filtered_image_information[image_id]
                assigned_images.add(image_id)
        if 'table' in figure:
            table_id = str(figure['table'])
            if table_id in assigned_tables:
                continue
            if table_id in filtered_table_information:
                arranged_tables[table_id] = filtered_table_information[table_id]
                assigned_tables.add(table_id)
    
    compute_gp(arranged_tables, arranged_images)

    return total_input_token, total_output_token, time_taken, figure_arrangement