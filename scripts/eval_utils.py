import os
import torch
import re

from ldm.data.personalized import PersonalizedBase
from evaluation.clip_eval import ImageDirEvaluator
from evaluation.vit_eval import ViTEvaluator

def init_evaluators(gpu_id):
    clip_evator = ImageDirEvaluator(f'cuda:{gpu_id}')
    dino_evator = ViTEvaluator(f'cuda:{gpu_id}')
    return clip_evator, dino_evator

# num_samples: only evaluate the last (latest) num_samples images. 
# If -1, then evaluate all images
def compare_folders(clip_evator, dino_evator, gt_dir, samples_dir, prompt, num_samples=-1, gt_self_compare=False):
    gt_data_loader     = PersonalizedBase(gt_dir,      set='evaluation', size=256, flip_p=0.0)
    if gt_self_compare:
        sample_data_loader = gt_data_loader
        # Always compare all images in the subject gt image folder.
        num_samples = -1
    else:
        sample_data_loader = PersonalizedBase(samples_dir, set='evaluation', size=256, flip_p=0.0)

    sample_start_idx = 0 if num_samples == -1 else sample_data_loader.num_images - num_samples
    sample_range     = range(sample_start_idx, sample_data_loader.num_images)
    # sample_filenames = [sample_data_loader.image_paths[i] for i in sample_range]
    # print("Sample filenames:", sample_filenames)

    gt_images = [torch.from_numpy(gt_data_loader[i]["image"]).permute(2, 0, 1) for i in range(gt_data_loader.num_images)]
    gt_images = torch.stack(gt_images, axis=0)
    sample_images = [torch.from_numpy(sample_data_loader[i]["image"]).permute(2, 0, 1) for i in sample_range]
    sample_images = torch.stack(sample_images, axis=0)

    sim_img, sim_text = clip_evator.evaluate(sample_images, gt_images, prompt)

    # huggingface's ViT pipeline requires a list of PIL images as input.
    gt_pil_images     = [ gt_data_loader[i]["image_unnorm"] for i in range(gt_data_loader.num_images) ]
    sample_pil_images = [ sample_data_loader[i]["image_unnorm"] for i in sample_range ]

    sim_dino = dino_evator.img_to_img_similarity(gt_pil_images, sample_pil_images)

    print(os.path.basename(gt_dir), "vs", os.path.basename(samples_dir))
    print(f"Image/text/dino sim: {sim_img:.3f} {sim_text:.3f} {sim_dino:.3f}")
    return sim_img, sim_text, sim_dino

def split_string(input_string):
    pattern = r'"[^"]*"|\S+'
    substrings = re.findall(pattern, input_string)
    substrings = [ s.strip('"') for s in substrings ]
    return substrings

def parse_subject_file(subject_file_path, method):
    subjects, class_tokens, broad_classes, sel_set = None, None, None, None

    with open(subject_file_path, "r") as f:
        lines = f.readlines()
        lines = [line.strip() for line in lines]
        for line in lines:
            if re.search(r"^set -[la] [a-zA-Z_]+ ", line):
                # set -l subjects  alexachung    alita...
                mat = re.search(r"^set -[la] ([a-zA-Z_]+)\s+(\S.+\S)", line)
                if mat is not None:
                    var_name = mat.group(1)
                    substrings = split_string(mat.group(2))
                    if var_name == "subjects":
                        subjects = substrings
                    elif var_name == "db_prompts" and method == "db":
                        class_tokens = substrings
                    elif var_name == "cls_tokens" and method != "db":
                        class_tokens = substrings
                    elif var_name == "broad_classes":
                        broad_classes = [ int(s) for s in substrings ]
                    elif var_name == 'sel_set':
                        sel_set = [ int(s) - 1 for s in substrings ]
                else:
                    breakpoint()

    if broad_classes is None:
        # By default, all subjects are humans/animals, unless specified in the subject file.
        broad_classes = [ 1 for _ in subjects ]

    if sel_set is None:
        sel_set = list(range(len(subjects)))

    if subjects is None or class_tokens is None:
        raise ValueError("subjects or db_prompts is None")
    
    return subjects, class_tokens, broad_classes, sel_set

