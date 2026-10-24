"""从任意目录加载视频 / 音频的节点，交互与 LoadImageAtFolder 一致。

官方 LoadVideo / LoadAudio 只认 ComfyUI 的 input 目录，这两个节点把目录开放成
任意路径：列举、缩略图、解码都由 media_core 提供，前端扩展负责画廊与播放。

BrowseMediaAtFolder 是配套的复杂选择节点：全屏选择器在任意根目录里按目录树
浏览、搜索，按扩展名输出 IMAGE / AUDIO / VIDEO；不做解码参数，视频用
InputImpl.VideoFromFile 直接引用文件。
"""

import os
from fractions import Fraction

import torch
from comfy_api.latest import InputImpl, VideoComponents

import folder_paths

from . import media_core

_any_empty_image = torch.zeros((0, 3, 8, 8), dtype=torch.float32)


def _folder_name_and_file_name(folder, media, media_path):
    # folder_name 从解析后的路径取，兼容 xyz 等外部插件直接把输入值覆盖成
    # 完整路径（此时 folder 输入并未同步变化）的情况
    folder_name = os.path.basename(os.path.dirname(media_path)) if media_path else ""
    file_name = os.path.splitext(os.path.basename(media))[0] if media and media != "none" else ""
    return folder_name, file_name


class LoadVideoAtFolder:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder": (
                    "STRING",
                    {
                        "default": folder_paths.get_input_directory(),
                        "multiline": False,
                        "placeholder": "视频所在目录的完整路径",
                        "tooltip": "视频所在目录的完整路径，可点击节点上的 [📁 目录] 按钮弹窗选择",
                    },
                ),
                "video": (
                    ["none"],
                    {
                        "default": "none",
                        "tooltip": "从下拉列表或缩略图画廊选一个视频；选择 none 表示不加载",
                    },
                ),
                "fps_limit": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 240,
                        "tooltip": "0=按原帧率逐帧解码；大于 0 时按该帧率抽帧（高于原帧率不生效）。数值越大解码越久、占内存越多",
                    },
                ),
                "max_frames": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 100000,
                        "tooltip": "最多输出多少帧，0=不限制。达到上限就停止解码",
                    },
                ),
                "max_side": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 8192,
                        "tooltip": "0=原始分辨率；大于 0 时按长边等比缩小到该像素，避免几百帧 1080p 把内存吃满",
                    },
                ),
            }
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "FLOAT", "VIDEO", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("images", "audio", "frame_rate", "video", "video_path", "folder_name", "file_name")
    FUNCTION = "load_video"
    CATEGORY = "video/loaders"
    DESCRIPTION = "从任意目录加载一个视频，输出帧序列 IMAGE [T,H,W,C]、对应音轨 AUDIO、帧率与官方 VIDEO 对象；支持目录选择、缩略图画廊选帧、抽帧/限帧/缩放控制。音轨长度会截到与输出画面等长，保证音画对齐。选择 none 时输出空。"

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        # 下拉选项由前端按目录动态生成，跳过默认校验
        return True

    def load_video(self, folder, video, fps_limit, max_frames, max_side):
        if not folder or not video or video == "none":
            print("[LoadVideoAtFolder] 未选择视频（none），输出空。")
            return (_any_empty_image, None, 0.0, None, "", "", "")

        media_path = media_core.resolve_media_path(folder, video)
        folder_name, file_name = _folder_name_and_file_name(folder, video, media_path)
        if not os.path.isfile(media_path) or not media_core.is_allowed_media(media_path):
            print(f"[LoadVideoAtFolder] 视频不可用: {media_path}，输出空。")
            return (_any_empty_image, None, 0.0, None, "", folder_name, file_name)

        images, frame_rate, audio = media_core.load_video(media_path, fps_limit, max_frames, max_side)
        if images is None:
            print(f"[LoadVideoAtFolder] 解不出视频帧: {media_path}，输出空。")
            return (_any_empty_image, None, 0.0, None, "", folder_name, file_name)

        components = VideoComponents(
            images=images,
            frame_rate=Fraction(frame_rate).limit_denominator(100000),
            audio=audio,
        )
        video_source = InputImpl.VideoFromComponents(components)
        return (images, audio, float(frame_rate), video_source, media_path, folder_name, file_name)


