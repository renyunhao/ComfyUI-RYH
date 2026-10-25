"""ComfyUI-RYH · 自定义节点包。

当前节点：
- LoadImageAtFolder：从任意目录加载一张图片（目录选择 / ◀ ▶ 切换 / 缩略图画廊 / 节点内等比预览）。
- LoadVideoAtFolder：从任意目录加载视频，输出帧序列 + 音轨 + 帧率 + 官方 VIDEO。
- LoadAudioAtFolder：从任意目录加载音频，输出官方 AUDIO 结构，支持秒级裁剪。
- BrowseMediaAtFolder：复杂选择节点。全屏选择器（目录树 / 搜索 / 虚拟滚动 / 缩略图）
  在任意根目录里挑图片、视频或音频，按类型输出 IMAGE / AUDIO / VIDEO。
- ExtractMetadata：解析视频/图片中的 metadata，输出 prompt 与 workflow。
- SendNotification：通过 ntfy 发送推送通知。
- CaptionCachePolicy：反推缓存开关，接到 comfyui-llama-TE / XB_ToolBox 反推节点
  被注入的「缓存控制」口上；不接线即不缓存。
- H3DirectorPromptSpy：抓取官方 MiniMax H3 条件节点实际收到的最终 prompt。
- H3DirectorSegment / H3DirectorSegmentList：把导演台合并后的每段提示词、裁剪画面与参考图读到图上。
- H3DirectorSegmentPromptOverride / H3DirectorSegmentListPromptOverride：改完通过导演台新增的
  h3_bus 输入口回写，一次 Queue 即生效。
- H3DirectorBusDoctor：自检补丁状态、快照内容与本次回写记录。

第三方节点补丁（不改对方源文件，默认保持原版行为）：
- Video Combine 🎥🅥🅗🅢：注入 only_save_with_audio 开关，打开后只保留带音频那份视频。
- comfyui-llama-TE / XB_ToolBox 反推节点：注入「缓存控制」口 + 懒加载（见 caption_cache）。
- MiniMaxH3 导演台：注入 h3_bus 回传口 + plan 快照（见 h3_segment_bus）。

排查崩溃时可以用环境变量 `RYH_DISABLE_PATCHES=1` 一次性关掉上面三类补丁，
让第三方节点回到原版行为做 A/B 对比（改了要重启 ComfyUI 才生效）。
"""

import asyncio
import os

import folder_paths
from aiohttp import web
from server import PromptServer

from . import media_core
from .media_nodes import BrowseMediaAtFolder, LoadAudioAtFolder, LoadVideoAtFolder
from .nodes import ExtractMetadata, LoadImageAtFolder, SendNotification
from . import caption_cache
from .caption_cache import CaptionCachePolicy
from . import vhs_save_policy
from .h3_prompt_spy import H3DirectorPromptSpy
from .h3_segment_bus import (
    H3DirectorBusDoctor,
    H3DirectorSegment,
    H3DirectorSegmentList,
    H3DirectorSegmentListPromptOverride,
    H3DirectorSegmentPromptOverride,
)

NODE_CLASS_MAPPINGS = {
    "LoadImageAtFolder": LoadImageAtFolder,
    "LoadVideoAtFolder": LoadVideoAtFolder,
    "LoadAudioAtFolder": LoadAudioAtFolder,
    "BrowseMediaAtFolder": BrowseMediaAtFolder,
    "ExtractMetadata": ExtractMetadata,
    "SendNotification": SendNotification,
    "CaptionCachePolicy": CaptionCachePolicy,
    "H3DirectorPromptSpy": H3DirectorPromptSpy,
    "H3DirectorSegment": H3DirectorSegment,
    "H3DirectorSegmentList": H3DirectorSegmentList,
    "H3DirectorSegmentPromptOverride": H3DirectorSegmentPromptOverride,
    "H3DirectorSegmentListPromptOverride": H3DirectorSegmentListPromptOverride,
    "H3DirectorBusDoctor": H3DirectorBusDoctor,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadImageAtFolder": "Load Image At Folder (RYH)",
    "LoadVideoAtFolder": "Load Video At Folder (RYH)",
    "LoadAudioAtFolder": "Load Audio At Folder (RYH)",
    "BrowseMediaAtFolder": "Browse Media At Folder (RYH)",
    "ExtractMetadata": "Extract Metadata (RYH)",
    "SendNotification": "Send Notification (RYH)",
    "CaptionCachePolicy": "Caption Cache Policy (RYH)",
    "H3DirectorPromptSpy": "H3Director Prompt Spy (RYH)",
    "H3DirectorSegment": "H3Director Segment (RYH)",
    "H3DirectorSegmentList": "H3Director Segment List (RYH)",
    "H3DirectorSegmentPromptOverride": "H3Director Segment Prompt Override (RYH)",
    "H3DirectorSegmentListPromptOverride": "H3Director Segment List Prompt Override (RYH)",
    "H3DirectorBusDoctor": "H3Director Bus Doctor (RYH)",
}

