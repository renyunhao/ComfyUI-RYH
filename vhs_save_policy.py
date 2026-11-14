"""给 Video Helper Suite 的 Video Combine 🎥🅥🅗🅢 节点加「保存哪份视频」选项。

原版行为：接了 audio 输入时，先写出无声视频 `xxx_00001.mp4`，再 mux 出
`xxx_00001-audio.mp4`，两个文件都留在 output 目录里，输出列表末尾是
[... , 无声视频, -audio 视频]。

补丁行为：Video Combine 的 optional 里多出一个 `save_audio_mode` 下拉：
- 都保存（默认）：与原版完全一致，两份视频都留着；
- 有音频：删掉无声副本，只留 -audio 那份（原布尔开关 True 的行为）；
- 无音频：删掉 -audio 副本，只留无声那份。
两种单选模式都会同步收敛 VHS_FILENAMES 输出与节点预览，避免预览指向被删掉的文件。
没接 audio 时只有一份无声视频，选项不做任何事。

旧版补丁用的是布尔开关 `only_save_with_audio`；这里后端仍兼容该参数
（老 prompt 直传时映射成「有音频」），前端由 js/vhs_save_policy.js 负责把
旧工作流里保存的布尔值迁移到新下拉。任何认不出的值都回退到「都保存」，
绝不因为解析失败而误删文件。

实现方式是对第三方节点做 monkey-patch，不修改任何第三方源文件：
注入 widget 靠包一层 `INPUT_TYPES`，清理逻辑包在 `combine_video` 外面。
"""

import os

try:
    from .ryh_director import DISABLE_ENV, patches_disabled
except ImportError:  # 直接按目录导入（离线测试用）
    from ryh_director import DISABLE_ENV, patches_disabled

LOG_PREFIX = "[RYH VideoCombine]"

_WIDGET_NAME = "save_audio_mode"
_LEGACY_WIDGET_NAME = "only_save_with_audio"
MODE_ALL = "都保存"
MODE_NO_AUDIO = "无音频"
MODE_WITH_AUDIO = "有音频"
_MODES = [MODE_ALL, MODE_NO_AUDIO, MODE_WITH_AUDIO]
_WIDGET_TOOLTIP = (
    "仅在接了 audio 输入时生效：\n"
    "都保存（默认）= 与原版一致，无声视频和 -audio 视频都保留；\n"
    "有音频 = 只保留 -audio 视频，删除无声副本；\n"
    "无音频 = 只保留无声视频，删除 -audio 副本。\n"
    "VHS_FILENAMES 输出与节点预览会同步指向保留的那份。"
)
_IT_MARKER = "_ryh_vhs_save_policy_input"
_PATCHED_FLAG = "_ryh_vhs_save_policy_patched"


def _normalize_mode(value):
    """把前端传来的值收敛到三个选项，兼容旧布尔开关与常见英文别名。"""
    if value is None:
        return MODE_ALL
    if isinstance(value, bool):
        return MODE_WITH_AUDIO if value else MODE_ALL
    if isinstance(value, str):
        v = value.strip().lower()
        if v in (MODE_ALL, "all", "both", "keep all", "全部", ""):
            return MODE_ALL
        if v in (MODE_NO_AUDIO, "no audio", "no_audio", "without audio", "silent", "mute", "无声", "不带音频"):
            return MODE_NO_AUDIO
        if v in (MODE_WITH_AUDIO, "with audio", "with_audio", "audio", "audio only", "only audio", "带音频"):
            return MODE_WITH_AUDIO
        # 老 API / 老前端可能把布尔开关传成字符串
        if v in ("true", "1", "on", "yes"):
            return MODE_WITH_AUDIO
        if v in ("false", "0", "off", "no"):
            return MODE_ALL
    return MODE_ALL  # 未知值一律退回原版行为


