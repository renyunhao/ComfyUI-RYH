"""ComfyUI-RYH · 自定义节点包。

当前节点：
- LoadImageAtFolder：从任意目录加载一张图片（目录选择 / ◀ ▶ 切换 / 节点内等比预览）。
"""

import os

from aiohttp import web
from server import PromptServer

from .nodes import LoadImageAtFolder, list_images_in_folder, resolve_image_path

NODE_CLASS_MAPPINGS = {
    "LoadImageAtFolder": LoadImageAtFolder,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadImageAtFolder": "Load Image At Folder (RYH)",
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