WEB_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")

HAS_TKINTER = False
try:
    import tkinter as tk
    from tkinter import filedialog

    HAS_TKINTER = True
except ImportError:
    pass


@PromptServer.instance.routes.post("/ryh/list_media")
async def ryh_list_media(request):
    """按目录 + 类型（image/video/audio）返回文件列表，带 mtime 与时长/帧数等标注信息。

    mtime 会随缩略图/播放 URL 一起回传，前端用它做缓存位，比每次塞 t=Date.now()
    少一堆穿透请求，文件被替换后也能正确失效。
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    folder = data.get("folder", "")
    kind = data.get("kind", "image")
    return web.json_response({"files": media_core.list_media(folder, kind)})


@PromptServer.instance.routes.get("/ryh/browse_media")
async def ryh_browse_media(request):
    """媒体选择器用：返回 root 下的目录树 + 当前层文件，或递归搜索结果。

    目录树/搜索会遍历整棵子树，必须丢到线程池，否则大目录会把整个
    aiohttp 事件循环卡住（进度条、websocket 一起停摆）。
    """
    root = request.query.get("root") or folder_paths.get_input_directory()
    rel_dir = request.query.get("dir", "")
    kind = request.query.get("kind", "all")
    query = (request.query.get("q") or "").strip()
    refresh = request.query.get("refresh") == "1"
    try:
        data = await asyncio.to_thread(media_core.browse_media, root, rel_dir, kind, query, refresh)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(data)


@PromptServer.instance.routes.post("/ryh/media_meta")
async def ryh_media_meta(request):
    """批量补当前可见文件的尺寸/时长/帧数；只探可见页，避免打开就扫全目录。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    paths = [p for p in data.get("paths") or [] if isinstance(p, str)]
    meta = await asyncio.to_thread(media_core.probe_many, paths)
    return web.json_response({"meta": meta})


@PromptServer.instance.routes.post("/ryh/choose_folder")
async def ryh_choose_folder(request):
    """弹出原生目录选择对话框（tkinter），返回所选目录路径。"""
    if not HAS_TKINTER:
        return web.json_response(
            {"path": "", "error": "当前环境缺少 tkinter 弹窗依赖，请直接在输入框中填写目录路径。"}
        )
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory()
        root.destroy()
        return web.json_response({"path": folder or ""})
    except Exception as e:
        return web.json_response({"path": "", "error": f"目录选择失败: {e}"})


@PromptServer.instance.routes.post("/ryh/choose_file")
async def ryh_choose_file(request):
    """弹出原生文件选择对话框（tkinter），返回所选文件路径。"""
    if not HAS_TKINTER:
        return web.json_response(
            {"path": "", "error": "当前环境缺少 tkinter 弹窗依赖，请直接在输入框中填写文件路径。"}
        )
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        filetypes = [
            ("ComfyUI metadata 文件", "*.mp4 *.webm *.mkv *.mov *.png *.webp *.jpg *.jpeg *.gif *.bmp *.tif *.tiff"),
            ("视频文件", "*.mp4 *.webm *.mkv *.mov *.m4v *.avi *.flv *.wmv"),
            ("图片文件", "*.png *.webp *.jpg *.jpeg *.gif *.bmp *.tif *.tiff"),
            ("所有文件", "*.*"),
        ]
        path = filedialog.askopenfilename(title="选择视频或图片文件", filetypes=filetypes)
        root.destroy()
        return web.json_response({"path": path or ""})
    except Exception as e:
        return web.json_response({"path": "", "error": f"文件选择失败: {e}"})


