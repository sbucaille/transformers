# Copyright 2025 The HuggingFace Team. All rights reserved.
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
from datasets import load_dataset
from huggingface_hub import hf_hub_download

from transformers import SuperGlueImageProcessor
from transformers.models.efficientloftr.modeling_efficientloftr import (
    EfficientLoFTRConfig,
    EfficientLoFTRForKeypointMatching,
)


DEFAULT_MODEL_REPO = "stevenbucaille/efficient_loftr_pth"
DEFAULT_FILE = "eloftr.pth"


def prepare_imgs():
    dataset = load_dataset("hf-internal-testing/image-matching-test-dataset", split="train")
    image0 = dataset[0]["image"]
    image2 = dataset[2]["image"]
    return [[image2, image0]]


def verify_model_outputs(model, device):
    images = prepare_imgs()
    preprocessor = SuperGlueImageProcessor()
    inputs = preprocessor(images=images, return_tensors="pt").to(device)
    model.to(device)
    model.eval()
    with torch.no_grad():
        outputs = model(**inputs, output_hidden_states=True, output_attentions=True)

    predicted_matches_values = outputs.matches[0, 0, 20:30]
    predicted_matching_scores_values = outputs.matching_scores[0, 0, 20:30]

    predicted_number_of_matches = torch.sum(outputs.matches[0][0] != -1).item()

    expected_number_of_matches = 383
    expected_matches_shape = torch.Size((len(images), 2, expected_number_of_matches))
    expected_matching_scores_shape = torch.Size((len(images), 2, expected_number_of_matches))

    expected_matches_values = torch.tensor([20, 21, 22, 23, 24, 25, 26, 27, 28, 29], dtype=torch.int64).to(device)
    expected_matching_scores_values = torch.tensor(
        [0.3340, 0.7391, 0.2851, 0.2058, 0.2835, 0.92265, 0.3522, 0.2954, 0.3348, 0.6370]
    ).to(device)

    assert outputs.matches.shape == expected_matches_shape
    assert outputs.matching_scores.shape == expected_matching_scores_shape

    torch.testing.assert_close(predicted_matches_values, expected_matches_values, rtol=5e-3, atol=5e-3)
    torch.testing.assert_close(predicted_matching_scores_values, expected_matching_scores_values, rtol=5e-3, atol=5e-3)

    assert predicted_number_of_matches == expected_number_of_matches


