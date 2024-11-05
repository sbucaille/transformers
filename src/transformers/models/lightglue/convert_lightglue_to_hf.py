# Copyright 2024 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import gc
import os
import re
from typing import List

import torch

from tests.models.lightglue.test_modeling_lightglue import prepare_imgs
from transformers import (
    AutoModelForKeypointDetection,
    LightGlueForKeypointMatching,
    LightGlueImageProcessor,
)
from transformers.models.lightglue.configuration_lightglue import LightGlueConfig


def verify_model_outputs(model):
    images = prepare_imgs()
    preprocessor = LightGlueImageProcessor()
    inputs = preprocessor(images=images, return_tensors="pt").to("cuda")
    model.to("cuda")
    with torch.no_grad():
        outputs = model(**inputs, output_hidden_states=True, output_attentions=True)

    predicted_matches_values = outputs.matches[0, 0, 20:30]
    predicted_matching_scores_values = outputs.matching_scores[0, 0, 20:30]

    predicted_number_of_matches = torch.sum(outputs.matches[0][0] != -1).item()

    expected_max_number_keypoints = 866
    expected_matches_shape = torch.Size((len(images), 2, expected_max_number_keypoints))
    expected_matching_scores_shape = torch.Size((len(images), 2, expected_max_number_keypoints))

    expected_matches_values = torch.tensor([-1, -1, -1, -1, -1, -1, -1, -1, 540, -1], dtype=torch.int64).to("cuda")
    expected_matching_scores_values = torch.tensor([0, 0, 0.0167, 0.0304, 0.0328, 0, 0, 0.0095, 0.2964, 0.0352]).to(
        "cuda"
    )

    expected_number_of_matches = 127

    assert outputs.matches.shape == expected_matches_shape
    assert outputs.matching_scores.shape == expected_matching_scores_shape

    assert torch.allclose(predicted_matches_values, expected_matches_values, atol=1e-4)
    assert torch.allclose(predicted_matching_scores_values, expected_matching_scores_values, atol=1e-4)

    assert predicted_number_of_matches == expected_number_of_matches


ORIGINAL_TO_CONVERTED_KEY_MAPPING = {
    r"posenc.Wr.weight": r"positional_encoder.projector.weight",
    r"self_attn.(\d+).Wqkv.weight": r"transformer_layers.\1.self_attention_block.Wqkv.weight",
    r"self_attn.(\d+).Wqkv.bias": r"transformer_layers.\1.self_attention_block.Wqkv.bias",
    r"self_attn.(\d+).out_proj.weight": r"transformer_layers.\1.self_attention_block.output_projection.weight",
    r"self_attn.(\d+).out_proj.bias": r"transformer_layers.\1.self_attention_block.output_projection.bias",
    r"self_attn.(\d+).ffn.(\d+).weight": r"transformer_layers.\1.self_attention_block.ffn.\2.weight",
    r"self_attn.(\d+).ffn.(\d+).bias": r"transformer_layers.\1.self_attention_block.ffn.\2.bias",
    r"cross_attn.(\d+).to_qk.weight": r"transformer_layers.\1.cross_attention_block.to_qk.weight",
    r"cross_attn.(\d+).to_qk.bias": r"transformer_layers.\1.cross_attention_block.to_qk.bias",
    r"cross_attn.(\d+).to_v.weight": r"transformer_layers.\1.cross_attention_block.to_v.weight",
    r"cross_attn.(\d+).to_v.bias": r"transformer_layers.\1.cross_attention_block.to_v.bias",
    r"cross_attn.(\d+).to_out.weight": r"transformer_layers.\1.cross_attention_block.to_out.weight",
    r"cross_attn.(\d+).to_out.bias": r"transformer_layers.\1.cross_attention_block.to_out.bias",
    r"cross_attn.(\d+).ffn.(\d+).weight": r"transformer_layers.\1.cross_attention_block.ffn.\2.weight",
    r"cross_attn.(\d+).ffn.(\d+).bias": r"transformer_layers.\1.cross_attention_block.ffn.\2.bias",
    r"log_assignment.(\d+).matchability.weight": r"match_assignment_layers.\1.matchability.weight",
    r"log_assignment.(\d+).matchability.bias": r"match_assignment_layers.\1.matchability.bias",
    r"log_assignment.(\d+).final_proj.weight": r"match_assignment_layers.\1.final_projection.weight",
    r"log_assignment.(\d+).final_proj.bias": r"match_assignment_layers.\1.final_projection.bias",
    r"token_confidence.(\d+).token.0.weight": r"token_confidence.\1.token.weight",
    r"token_confidence.(\d+).token.0.bias": r"token_confidence.\1.token.bias",
}


