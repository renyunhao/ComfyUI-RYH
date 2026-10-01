"""ComfyUI-RYH · 自定义节点包。

当前节点：
- LoadImageAtFolder：从任意目录加载一张图片（目录选择 / ◀ ▶ 切换 / 节点内等比预览）。
- ExtractMetadata：解析视频/图片中的 metadata，输出 prompt 与 workflow。
"""

import os

from aiohttp import web
from server import PromptServer

from .nodes import ExtractMetadata, LoadImageAtFolder, list_images_in_folder, resolve_image_path
from . import caption_cache

NODE_CLASS_MAPPINGS = {
    "LoadImageAtFolder": LoadImageAtFolder,
    "ExtractMetadata": ExtractMetadata,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadImageAtFolder": "Load Image At Folder (RYH)",
    "ExtractMetadata": "Extract Metadata (RYH)",
}

WEB_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")

HAS_TKINTER = False
try:
    import tkinter as tk
    from tkinter import filedialog

    HAS_TKINTER = True
except ImportError:
    pass


@PromptServer.instance.routes.post("/ryh/list_images")
async def ryh_list_images(request):
    """根据目录路径返回该目录下的图片文件名列表（前端刷新下拉选项用）。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    folder = data.get("folder", "")
    return web.json_response({"images": list_images_in_folder(folder)})


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


@PromptServer.instance.routes.post("/ryh/delete_image")
async def ryh_delete_image(request):
    """删除目录下的指定图片文件（LoadImageAtFolder 节点前端删除按钮调用）。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    folder = data.get("folder", "")
    image = data.get("image", "")
    if not image or image == "none":
        return web.json_response({"ok": False, "error": "未选择图片，无法删除。"})
    path = resolve_image_path(folder, image)
    if not path or not os.path.isfile(path):
        return web.json_response({"ok": False, "error": f"文件不存在: {path}"})
    try:
        os.remove(path)
    except OSError as e:
        return web.json_response({"ok": False, "error": f"删除失败: {e}"})
    return web.json_response({"ok": True, "path": path})


@PromptServer.instance.routes.get("/ryh/image")
async def ryh_image(request):
    """返回目录下指定图片的原始字节，供前端节点内预览。"""
    folder = request.query.get("folder", "")
    image = request.query.get("image", "")
    path = resolve_image_path(folder, image)
    if not path or not os.path.isfile(path):
        return web.Response(status=404, text="file not found")
    return web.FileResponse(path)


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


_install_caption_cache()
