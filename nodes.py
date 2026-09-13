"""ComfyUI-RYH 节点实现。

- LoadImageAtFolder：从任意目录加载一张图片。
- ExtractMetadata：解析视频/图片容器中的 prompt / workflow metadata。
"""

import json
import os
import shutil
import subprocess

import numpy as np
import torch
from PIL import Image, ImageOps, ImageSequence

import folder_paths
import node_helpers

# 支持的图片扩展名（与官方 LoadImage 保持一致的常见格式）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif")

# 支持的视频扩展名（通过 ffprobe / ffmpeg 读取容器级 metadata tag）
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".wmv")


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


def resolve_image_path(folder, image):
    """把 image 输入值解析为绝对路径。

    兼容三种取值：
    1. 绝对路径 -> 原样使用；
    2. 裸文件名 -> 与 folder 拼接（节点自身下拉列表的行为）；
    3. 含目录分隔符的路径 -> 先尝试相对 folder，不存在再按相对
       ComfyUI input 根目录解析（批量工具下拉注入的是这种路径）。
    """
    if not image:
        return ""
    if os.path.isabs(image):
        return os.path.normpath(image)
    if "/" in image or os.path.sep in image:
        folder_candidate = os.path.normpath(os.path.join(folder, image))
        if os.path.isfile(folder_candidate):
            return folder_candidate
        input_candidate = os.path.normpath(
            os.path.join(folder_paths.get_input_directory(), image)
        )
        if os.path.isfile(input_candidate):
            return input_candidate
        return folder_candidate
    return os.path.normpath(os.path.join(folder, image))


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

    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "STRING")
    RETURN_NAMES = ("image", "mask", "image_path", "image_name")
    FUNCTION = "load_image"
    CATEGORY = "image/loaders"
    DESCRIPTION = "从任意目录加载一张图片，支持目录选择、◀ ▶ 快速切换与节点内预览；选择 none 时输出空张量。额外输出图片完整路径与文件名。"

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
                "",
                "",
            )

        image_path = resolve_image_path(folder, image)
        file_name = os.path.splitext(os.path.basename(image))[0]
        if not os.path.isfile(image_path):
            print(f"[LoadImageAtFolder] 图片不存在: {image_path}，输出空张量。")
            return (
                torch.zeros((0, 3, 8, 8), dtype=torch.float32),
                torch.zeros((0, 8, 8), dtype=torch.float32),
                "",
                "",
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
        return (image_tensor, mask, image_path, file_name)


def _find_ffprobe():
    """定位 ffprobe 可执行文件，找不到时返回 None。"""
    for name in ("ffprobe", "ffprobe.exe"):
        found = shutil.which(name)
        if found:
            return found
    # imageio-ffmpeg 自带的 ffmpeg 旁边通常没有 ffprobe，但仍尝试一下
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        candidate = os.path.join(os.path.dirname(get_ffmpeg_exe()), "ffprobe.exe")
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        pass
    return None


def _read_video_tags(path):
    """用 ffprobe 读取视频容器级 metadata tag，返回 dict。"""
    ffprobe = _find_ffprobe()
    if ffprobe is None:
        raise RuntimeError("未找到 ffprobe，无法解析视频 metadata，请安装 ffmpeg 并加入 PATH。")
    result = subprocess.run(
        [ffprobe, "-v", "quiet", "-show_entries", "format_tags", "-of", "json", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 解析失败: {result.stderr.strip()[:200]}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe 输出解析失败: {e}")
    return (data.get("format") or {}).get("tags") or {}


def _read_image_tags(path):
    """用 PIL 读取图片 metadata（PNG tEXt / WebP / TIFF 文本块），返回 str tag dict。"""
    with Image.open(path) as img:
        info = getattr(img, "info", {}) or {}
    return {k: v for k, v in info.items() if isinstance(v, str)}


def _pretty_json(raw):
    """把 metadata 中的 JSON 字符串格式化为易读文本；解析失败时原样返回。"""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False)
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        return raw


class ExtractMetadata:
    """解析视频或图片文件中的 ComfyUI metadata，输出 prompt 与 workflow。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "file": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "视频或图片文件的完整路径",
                        "tooltip": "待解析的文件路径（绝对路径，或相对 ComfyUI input 目录的路径），支持 mp4/webm/mkv 等视频与 png/webp 等图片",
                    },
                ),
                "save": (
                    "BOOLEAN",
                    {"default": False, "label_on": "保存 JSON", "label_off": "不保存"},
                ),
                "filename_prefix": (
                    "STRING",
                    {
                        "default": "metadata",
                        "tooltip": "保存时的文件名前缀，文件写入 ComfyUI output 目录",
                    },
                ),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("prompt", "workflow")
    FUNCTION = "extract"
    CATEGORY = "utils"
    DESCRIPTION = "读取视频（ffprobe）或图片（PIL）容器中的 metadata，提取并输出 prompt 与 workflow；可选保存为 JSON 文件。"

    def extract(self, file, save, filename_prefix):
        path = file.strip().strip('"') if file else ""
        if not path:
            raise ValueError("ExtractMetadata: 文件路径为空")
        if not os.path.isabs(path):
            candidate = os.path.normpath(os.path.join(folder_paths.get_input_directory(), path))
            if os.path.isfile(candidate):
                path = candidate
        if not os.path.isfile(path):
            raise ValueError(f"ExtractMetadata: 文件不存在: {path}")

        ext = os.path.splitext(path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            tags = _read_video_tags(path)
        elif ext in IMAGE_EXTENSIONS:
            tags = _read_image_tags(path)
        else:
            raise ValueError(f"ExtractMetadata: 不支持的文件类型: {ext or path}")
        # ffprobe / PIL 返回的 key 大小写不统一，统一小写方便查找
        tags = {str(k).lower(): v for k, v in tags.items()}

        prompt_text = _pretty_json(tags.get("prompt"))
        workflow_text = _pretty_json(tags.get("workflow"))
        if not prompt_text and not workflow_text:
            print(f"[ExtractMetadata] 文件中未找到 prompt/workflow metadata: {path}")

        if save:
            output_dir = folder_paths.get_output_directory()
            base = os.path.splitext(os.path.basename(path))[0]
            prefix = filename_prefix.strip() or "metadata"
            saved = []
            for name, content in (("prompt", prompt_text), ("workflow", workflow_text)):
                if not content:
                    continue
                out_path = os.path.join(output_dir, f"{prefix}_{base}_{name}.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(content)
                saved.append(out_path)
            for p in saved:
                print(f"[ExtractMetadata] 已保存: {p}")

        return (prompt_text, workflow_text)