def _inject_widget(cls):
    """往 Video Combine 的 optional 里注入一个 combo widget（默认「都保存」）。"""
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
        optional = spec.setdefault("optional", {})
        optional.pop(_LEGACY_WIDGET_NAME, None)  # 万一是热重载到旧版定义，别留两个控件
        optional[_WIDGET_NAME] = (
            list(_MODES),
            {"default": MODE_ALL, "tooltip": _WIDGET_TOOLTIP},
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


def _preview(result):
    """拿节点预览 dict；VHS 在 batched 未完成时没有 gifs。"""
    gifs = (result.get("ui") or {}).get("gifs")
    if isinstance(gifs, list) and gifs and isinstance(gifs[0], dict):
        return gifs[0]
    return None


def _keep_only(result, keep_audio):
    """删掉不需要的那份，把输出列表与预览收敛到保留的那份。

    返回 True 表示识别到成对产物并做出处理（含明确提示过的放弃分支），
    False 表示没找到成对产物或删除失败，交由调用方决定是否提示。
    """
    files = _output_files(result)
    found = _audio_path(files) if files else None
    if found is None:
        return False
    audio, silent = found
    keep, drop = (audio, silent) if keep_audio else (silent, audio)
    label = "无声副本" if keep_audio else "带音频副本"

    # 选「无音频」时若无声母本已被 VHS 自己删掉（workflow 里开了
    # VHS_KeepIntermediate=False），再删 audio 就一份视频都不剩，直接放弃清理。
    if not keep_audio and not os.path.isfile(silent):
        print(
            f"{LOG_PREFIX} 选择「{MODE_NO_AUDIO}」但无声副本已不存在（检查 workflow 的 "
            "VHS_KeepIntermediate 设置），保留带音频副本。",
            flush=True,
        )
        return True

    if os.path.isfile(drop):
        try:
            os.remove(drop)
        except OSError as e:
            print(f"{LOG_PREFIX} 无法删除{label} {os.path.basename(drop)}: {e}", flush=True)
            return False
        print(f"{LOG_PREFIX} [{MODE_WITH_AUDIO if keep_audio else MODE_NO_AUDIO}] "
              f"已删除{label}：{os.path.basename(drop)}", flush=True)

    # audio 恒为最后一项、silent 恒为倒数第二项；收敛后 files[-1] 就是保留的那份
    files.pop(-2 if keep_audio else -1)
    preview = _preview(result)
    if preview is not None:
        preview["filename"] = os.path.basename(keep)
        preview["fullpath"] = keep
    return True


def _install(cls):
    if getattr(cls, _PATCHED_FLAG, False):
        return True
    _inject_widget(cls)

    original_combine = cls.combine_video

    def combine_video(self, *args, **kwargs):
        raw_mode = kwargs.pop(_WIDGET_NAME, None)
        # 旧布尔开关已不在 INPUT_TYPES 里，但老 prompt 仍可能带上；必须吃掉，
        # 免得它被当成未知参数传进原函数。
        legacy = kwargs.pop(_LEGACY_WIDGET_NAME, None)
        mode = _normalize_mode(raw_mode)
        if raw_mode is None and legacy is not None:
            mode = _normalize_mode(legacy)  # True -> 有音频，False -> 都保存
        # 开关生效却什么都没清：接了 audio 说明是补丁失效（对方改了输出形态），
        # 没接 audio 则本来就只有份无声视频，属正常，不该刷屏
        had_audio = kwargs.get("audio") is not None
        result = original_combine(self, *args, **kwargs)
        if mode == MODE_ALL or not isinstance(result, dict):
            return result
        if not _keep_only(result, mode == MODE_WITH_AUDIO) and had_audio:
            print(
                f"{LOG_PREFIX} 已选择「{mode}」但未找到成对的视频产物（可能没接有效 audio、"
                "当前格式不带音轨，或 VHS 改了输出形态），本次不清理。",
                flush=True,
            )
        return result

    cls.combine_video = combine_video
    setattr(cls, _PATCHED_FLAG, True)
    print(f"{LOG_PREFIX} 已为 Video Combine 安装 save_audio_mode 选项"
          "（都保存 / 无音频 / 有音频）。", flush=True)
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
        print(f"{LOG_PREFIX} 选项注入失败，已跳过（不影响正常使用）: {e}", flush=True)
        return False
