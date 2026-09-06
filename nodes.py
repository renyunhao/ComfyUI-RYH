"""LoadImageAtFolder 节点：从任意目录加载一张图片。"""

import os

import numpy as np
import torch
from PIL import Image, ImageOps, ImageSequence

import folder_paths
import node_helpers

# 支持的图片扩展名（与官方 LoadImage 保持一致的常见格式）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif")


def list_images_in_folder(folder):
    """返回目录下按文件名排序的图片列表（仅文件名）。目录无效时返回空列表。"""
    if not folder or not os.path.isdir(folder):
        return []
    try:
        names = []
        for name in os.listdir(folder):
            full_path = os.path.join(folder, name)
            if os.path.isfile(full_path) and os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
                names.append(name)
    except OSError:
        return []
    return sorted(names, key=str.lower)


class LoadImageAtFolder:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder": (
                    "STRING",
                    {
                        "default": folder_paths.get_input_directory(),
                        "multiline": False,
                        "placeholder": "图片所在目录的完整路径",
                        "tooltip": "图片所在目录的完整路径，可点击节点上的 [📁 目录] 按钮弹窗选择",
                    },
                ),
                "image": (
                    ["none"],
                    {
                        "default": "none",
                        "tooltip": "从下拉列表选择一张图片；选择 none 表示不加载任何图片",
                    },
                ),
            }
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    FUNCTION = "load_image"
    CATEGORY = "image/loaders"
    DESCRIPTION = "从任意目录加载一张图片，支持目录选择、◀ ▶ 快速切换与节点内预览；选择 none 时输出空张量。"

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        # 图片下拉选项是前端根据目录动态生成的，跳过默认校验
        return True

    def load_image(self, folder, image):
        if not folder or not image or image == "none":
            print("[LoadImageAtFolder] 未选择图片（none），输出空张量。")
            return (
                torch.zeros((0, 3, 8, 8), dtype=torch.float32),
                torch.zeros((0, 8, 8), dtype=torch.float32),
            )

        image_path = os.path.join(folder, image)
        if not os.path.isfile(image_path):
            print(f"[LoadImageAtFolder] 图片不存在: {image_path}，输出空张量。")
            return (
                torch.zeros((0, 3, 8, 8), dtype=torch.float32),
                torch.zeros((0, 8, 8), dtype=torch.float32),
            )

        img = node_helpers.pillow(Image.open, image_path)
        # 只取第一帧（多帧 GIF/webp 仅加载首帧）
        frame = next(iter(ImageSequence.Iterator(img)))
        frame = node_helpers.pillow(ImageOps.exif_transpose, frame)

        image = frame.convert("RGB")
        image_tensor = torch.from_numpy(np.array(image).astype(np.float32) / 255.0)[None,]

        if "A" in frame.getbands():
            mask = np.array(frame.getchannel("A")).astype(np.float32) / 255.0
            mask = 1.0 - torch.from_numpy(mask)
        else:
            mask = torch.zeros((64, 64), dtype=torch.float32)

        img.close()
        return (image_tensor, mask)