ORIGINAL_TO_CONVERTED_KEY_MAPPING = {
    r"matcher.backbone.layer(\d+).rbr_dense.conv": r"model.backbone.stages.\1.blocks.0.conv1.conv",
    r"matcher.backbone.layer(\d+).rbr_dense.bn": r"model.backbone.stages.\1.blocks.0.conv1.norm",
    r"matcher.backbone.layer(\d+).rbr_1x1.conv": r"model.backbone.stages.\1.blocks.0.conv2.conv",
    r"matcher.backbone.layer(\d+).rbr_1x1.bn": r"model.backbone.stages.\1.blocks.0.conv2.norm",
    r"matcher.backbone.layer(\d+).(\d+).rbr_dense.conv": r"model.backbone.stages.\1.blocks.\2.conv1.conv",
    r"matcher.backbone.layer(\d+).(\d+).rbr_dense.bn": r"model.backbone.stages.\1.blocks.\2.conv1.norm",
    r"matcher.backbone.layer(\d+).(\d+).rbr_1x1.conv": r"model.backbone.stages.\1.blocks.\2.conv2.conv",
    r"matcher.backbone.layer(\d+).(\d+).rbr_1x1.bn": r"model.backbone.stages.\1.blocks.\2.conv2.norm",
    r"matcher.backbone.layer(\d+).(\d+).rbr_identity": r"model.backbone.stages.\1.blocks.\2.identity",
    r"matcher.loftr_coarse.layers.(\d*[02468]).aggregate": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.self_attention.aggregation.q_aggregation",
    r"matcher.loftr_coarse.layers.(\d*[02468]).norm1": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.self_attention.aggregation.norm",
    r"matcher.loftr_coarse.layers.(\d*[02468]).q_proj": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.self_attention.attention.q_proj",
    r"matcher.loftr_coarse.layers.(\d*[02468]).k_proj": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.self_attention.attention.k_proj",
    r"matcher.loftr_coarse.layers.(\d*[02468]).v_proj": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.self_attention.attention.v_proj",
    r"matcher.loftr_coarse.layers.(\d*[02468]).merge": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.self_attention.attention.o_proj",
    r"matcher.loftr_coarse.layers.(\d*[02468]).mlp.(\d+)": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.self_attention.mlp.fc{1 if m.group(2) == '0' else 2}",
    r"matcher.loftr_coarse.layers.(\d*[02468]).norm2": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.self_attention.mlp.layer_norm",
    r"matcher.loftr_coarse.layers.(\d*[13579]).aggregate": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.cross_attention.aggregation.q_aggregation",
    r"matcher.loftr_coarse.layers.(\d*[13579]).norm1": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.cross_attention.aggregation.norm",
    r"matcher.loftr_coarse.layers.(\d*[13579]).q_proj": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.cross_attention.attention.q_proj",
    r"matcher.loftr_coarse.layers.(\d*[13579]).k_proj": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.cross_attention.attention.k_proj",
    r"matcher.loftr_coarse.layers.(\d*[13579]).v_proj": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.cross_attention.attention.v_proj",
    r"matcher.loftr_coarse.layers.(\d*[13579]).merge": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.cross_attention.attention.o_proj",
    r"matcher.loftr_coarse.layers.(\d*[13579]).mlp.(\d+)": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.cross_attention.mlp.fc{1 if m.group(2) == '0' else 2}",
    r"matcher.loftr_coarse.layers.(\d*[13579]).norm2": lambda m: f"model.local_feature_transformer.layers.{int(m.group(1)) // 2}.cross_attention.mlp.layer_norm",
    r"matcher.fine_preprocess.layer3_outconv": "refinement_layer.out_conv",
    r"matcher.fine_preprocess.layer(\d+)_outconv.weight": lambda m: f"refinement_layer.out_conv_layers.{0 if int(m.group(1)) == 2 else m.group(1)}.out_conv1.weight",
    r"matcher.fine_preprocess.layer(\d+)_outconv2\.0": lambda m: f"refinement_layer.out_conv_layers.{0 if int(m.group(1)) == 2 else m.group(1)}.out_conv2",
    r"matcher.fine_preprocess.layer(\d+)_outconv2\.1": lambda m: f"refinement_layer.out_conv_layers.{0 if int(m.group(1)) == 2 else m.group(1)}.batch_norm",
    r"matcher.fine_preprocess.layer(\d+)_outconv2\.3": lambda m: f"refinement_layer.out_conv_layers.{0 if int(m.group(1)) == 2 else m.group(1)}.out_conv3",
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


@torch.no_grad()
def write_model(
    model_path,
    model_repo,
    file_name,
    organization,
    safe_serialization=True,
    push_to_hub=False,
):
    os.makedirs(model_path, exist_ok=True)

    # ------------------------------------------------------------
    # EfficientLoFTR config
    # ------------------------------------------------------------

    config = EfficientLoFTRConfig(rope_scaling={"rope_type": "2d", "dim": 64})
    config.architectures = ["EfficientLoFTRForKeypointMatching"]
    config.save_pretrained(model_path)
    print("Model config saved successfully...")

    # ------------------------------------------------------------
    # Convert weights
    # ------------------------------------------------------------

    print(f"Fetching all parameters from the checkpoint at {model_repo}/{file_name}...")
    checkpoint_path = hf_hub_download(repo_id=model_repo, filename=file_name)
    original_state_dict = torch.load(checkpoint_path, weights_only=True, map_location="cpu")["state_dict"]

    print("Converting model...")
    all_keys = list(original_state_dict.keys())
    new_keys = convert_old_keys_to_new_keys(all_keys)

    state_dict = {}
    for key in all_keys:
        new_key = new_keys[key]
        state_dict[new_key] = original_state_dict.pop(key).contiguous().clone()

    del original_state_dict
    gc.collect()

    print("Loading the checkpoint in a EfficientLoFTR model...")
    device = "cuda"
    with torch.device(device):
        model = EfficientLoFTRForKeypointMatching(config)
    model.load_state_dict(state_dict)
    print("Checkpoint loaded successfully...")
    del model.config._name_or_path

    print("Saving the model...")
    model.save_pretrained(model_path, safe_serialization=safe_serialization)
    del state_dict, model

    # Safety check: reload the converted model
    gc.collect()
    print("Reloading the model to check if it's saved correctly.")
    model = EfficientLoFTRForKeypointMatching.from_pretrained(model_path)
    print("Model reloaded successfully.")

    model_name = "efficientloftr"
    if model_repo == DEFAULT_MODEL_REPO:
        print("Checking the model outputs...")
        verify_model_outputs(model, device)
    print("Model outputs verified successfully.")

    if push_to_hub:
        print("Pushing model to the hub...")
        model.push_to_hub(
            repo_id=f"{organization}/{model_name}",
            commit_message="Add model",
        )
        config.push_to_hub(repo_id=f"{organization}/{model_name}", commit_message="Add config")

    write_image_processor(model_path, model_name, organization, push_to_hub=push_to_hub)


def write_image_processor(save_dir, model_name, organization, push_to_hub=False):
    image_processor = SuperGlueImageProcessor()
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
        "--repo_id",
        default=DEFAULT_MODEL_REPO,
        type=str,
        help="Model repo ID of the original EfficientLoFTR checkpoint you'd like to convert.",
    )
    parser.add_argument(
        "--file_name",
        default=DEFAULT_FILE,
        type=str,
        help="File name of the original EfficientLoFTR checkpoint you'd like to convert.",
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
    parser.add_argument(
        "--organization",
        default="stevenbucaille",
        type=str,
        help="Hub organization in which you want the model to be uploaded.",
    )

    args = parser.parse_args()
    write_model(
        args.pytorch_dump_folder_path,
        args.repo_id,
        args.file_name,
        args.organization,
        safe_serialization=True,
        push_to_hub=args.push_to_hub,
    )