def convert_old_keys_to_new_keys(state_dict_keys: List[str]):
    """
    This function should be applied only once, on the concatenated keys to efficiently rename using
    the key mappings.
    """
    output_dict = {}
    if state_dict_keys is not None:
        old_text = "\n".join(state_dict_keys)
        new_text = old_text
        for pattern, replacement in ORIGINAL_TO_CONVERTED_KEY_MAPPING.items():
            if replacement is None:
                new_text = re.sub(pattern, "", new_text)  # an empty line
                continue
            new_text = re.sub(pattern, replacement, new_text)
        output_dict = dict(zip(old_text.split("\n"), new_text.split("\n")))
    return output_dict


def add_keypoint_detector_state_dict(lightglue_state_dict):
    keypoint_detector = AutoModelForKeypointDetection.from_pretrained("magic-leap-community/superpoint")
    keypoint_detector_state_dict = keypoint_detector.state_dict()
    for k, v in keypoint_detector_state_dict.items():
        lightglue_state_dict[f"keypoint_detector.{k}"] = v
    return lightglue_state_dict


@torch.no_grad()
def write_model(
    model_path,
    checkpoint_url,
    safe_serialization=True,
    push_to_hub=False,
):
    os.makedirs(model_path, exist_ok=True)

    # ------------------------------------------------------------
    # SuperGlue config
    # ------------------------------------------------------------

    config = LightGlueConfig(
        descriptor_dim=256,
        num_layers=9,
        num_heads=4,
    )
    config.architectures = ["LightGlueForKeypointMatching"]
    config.save_pretrained(model_path)
    print("Model config saved successfully...")

    # ------------------------------------------------------------
    # Convert weights
    # ------------------------------------------------------------

    print(f"Fetching all parameters from the checkpoint at {checkpoint_url}...")
    original_state_dict = torch.hub.load_state_dict_from_url(checkpoint_url)

    print("Converting model...")
    all_keys = list(original_state_dict.keys())
    new_keys = convert_old_keys_to_new_keys(all_keys)

    state_dict = {}
    for key in all_keys:
        new_key = new_keys[key]
        state_dict[new_key] = original_state_dict.pop(key).contiguous().clone()

    del original_state_dict
    gc.collect()
    state_dict = add_keypoint_detector_state_dict(state_dict)

    print("Loading the checkpoint in a SuperGlue model...")
    with torch.device("cuda"):
        model = LightGlueForKeypointMatching(config)
    model.load_state_dict(state_dict, strict=False)
    print("Checkpoint loaded successfully...")
    del model.config._name_or_path

    model_name = "lightglue"
    print("Checking the model outputs...")
    verify_model_outputs(model)
    print("Model outputs verified successfully.")

    organization = "stevenbucaille"
    if push_to_hub:
        print("Pushing model to the hub...")
        model.push_to_hub(
            repo_id=f"{organization}/{model_name}",
            commit_message="Add model",
        )
        config.push_to_hub(repo_id=f"{organization}/{model_name}", commit_message="Add config")

    write_image_processor(model_path, model_name, organization, push_to_hub=push_to_hub)


def write_image_processor(save_dir, model_name, organization, push_to_hub=False):
    image_processor = LightGlueImageProcessor()
    image_processor.save_pretrained(save_dir)

    if push_to_hub:
        print("Pushing image processor to the hub...")
        image_processor.push_to_hub(
            repo_id=f"{organization}/{model_name}",
            commit_message="Add image processor",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Required parameters
    parser.add_argument(
        "--checkpoint_url",
        default="https://github.com/cvg/LightGlue/releases/download/v0.1_arxiv/superpoint_lightglue.pth",
        type=str,
        help="URL of the original LightGlue checkpoint you'd like to convert.",
    )
    parser.add_argument(
        "--pytorch_dump_folder_path",
        default=None,
        type=str,
        required=True,
        help="Path to the output PyTorch model directory.",
    )
    parser.add_argument("--save_model", action="store_true", help="Save model to local")
    parser.add_argument(
        "--push_to_hub",
        action="store_true",
        help="Push model and image preprocessor to the hub",
    )

    args = parser.parse_args()
    write_model(
        args.pytorch_dump_folder_path, args.checkpoint_url, safe_serialization=True, push_to_hub=args.push_to_hub
    )