# extra_sig could be a regular expression
def find_first_match(lst, search_term, extra_sig=""):
    for item in lst:
        if search_term in item and re.search(extra_sig, item):
            return item
    return None  # If no match is found

# if fix_1_offset: range_str "3-7,8,10" => [2, 3, 4, 5, 6, 7, 9]
# else:            range_str "3-7,8,10" => [3, 4, 5, 6, 7, 8, 10]
# "a-b" is always inclusive, i.e., "a-b" = [a, a+1, ..., b]
def parse_range_str(range_str, fix_1_offset=True):
    result = []
    offset = 1 if fix_1_offset else 0

    for part in range_str.split(','):
        if '-' in part:
            a, b = part.split('-')
            a, b = int(a) - offset, int(b) - offset
            result.extend(list(range(a, b + 1)))
        else:
            a = int(part) - offset
            result.append(a)
    return result

def get_promt_list(subject_name, unique_token, class_token, broad_class):
    object_prompt_list = [
    # The space between "{0} {1}" is removed, so that prompts for ada/ti could be generated by
    # providing an empty class_token. To generate prompts for DreamBooth, 
    # provide a class_token starting with a space.
    'a {0}{1} in the jungle',
    'a {0}{1} in the snow',
    'a {0}{1} on the beach',
    'a {0}{1} on a cobblestone street',
    'a {0}{1} on top of pink fabric',
    'a {0}{1} on top of a wooden floor',
    'a {0}{1} with a city in the background',
    'a {0}{1} with a mountain in the background',
    'a {0}{1} with a blue house in the background',
    'a {0}{1} on top of a purple rug in a forest',
    'a {0}{1} with a wheat field in the background',
    'a {0}{1} with a tree and autumn leaves in the background',
    'a {0}{1} with the Eiffel Tower in the background',
    'a {0}{1} floating on top of water',
    'a {0}{1} floating in an ocean of milk',
    'a {0}{1} on top of green grass with sunflowers around it',
    'a {0}{1} on top of a mirror',
    'a {0}{1} on top of the sidewalk in a crowded street',
    'a {0}{1} on top of a dirt road',
    'a {0}{1} on top of a white rug',
    'a red {0}{1}',
    'a purple {0}{1}',
    'a shiny {0}{1}',
    'a wet {0}{1}',
    'a cube shaped {0}{1}'
    ]

    animal_prompt_list = [
    'a {0}{1} in the jungle',
    'a {0}{1} in the snow',
    'a {0}{1} on the beach',
    'a {0}{1} on a cobblestone street',
    'a {0}{1} on top of pink fabric',
    'a {0}{1} on top of a wooden floor',
    'a {0}{1} with a city in the background',
    'a {0}{1} with a mountain in the background',
    'a {0}{1} with a blue house in the background',
    'a {0}{1} on top of a purple rug in a forest',
    'a {0}{1} wearing a red hat',
    'a {0}{1} wearing a santa hat',
    'a {0}{1} wearing a rainbow scarf',
    'a {0}{1} wearing a black top hat and a monocle',
    'a {0}{1} in a chef outfit',
    'a {0}{1} in a firefighter outfit',
    'a {0}{1} in a police outfit',
    'a {0}{1} wearing pink glasses',
    'a {0}{1} wearing a yellow shirt',
    'a {0}{1} in a purple wizard outfit',
    'a red {0}{1}',
    'a purple {0}{1}',
    'a shiny {0}{1}',
    'a wet {0}{1}',
    'a cube shaped {0}{1}'
    ]

    # humans/animals and cartoon characters.
    if broad_class == 1 or broad_class == 2:
        orig_prompt_list = animal_prompt_list
    else:
        # object.
        orig_prompt_list = object_prompt_list
    
    prompt_list = [ prompt.format(unique_token, class_token) for prompt in orig_prompt_list ]
    return prompt_list, orig_prompt_list
