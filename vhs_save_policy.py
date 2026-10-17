"""给 Video Helper Suite 的 Video Combine 🎥🅥🅗🅢 节点加一个「只保留带音频副本」开关。

原版行为：接了 audio 输入时，先写出无声视频 `xxx_00001.mp4`，再 mux 出
`xxx_00001-audio.mp4`，两个文件都留在 output 目录里。

补丁行为：Video Combine 上多出一个 `only_save_with_audio` 开关（默认 False）。
关掉时与原版完全一致；打开时在原函数返回后删掉无声副本，并把 VHS_FILENAMES
输出与预览一并收敛到带音频那一个文件。没有接 audio 时无声副本就是唯一产物，
开关不做任何事。

实现方式是对第三方节点做 monkey-patch，不修改任何第三方源文件：
注入 widget 靠包一层 `INPUT_TYPES`，清理逻辑包在 `combine_video` 外面。
"""

import os

try:
    from .ryh_director import DISABLE_ENV, patches_disabled
except ImportError:  # 直接按目录导入（离线测试用）
    from ryh_director import DISABLE_ENV, patches_disabled

LOG_PREFIX = "[RYH VideoCombine]"

_WIDGET_NAME = "only_save_with_audio"
_WIDGET_TOOLTIP = (
    "仅在接了 audio 输入时生效：\n"
    "False（默认）= 与原版一致，无声视频和 -audio 视频都保存；\n"
    "True = 写完 -audio 视频后删掉无声那份，只保留带音频的。"
)
_IT_MARKER = "_ryh_vhs_save_policy_input"
_PATCHED_FLAG = "_ryh_vhs_save_policy_patched"


def _inject_switch(cls):
    """往 Video Combine 的 optional 里注入一个 BOOLEAN widget（默认 False）。"""
    if getattr(cls.INPUT_TYPES, _IT_MARKER, False):
        return

    import inspect

    # 取未绑定的底层函数：直接 cls.INPUT_TYPES 拿到的是已绑定 cls 的 classmethod，
    # 包装后再传 klass 会变成两个位置参数
    raw = inspect.getattr_static(cls, "INPUT_TYPES")
    original = getattr(raw, "__func__", raw)
    if not callable(original):
        raise TypeError("VideoCombine.INPUT_TYPES 不可调用，跳过注入口")

    def input_types(klass):
        spec = original(klass)
        spec.setdefault("optional", {})[_WIDGET_NAME] = (
            "BOOLEAN",
            {"default": False, "tooltip": _WIDGET_TOOLTIP},
        )
        return spec

    setattr(input_types, _IT_MARKER, True)
    cls.INPUT_TYPES = classmethod(input_types)


def _audio_path(files):
    """返回 (带音频副本, 无声副本) 路径，形态不符则返回 None。"""
    if len(files) < 2:
        return None
    audio = files[-1]
    name = os.path.basename(audio)
    if "-audio." not in name:
        return None
    silent = files[-2]
    # 带音频那份是「无声文件名 + -audio」，对不上说明倒数第二项不是它的无声母本
    silent_name = os.path.basename(silent)
    if os.path.splitext(silent_name)[0] != name.rsplit("-audio.", 1)[0]:
        return None
    return audio, silent


def _output_files(result):
    """取 VHS 的产物列表。

    返回形态是 {"result": ((save_output, [路径, ...]),)}——注意是双层元组，
    列表在 [0][1] 而不是 [1]。
    """
    try:
        files = result["result"][0][1]
    except (KeyError, IndexError, TypeError):
        return None
    return files if isinstance(files, list) else None


def _drop_silent(result):
    """删掉无声副本并把输出列表收敛到带音频那份；返回被删路径或 None。"""
    files = _output_files(result)
    found = _audio_path(files) if files else None
    if found is None:
        return None
    _, silent = found
    if os.path.isfile(silent):
        try:
            os.remove(silent)
        except OSError as e:
            print(f"{LOG_PREFIX} 无法删除无声副本: {e}", flush=True)
            return None
    else:
        silent = None
    files.pop(-2)
    preview = (result.get("ui") or {}).get("gifs")
    if preview and preview[0].get("fullpath") not in (None, files[-1]):
        preview[0]["fullpath"] = files[-1]
    return silent


def _install(cls):
    if getattr(cls, _PATCHED_FLAG, False):
        return True
    _inject_switch(cls)

    original_combine = cls.combine_video

    def combine_video(self, *args, **kwargs):
        keep_audio_only = kwargs.pop(_WIDGET_NAME, False)
        # 开关打开却什么都没清：接了 audio 说明是补丁失效（对方改了输出形态），
        # 没接 audio 则本来就只有份无声视频，属正常，不该刷屏
        had_audio = kwargs.get("audio") is not None
        result = original_combine(self, *args, **kwargs)
        # 兼容 ComfyUI 前端把 BOOLEAN 传成字符串 "true"/"false"
        if isinstance(keep_audio_only, str):
            keep_audio_only = keep_audio_only.strip().lower() in ("true", "1", "on")
        if not keep_audio_only or not isinstance(result, dict):
            return result
        removed = _drop_silent(result)
        if removed:
            print(f"{LOG_PREFIX} 已删除无声副本：{os.path.basename(removed)}", flush=True)
        elif had_audio:
            print(f"{LOG_PREFIX} 开关已打开但未找到无声副本，输出形态可能变了。", flush=True)
        return result

    cls.combine_video = combine_video
    setattr(cls, _PATCHED_FLAG, True)
    print(f"{LOG_PREFIX} 已为 Video Combine 安装 only_save_with_audio 开关。", flush=True)
    return True


def apply_patch():
    """幂等安装；未安装 VHS 时静默跳过。

    只从 sys.modules 里找已经加载好的那个 VideoCombine 类，不能 import：
    VHS 是以 `ComfyUI-VideoHelperSuite.videohelpersuite.nodes` 的名字被 ComfyUI
    导入的，另行 `import videohelpersuite.nodes` 会得到第二个模块对象，
    patch 打在没人用的那份类上。
    """
    import inspect
    import sys

    if patches_disabled():
        print(f"{LOG_PREFIX} {DISABLE_ENV} 已设置，跳过 patch（Video Combine 保持原版行为）。", flush=True)
        return False

    cls = None
    for name in list(sys.modules):
        if not name.endswith("videohelpersuite.nodes"):
            continue
        candidate = getattr(sys.modules.get(name), "VideoCombine", None)
        if inspect.isclass(candidate) and callable(getattr(candidate, "combine_video", None)):
            cls = candidate
            break
    if cls is None:
        print(f"{LOG_PREFIX} 未检测到 VideoHelperSuite，跳过。", flush=True)
        return False
    try:
        return _install(cls)
    except Exception as e:
        print(f"{LOG_PREFIX} 开关注入失败，已跳过（不影响正常使用）: {e}", flush=True)
        return False