class LoadAudioAtFolder:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder": (
                    "STRING",
                    {
                        "default": folder_paths.get_input_directory(),
                        "multiline": False,
                        "placeholder": "音频所在目录的完整路径",
                        "tooltip": "音频所在目录的完整路径，可点击节点上的 [📁 目录] 按钮弹窗选择",
                    },
                ),
                "audio": (
                    ["none"],
                    {
                        "default": "none",
                        "tooltip": "从下拉列表或缩略图画廊选一个音频；选择 none 表示不加载",
                    },
                ),
                "start_time": (
                    "FLOAT",
                    {
                        "default": 0.0,
                        "min": 0.0,
                        "max": 100000.0,
                        "step": 0.01,
                        "tooltip": "从第几秒开始取，0=从头",
                    },
                ),
                "duration": (
                    "FLOAT",
                    {
                        "default": 0.0,
                        "min": 0.0,
                        "max": 100000.0,
                        "step": 0.01,
                        "tooltip": "取多少秒，0=取到结尾",
                    },
                ),
            }
        }

    RETURN_TYPES = ("AUDIO", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("audio", "audio_path", "folder_name", "file_name")
    FUNCTION = "load_audio"
    CATEGORY = "audio/loaders"
    DESCRIPTION = "从任意目录加载一个音频，按 ComfyUI 官方 AUDIO 结构输出（waveform [1,C,L] + sample_rate），支持目录选择、波形缩略图画廊与秒级裁剪。选择 none 时输出空。"

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        return True

    def load_audio(self, folder, audio, start_time, duration):
        if not folder or not audio or audio == "none":
            print("[LoadAudioAtFolder] 未选择音频（none），输出空。")
            return (None, "", "", "")

        media_path = media_core.resolve_media_path(folder, audio)
        folder_name, file_name = _folder_name_and_file_name(folder, audio, media_path)
        if not os.path.isfile(media_path) or not media_core.is_allowed_media(media_path):
            print(f"[LoadAudioAtFolder] 音频不可用: {media_path}，输出空。")
            return (None, "", folder_name, file_name)

        loaded = media_core.load_audio(media_path, start_time, duration)
        if loaded is None:
            print(f"[LoadAudioAtFolder] 没有可用音轨: {media_path}，输出空。")
            return (None, "", folder_name, file_name)
        return (loaded, media_path, folder_name, file_name)


class BrowseMediaAtFolder:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder": (
                    "STRING",
                    {
                        "default": folder_paths.get_input_directory(),
                        "multiline": False,
                        "placeholder": "浏览根目录的完整路径",
                        "tooltip": "选择器从这个目录开始向下浏览，可点节点上的 [📁 根目录] 按钮更换",
                    },
                ),
                "kind": (
                    ["all", "image", "video", "audio"],
                    {
                        "default": "all",
                        "tooltip": "选择器过滤的类型；all 同时列出图片、视频和音频",
                    },
                ),
                "file": (
                    ["none"],
                    {
                        "default": "none",
                        "tooltip": "相对根目录的文件路径，点 [🗂 浏览] 在全屏选择器里挑选",
                    },
                ),
            }
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "VIDEO", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "audio", "video", "path", "folder_name", "file_name", "media_kind")
    FUNCTION = "load_media"
    CATEGORY = "RYH/media"
    DESCRIPTION = "复杂媒体选择节点：目录树 + 搜索 + 虚拟滚动的全屏选择器，在任意根目录里挑图片/视频/音频，按文件类型只填对应的一路输出（IMAGE / AUDIO / VIDEO），另附完整路径、目录名、文件名与媒体类型。视频不抽帧，直接输出官方 VIDEO 引用。"

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        # file 下拉由前端选择器动态写入，跳过默认校验
        return True

    def load_media(self, folder, kind, file):
        if not folder or not file or file == "none":
            print("[BrowseMediaAtFolder] 未选择文件（none），输出空。")
            return (_any_empty_image, None, None, "", "", "", "")

        media_path = media_core.resolve_media_path(folder, file)
        if not media_path or not os.path.isfile(media_path) or not media_core.is_allowed_media(media_path):
            print(f"[BrowseMediaAtFolder] 文件不可用: {media_path or file}，输出空。")
            return (_any_empty_image, None, None, media_path or "", "", "", "")

        media_kind = media_core.media_kind(media_path)
        folder_name = os.path.basename(os.path.dirname(media_path))
        file_name = os.path.splitext(os.path.basename(media_path))[0]

        image = None
        audio = None
        video = None
        if media_kind == "image":
            image, _mask = media_core.load_image(media_path)
        elif media_kind == "video":
            video = InputImpl.VideoFromFile(media_path)
        elif media_kind == "audio":
            audio = media_core.load_audio(media_path, 0.0, 0.0)

        return (
            image if image is not None else _any_empty_image,
            audio,
            video,
            media_path,
            folder_name,
            file_name,
            media_kind,
        )
