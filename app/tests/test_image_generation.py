from __future__ import annotations

import base64

from chitti.image_generation import _request_payload


def test_image_request_owns_comfy_workflow_and_accepts_only_intent() -> None:
    payload = _request_payload(
        {
            "prompt": "cinematic cricket stadium",
            "negative_prompt": "text",
            "width": 1024,
            "height": 1024,
            "denoise": 1.0,
        },
        42,
    )
    workflow = payload["input"]["workflow"]
    assert workflow["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    assert workflow["5"]["inputs"]["width"] == 1024
    assert workflow["3"]["inputs"]["seed"] == 42
    assert "class_type" in workflow["9"]
    assert "images" not in payload["input"]


def test_reference_intent_is_translated_to_host_owned_img2img_workflow() -> None:
    payload = _request_payload(
        {
            "prompt": "cinematic lighting",
            "negative_prompt": "",
            "width": 1024,
            "height": 1024,
            "denoise": 0.35,
        },
        7,
        b"\x89PNG\r\n\x1a\n" + b"\0" * 16,
    )
    workflow = payload["input"]["workflow"]
    assert workflow["1"]["class_type"] == "LoadImage"
    assert workflow["5"]["class_type"] == "VAEEncode"
    assert payload["input"]["images"][0]["name"] == "reference.png"
    assert base64.b64decode(
        payload["input"]["images"][0]["image"].split(",", 1)[1]
    ).startswith(b"\x89PNG")
