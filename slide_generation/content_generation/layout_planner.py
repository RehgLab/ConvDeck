from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
import yaml
from jinja2 import Environment, StrictUndefined
from utils.core.helpers import get_json_from_response, validate_slide_references
from utils.llm import *
from utils.metrics import *
from utils.styling import *
from utils.llm.chat import extract_text_from_responses
from openai import OpenAI       
from camel.models import ModelFactory          
from camel.agents import ChatAgent     
from pptx.util import Cm, Pt
import time
import shutil
 
def generate_slide_plan(args) -> Dict[str, Any]:
    paper_outline_json = f'contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_raw_content.json' 
    figures_path=f'contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_figures.json'

    raw_json = json.loads(Path(paper_outline_json).read_text(encoding="utf-8"))
    figures_json = json.loads(Path(figures_path).read_text(encoding="utf-8"))
    images = json.loads(Path(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/images_filtered.json').read_text(encoding="utf-8"))
    tables = json.loads(Path(f'<{args.model_name_t}_{args.model_name_v}>_images_and_tables/{args.paper_name}/tables_filtered.json' ).read_text(encoding="utf-8"))
    commitment_md = open(f'rst_outputs/{args.paper_name}/commitment_output_dir/commitments.md', 'r').read()
    with open(f'prompts/pipeline/layout_agent_xin_rst.yaml', "r", encoding="utf-8") as f:
        prompt_cfg = yaml.safe_load(f)
    start_time = time.time()
    use_gpt5_responses = False
    cfg = get_agent_config(args.model_name_v)
    if "gpt-5" in args.model_name_t.lower():  
        client = OpenAI()  
        use_gpt5_responses = True
    else:
        if args.model_name_t.startswith('vllm_qwen'):
            model = ModelFactory.create(
                model_platform=cfg['model_platform'],
                model_type=cfg['model_type'],
                model_config_dict=cfg['model_config'],
                url=cfg['url'],
            )
        else:
            #Invoke LLM via ChatAgent and return its *raw* assistant message (string). 
            model = ModelFactory.create(
                model_platform=cfg["model_platform"],
                model_type=cfg["model_type"],
                model_config_dict=cfg["model_config"],
                url=cfg.get("url"),
            )  
        agent = ChatAgent(
            system_message=prompt_cfg['system_prompt'],  
            model=model,
            message_window_size=5,
        ) 

    jinja_env = Environment(undefined=StrictUndefined)
    jinja_args = {
        'raw_result_json': raw_json,
        'figures_json': figures_json,
        'image_informations_json' : images,
        'table_informations_json' : tables
    } 
    jinja_args['commitment_md'] = commitment_md
    template =  jinja_env.from_string(prompt_cfg["template"]) 
    planner_prompt = template.render(**jinja_args)
    
     
    if use_gpt5_responses:
        response = client.responses.create(
            model=args.model_name_v,               
            input=planner_prompt,
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"},    
        )
        raw_text = extract_text_from_responses(response)
        in_tok = getattr(getattr(response, "usage", None), "input_tokens", None)
        out_tok = getattr(getattr(response, "usage", None), "output_tokens", None)
    elif args.model_name_t.startswith('vllm_qwen'):
        print("planner_prompt",planner_prompt)
        print("prompt_cfg['system_prompt']",prompt_cfg['system_prompt'])
        response = chat_via_vllm(planner_prompt,cfg,model,prompt_cfg['system_prompt'])
        raw_text = response.choices[0].message.content 
        print("raw_output by qwen : ")
        in_tok = response.usage.prompt_tokens
        out_tok = response.usage.completion_tokens
        print(raw_text)    
    else:
        agent.reset() 
        response = agent.step(template.render(**jinja_args)) 
        raw_text = response.msgs[0].content
        in_tok, out_tok = account_token(response)
    print(f"[layout-agent] tokens: in={in_tok} out={out_tok}")
    end_time = time.time()
    time_taken = end_time - start_time
    print("time_taken:",time_taken)
    slide_plan = get_json_from_response(raw_text)
    slide_plan_path = f'contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_slide_plan.json'

    # reference correct postpocessing
    slide_plan = validate_slide_references(slide_plan, raw_json)


    with open(slide_plan_path, 'w') as f:
        json.dump(slide_plan, f, indent=4)
    shutil.copyfile(slide_plan_path, f'contents/{args.paper_name}/<{args.model_name_t}_{args.model_name_v}>_slide_plan_wo_refined.json')
    return in_tok, out_tok,time_taken
