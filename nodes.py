"""ComfyUI-RYH 节点实现。

- LoadImageAtFolder：从任意目录加载一张图片。
- ExtractMetadata：解析视频/图片容器中的 prompt / workflow metadata。
- SendNotification：通过 ntfy 发送推送通知。
"""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

import torch
from PIL import Image

import folder_paths

from .media_core import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, load_image, resolve_media_path


class AnyType(str):
    """通配类型：与任意输入/输出类型兼容（同 XB-BOX / LayerStyle 的做法）。"""

    def __eq__(self, _) -> bool:
        return True

    def __ne__(self, __value: object) -> bool:
        return False


_any = AnyType("*")


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

    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "mask", "image_path", "folder_name", "file_name")
    FUNCTION = "load_image"
    CATEGORY = "image/loaders"
    DESCRIPTION = "从任意目录加载一张图片，支持目录选择、◀ ▶ 快速切换、缩略图画廊选图与节点内大图预览；选择 none 时输出空张量。额外输出图片完整路径、文件名与所选目录的最后一级文件夹名。"

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
                "",
            )

        # folder_name 从解析后的图片路径取，兼容 image 被 xyz 等外部插件
        # 直接覆盖为完整路径（此时 folder 输入并未同步变化）的情况
        image_path = resolve_media_path(folder, image)
        folder_name = os.path.basename(os.path.dirname(image_path)) if image_path else ""
        file_name = os.path.splitext(os.path.basename(image))[0]
        if not os.path.isfile(image_path):
            print(f"[LoadImageAtFolder] 图片不存在: {image_path}，输出空张量。")
            return (
                torch.zeros((0, 3, 8, 8), dtype=torch.float32),
                torch.zeros((0, 8, 8), dtype=torch.float32),
                "",
                "",
                folder_name,
            )

        image_tensor, mask = load_image(image_path)
        # 把来源路径挂到 tensor 上（仅作元数据）：下游 caption_cache 会把
        # 它记录进缓存条目的 meta，方便人工核对；不参与缓存 key 计算。
        try:
            image_tensor.ryh_source_path = image_path
        except Exception:
            pass
        return (image_tensor, mask, image_path, folder_name, file_name)


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


class SendNotification:
    """通过 ntfy 向指定订阅渠道（topic）发送推送通知。

    参考 XB-BOX 显存清理节点：可选的通配输入 + 通配透传输出，
    可串接在工作流任意位置，执行到该节点时即发送通知。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "message": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "placeholder": "要发送的通知内容",
                        "tooltip": "通知正文，支持多行文本",
                    },
                ),
                "topic": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "订阅渠道名（topic）",
                        "tooltip": "ntfy 订阅渠道名，需与手机/桌面端 App 中订阅的 topic 一致；topic 名称是公开的，建议使用不易猜测的名称",
                    },
                ),
            },
            "optional": {
                "server": (
                    "STRING",
                    {
                        "default": "https://ntfy.sh",
                        "multiline": False,
                        "tooltip": "ntfy 服务器地址，默认使用官方公共服务器；自建服务器可改为自己的地址",
                    },
                ),
                "anything": (
                    _any,
                    {"tooltip": "任意输入：把上游节点的输出连到这里，节点即可串接在工作流任意位置触发通知"},
                ),
            },
        }

    OUTPUT_NODE = True
    RETURN_TYPES = (_any,)
    RETURN_NAMES = ("pass-through",)
    FUNCTION = "send"
    CATEGORY = "utils"
    DESCRIPTION = "通过 ntfy 发送推送通知到指定订阅渠道（topic）。可选连入任意上游输出，节点会原样透传，可串接在工作流任意位置；执行到该节点时即发送通知。发送失败仅打印日志，不中断工作流。"

    def send(self, message, topic, server="https://ntfy.sh", anything=None):
        message = (message or "").strip()
        topic = (topic or "").strip().strip("/")
        server = (server or "").strip().rstrip("/")
        if not topic:
            print("[SendNotification] topic 为空，跳过发送。")
            return (anything,)
        if not message:
            print("[SendNotification] 消息为空，跳过发送。")
            return (anything,)
        if not server:
            server = "https://ntfy.sh"

        url = f"{server}/{topic}"
        try:
            req = urllib.request.Request(
                url,
                data=message.encode("utf-8"),
                method="POST",
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.getcode()
            if 200 <= status < 300:
                print(f"[SendNotification] 已发送到 {url}")
            else:
                print(f"[SendNotification] 发送异常，HTTP 状态码 {status}: {url}")
        except urllib.error.HTTPError as e:
            print(f"[SendNotification] 发送失败，HTTP {e.code} {e.reason}: {url}")
        except urllib.error.URLError as e:
            print(f"[SendNotification] 发送失败，网络错误 {e.reason}: {url}")
        except Exception as e:
            print(f"[SendNotification] 发送失败: {e}")
        return (anything,)