@PromptServer.instance.routes.post("/ryh/delete_file")
async def ryh_delete_file(request):
    """删除目录下的指定媒体文件（三个节点前端的 🗑 按钮共用）。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    folder = data.get("folder", "")
    name = data.get("file", "")
    if not name or name == "none":
        return web.json_response({"ok": False, "error": "未选择文件，无法删除。"})
    path = media_core.resolve_media_path(folder, name)
    if not path or not os.path.isfile(path):
        return web.json_response({"ok": False, "error": f"文件不存在: {path}"})
    if not media_core.is_allowed_media(path):
        return web.json_response({"ok": False, "error": f"不是可删除的媒体文件: {path}"})
    try:
        os.remove(path)
    except OSError as e:
        return web.json_response({"ok": False, "error": f"删除失败: {e}"})
    return web.json_response({"ok": True, "path": path})


@PromptServer.instance.routes.get("/ryh/media")
async def ryh_media(request):
    """返回目录下指定媒体的原始字节，供前端 <img>/<video>/<audio> 预览。

    FileResponse 支持 Range 请求（206），浏览器标签才能拖动进度条播放；
    folder 是用户任意指定的路径，所以必须先用扩展名白名单挡住非媒体文件。
    """
    folder = request.query.get("folder", "")
    name = request.query.get("file", "")
    path = media_core.resolve_media_path(folder, name)
    if not path or not os.path.isfile(path) or not media_core.is_allowed_media(path):
        return web.Response(status=404, text="file not found")
    return web.FileResponse(path)


@PromptServer.instance.routes.get("/ryh/thumbnail")
async def ryh_thumbnail(request):
    """返回目录下指定媒体的正方形缩略图（图片裁切 / 视频取帧 / 音频波形）。

    响应带 ETag 且支持 If-None-Match 命中 304：画廊一次可能请求上百张，
    不能让浏览器每次都重新下载，也不能靠前端拼时间戳做缓存失效（那会全量穿透）。
    """
    folder = request.query.get("folder", "")
    name = request.query.get("file", "")
    try:
        size = int(request.query.get("size", "128"))
    except ValueError:
        size = 128
    size = min(512, max(16, size))
    fit = request.query.get("fit", "cover")
    if fit not in ("cover", "contain"):
        fit = "cover"

    path = media_core.resolve_media_path(folder, name)
    if not path or not os.path.isfile(path) or not media_core.is_allowed_media(path):
        return web.Response(status=404, text="file not found")
    # 大图缩放/视频取帧是同步 CPU 活，放在线程里做，别把事件循环和目录浏览一起卡住
    data, etag = await asyncio.to_thread(media_core.build_thumbnail, path, size, fit)
    if data is None:
        return web.Response(status=404, text="not a media file")

    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if etag in (request.headers.get("If-None-Match") or "").split(","):
        return web.Response(status=304, headers=headers)
    return web.Response(body=data, content_type="image/webp", headers=headers)


__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


def _install_caption_cache():
    """在全部 custom node 加载完成后再给 comfyui-llama-TE 装缓存 patch。

    加载顺序不保证（依赖 os.listdir），因此挂到 aiohttp 的 on_startup：
    它在 init_extra_nodes 之后、服务开始前触发，此时 llama-TE 模块必然已导入。
    """
    try:
        PromptServer.instance.app.on_startup.append(_on_startup_apply_cache)
    except Exception as e:
        # 兜底：拿不到 app 时直接尝试安装（幂等，且失败不影响启动）
        print(f"[ComfyUI-RYH] on_startup 注册失败，直接尝试安装缓存: {e}")
        try:
            caption_cache.apply_patch()
        except Exception as e2:
            print(f"[ComfyUI-RYH] 反推缓存 patch 安装失败，已跳过（不影响正常使用）: {e2}")


async def _on_startup_apply_cache(_app):
    # patch 失败绝不能影响 ComfyUI 启动（例如第三方节点结构变化时）
    try:
        caption_cache.apply_patch()
    except Exception as e:
        print(f"[ComfyUI-RYH] 反推缓存 patch 安装失败，已跳过（不影响正常使用）: {e}")


async def _on_startup_apply_vhs(_app):
    try:
        vhs_save_policy.apply_patch()
    except Exception as e:
        print(f"[ComfyUI-RYH] Video Combine 开关 patch 安装失败，已跳过（不影响正常使用）: {e}")


def _install_vhs_save_policy():
    """给 VHS 的 Video Combine 注入开关，同样要等第三方节点加载完。"""
    try:
        PromptServer.instance.app.on_startup.append(_on_startup_apply_vhs)
    except Exception as e:
        print(f"[ComfyUI-RYH] on_startup 注册失败，直接尝试安装 VHS 开关: {e}")
        try:
            vhs_save_policy.apply_patch()
        except Exception as e2:
            print(f"[ComfyUI-RYH] VHS 开关 patch 安装失败，已跳过（不影响正常使用）: {e2}")


_install_caption_cache()
_install_vhs_save_policy()
