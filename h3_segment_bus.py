"""H3 Segment Bus — 把导演台合并后的每段提示词与参考素材暴露到图上，改完再回传。

导演台每次 Queue 都会走 prepare_director_plan（nodes/director_common.py:299）建出
DirectorPlan；plan.segments[i] 里的 prompt / refs / ref_audios / ref_videos /
source_clip 已经是「全局 ∪ 本段」合并后的最终值（plan.py:884-903）。本模块在那一
刻把 plan 快照下来，并给导演台注入一个可选输入口 h3_bus 作为回传通道。

回传发生在建 plan 之后、采样之前，所以段间引导、二采、缓存指纹全部照常工作。
快照会常驻内存直到被下一次运行覆盖：全帧缓存下每段约 0.6GB（124×480×864 float）。
"""

from __future__ import annotations

import collections
import datetime
import inspect
import logging

import torch

try:
    from . import ryh_director as ref
except ImportError:  # 直接按目录导入（离线测试用）
    import ryh_director as ref

log = logging.getLogger("RYH.h3_segment_bus")
if not log.handlers:
    log.setLevel(logging.INFO)

PKG = "ComfyUI_MiniMaxH3_Director"
DIRECTOR_MODULE = f"{PKG}.nodes.director"
DIRECTOR_CLASS = "MiniMaxH3Director"
PLAN_FUNC = "prepare_director_plan"
BUS_TYPE = "RYH_H3_BUS"
BUS_INPUT = "h3_bus"
MARKER = "_ryh_segment_bus"
MAX_SNAPSHOTS = 2

# node_id -> snapshot
SNAPSHOTS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
# 读节点从本次 prompt 直接建出来的 plan，供导演台复用（一次生效的关键）
PENDING: dict = {}
_RUN: dict = {"bus": None, "node_id": None}

PLAN_SCALARS = ("timeline_data", "task_type", "global_prompt", "total_frames",
                "frame_rate", "width", "height", "ref_max_size")
PLAN_OBJECTS = ("i2v_groups", "r2v_groups", "selflift", "semantic_bridge",
                "refine", "face_refine")


def _is_link(value) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and isinstance(value[0], (int, str)) and isinstance(value[1], int))


def _plan_key(values: dict) -> tuple:
    return tuple(str(values.get(name, "")) for name in PLAN_SCALARS)


def director_node_ids(dynprompt) -> list:
    prompt = dynprompt.get_original_prompt() if dynprompt is not None else {}
    return [nid for nid, info in (prompt or {}).items()
            if isinstance(info, dict) and info.get("class_type") == DIRECTOR_CLASS]


def live_plan(dynprompt, selector: str = ""):
    """从本次 Queue 的 prompt 里直接建 DirectorPlan —— 所以一次 Queue 就生效。

    返回 (plan, node_id, note)；拿不到就返回 (None, None, 原因)，调用方回退到快照。
    """
    if dynprompt is None:
        return None, None, "无 dynprompt（离线调用）"
    ids = director_node_ids(dynprompt)
    if not ids:
        return None, None, "本次 prompt 里没有 MiniMaxH3Director 节点"
    want = str(selector or "").strip()
    if want:
        if want not in ids:
            return None, None, f"指定的导演台节点 {want} 不在本图里（可选：{ids}）"
        node_id = want
    elif len(ids) == 1:
        node_id = ids[0]
    else:
        return None, None, f"图里有 {len(ids)} 台导演台，请在 director_node 里填其中一个 id：{ids}"
    inputs = (dynprompt.get_node(node_id) or {}).get("inputs") or {}
    unusable = [name for name in PLAN_SCALARS if _is_link(inputs.get(name)) or name not in inputs]
    if unusable:
        return None, None, f"导演台 {node_id} 这些输入是连线值/缺失，无法离线解析：{unusable}"
    values = {name: inputs[name] for name in PLAN_SCALARS}
    common = ref.find("nodes.director_common")
    if common is None:
        return None, None, "找不到已加载的 nodes.director_common"
    try:
        plan = getattr(common, PLAN_FUNC)(unique_id=node_id, **values)
    except Exception as error:
        log.warning("H3 Segment Bus: 现场建 plan 失败 (%s)", error)
        return None, None, f"现场建 plan 失败：{error}"
    # 导演台接了 selflift/refine/外部组等图对象时不能复用那份 plan ——
    # 这些附件只有在导演台自己执行时才拿得到，复用得丢配置。
    attached = [name for name in PLAN_OBJECTS if _is_link(inputs.get(name))]
    if not attached:
        PENDING[str(node_id)] = {"plan": plan, "key": _plan_key(values)}
    return plan, node_id, ("现场建 plan（节点 %s，复用=%s）"
                           % (node_id, "否，已接 " + str(attached) if attached else "是"))


def resolve_plan(dynprompt, selector: str):
    """先现场建 plan，失败再退回上一次 Queue 的快照。"""
    plan, node_id, note = live_plan(dynprompt, selector)
    if plan is not None:
        return plan, note
    snap = get_snapshot()
    if snap is not None:
        return snap["plan"], f"{note} -> 回退到上次 Queue 的快照"
    return None, note


_RUN_FINGERPRINT = ""


def capture_fingerprint(prompt=None) -> None:
    """在缓存判定之前，记录本次 prompt 里所有导演台节点的 plan 输入指纹。

    ComfyUI 判断节点是否要重跑用的是 IS_CHANGED，而 IS_CHANGED 拿不到 dynprompt
    （execution.py 的 IsChangedCache 调 get_input_data 时不传），所以只能在这里
    从 PromptExecutor.execute 的入参里抓 —— 运行钩子正好早于缓存判定。
    """
    global _RUN_FINGERPRINT
    import hashlib

    parts = []
    for nid, info in (prompt or {}).items():
        if not isinstance(info, dict) or info.get("class_type") != DIRECTOR_CLASS:
            continue
        inputs = info.get("inputs") or {}
        parts.append((str(nid),) + tuple(str(inputs.get(k, "")) for k in PLAN_SCALARS))
    _RUN_FINGERPRINT = hashlib.md5(repr(sorted(parts)).encode("utf-8")).hexdigest()[:16] if parts else ""


def _tensor_to_cpu(obj):
    tensor = getattr(obj, "detach", None)
    return tensor().cpu() if callable(tensor) else obj


def _snapshot(plan, node_id, notes, bus_seen=False, override_count=0) -> None:
    key = str(node_id or "last")
    SNAPSHOTS[key] = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "plan": plan,
        "count": len(getattr(plan, "segments", []) or []),
        "notes": notes or [],
        "bus_seen": bus_seen,
        "override_count": override_count,
    }
    while len(SNAPSHOTS) > MAX_SNAPSHOTS:
        SNAPSHOTS.popitem(last=False)


def _apply_overrides(plan, bus) -> list[str]:
    """把 override 节点攒的 {段序: 新提示词} 写进合并后的 plan。"""
    overrides = (bus or {}).get("overrides") or {}
    notes: list[str] = []
    segments = getattr(plan, "segments", None) or []
    for seg in segments:
        idx = int(getattr(seg, "index", 0))
        text = overrides.get(idx, overrides.get(-1))
        if text is None:
            continue
        text = str(text)
        if text == getattr(seg, "prompt", ""):
            notes.append(f"段 {idx}: 文本与上轮相同（未变）")
            continue
        notes.append(f"段 {idx}: {len(seg.prompt or '')}→{len(text)} chars")
        seg.prompt = text
    for key in overrides:
        if int(key) == -1:
            continue
        if not any(int(getattr(s, "index", -99)) == int(key) for s in segments):
            notes.append(f"警告: override 段号 {key} 不存在（共 {len(segments)} 段）")
    return notes


def get_snapshot():
    """最近一次 Queue 的快照（现场建 plan 失败时的兜底数据源）。"""
    return next(reversed(SNAPSHOTS.values())) if SNAPSHOTS else None


def _segment_frames(plan, seg):
    """该段裁剪后的画面 —— 导演台自己的 resolve_segment_raw_clip，取不到就返回 None。"""
    runtime = ref.find("director.segment_runtime")
    if runtime is None:
        log.warning("H3 Segment Bus: segment_runtime 尚未加载，本次不返回段画面")
        return None
    try:
        clip = runtime.resolve_segment_raw_clip(plan, seg)
    except Exception as error:
        log.warning("H3 Segment Bus: 段 %s 取画面失败 (%s)", getattr(seg, "index", "?"), error)
        return None
    if clip is None or not clip.shape[0]:
        return None
    return _tensor_to_cpu(clip)


def _subsample(tensor, limit):
    """均匀抽到 limit 帧，返回 (张量, 段内帧号)。limit<=0 或不足则全帧。"""
    if tensor is None or not tensor.shape[0]:
        return None, []
    total = int(tensor.shape[0])
    n = int(limit or 0)
    if 0 < n < total:
        idx = [int(round(x)) for x in torch.linspace(0, total - 1, n).tolist()]
        return tensor[idx], idx
    return tensor, list(range(total))


def _time_labels(indices, fps) -> str:
    """段内时刻，从 0.00s 起（VLM 看到的片段就是从 0 开始的）。"""
    try:
        rate = float(fps) or 24.0
    except (TypeError, ValueError):
        rate = 24.0
    return "、".join(f"{int(i) / rate:.2f}s" for i in indices)


def _batch_images(tensors, what="图片"):
    """拼成 batch —— 与官方 Batch Images 同一策略：统一到第一张的尺寸再 cat。

    缩放用 comfy.utils.common_upscale(bilinear, center)，只改尺寸不改像素语义；
    通道数不同的图无法一起 cat，跳过并在控制台说明。
    """
    items = [t for t in (_tensor_to_cpu(x) for x in tensors)
             if t is not None and getattr(t, "shape", None) is not None and t.numel()]
    if not items:
        return None
    ref_h, ref_w, ref_c = (int(items[0].shape[1]), int(items[0].shape[2]), int(items[0].shape[3]))
    import comfy.utils

    out = []
    for t in items:
        if int(t.shape[3]) != ref_c:
            log.warning("H3 Segment Bus: %s 有 %d 通道图，与首张 %d 通道不兼容，已跳过",
                        what, int(t.shape[3]), ref_c)
            continue
        if int(t.shape[1]) != ref_h or int(t.shape[2]) != ref_w:
            t = comfy.utils.common_upscale(t.movedim(-1, 1), ref_w, ref_h, "bilinear",
                                           "center").movedim(1, -1)
        out.append(t)
    if not out:
        return None
    return torch.cat(out, dim=0) if len(out) > 1 else out[0]


class H3DirectorSegment:
    """读取导演台合并后的某一段：提示词 + 该段裁剪画面 + 该段参考图。"""

    @classmethod
    def INPUT_TYPES(cls):
        install()
        return {
            "required": {
                "segment_index": ("INT", {"default": 0, "min": 0, "max": 255}),
                "max_frames_per_segment": ("INT", {"default": 8, "min": 0, "max": 512,
                    "tooltip": "该段最多返回多少帧；0 = 全部帧。与列表节点同一策略："
                               "等距抽帧、含首尾、不影响 frame_count"}),
            },
            "optional": {
                "director_node": ("STRING", {"default": "", "tooltip":
                    "留空 = 自动找本图里唯一的 MiniMaxH3Director 节点；"
                    "有多台时填其中之一的节点 id（见 H3Director Bus Doctor）"}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "STRING", "INT", "INT", "INT", "IMAGE", "IMAGE", "STRING")
    RETURN_NAMES = ("prompt", "task_key", "segment_count", "frame_count", "segment_index",
                    "segment_frames", "ref_images", "segment_dump")
    FUNCTION = "read"
    CATEGORY = "RYH/minimax"

    @classmethod
    def IS_CHANGED(cls, segment_index=0, max_frames_per_segment=8, director_node="", **kwargs):
        # 数据来自本次 prompt 里的导演台，只有时间轴变了才需要重跑
        return f"{_RUN_FINGERPRINT}|{segment_index}|{max_frames_per_segment}|{director_node}"

    def read(self, segment_index=0, max_frames_per_segment=8, director_node="",
             dynprompt=None, unique_id=None):
        empty = torch.zeros((1, 16, 16, 3))
        plan, note = resolve_plan(dynprompt, director_node)
        if plan is None:
            return ("", "", 0, 0, 0, empty, empty.clone(), f"读不到分段：{note}")
        segments = getattr(plan, "segments", None) or []
        if not segments:
            return ("", "", 0, 0, 0, empty, empty.clone(), f"plan 里没有分段（{note}）")
        seg = segments[min(max(int(segment_index), 0), len(segments) - 1)]
        frames, picked = _subsample(_segment_frames(plan, seg), max_frames_per_segment)
        refs = sorted(getattr(seg, "refs", None) or [], key=lambda r: int(getattr(r, "index", 0)))
        prompt = str(getattr(seg, "prompt", "") or "")
        head = [
            f"数据来源: {note}",
            f"段 {seg.index}（时间轴卡 {getattr(seg, 'timeline_index', seg.index)}）"
            f" task={seg.task_key} use_global={getattr(seg, 'use_global', False)}",
            f"帧 {seg.start_frame}..{seg.end_frame}（{seg.frame_count}）"
            f" 画布 {plan.width}x{plan.height} @ {plan.frame_rate}fps",
            f"参考图 {len(refs)} 张"
            + ("：" + "、".join(f"#{int(r.index)}" for r in refs) if refs else ""),
            f"参考视频 {len(getattr(seg, 'ref_videos', None) or [])} 段 / "
            f"参考音频 {len(getattr(seg, 'ref_audios', None) or [])} 条 / "
            f"源视频音轨 {len(getattr(seg, 'ref_video_audios', None) or [])} 条",
            f"段间引导引用上段={getattr(seg, 'continuity_from_prev', True)} "
            f"ref_image_size={getattr(seg, 'ref_image_size', '-')}",
            f"全局提示词 {len(getattr(plan, 'global_prompt', '') or '')} chars"
            f" · 本段提示词 {len(prompt)} chars",
            f"该段画面 {tuple(frames.shape)}" if frames is not None
            else "该段画面: 未取到（t2v/r2v 无源片，或 segment_runtime 未加载）",
            (f"抽帧时刻 {len(picked)}/{seg.frame_count} 帧 @ {plan.frame_rate}fps："
             + _time_labels(picked, plan.frame_rate)) if picked else "抽帧时刻: 无画面",
        ]
        snap = SNAPSHOTS.get(str(unique_id or "")) or get_snapshot()
        mine = [n for n in (snap or {}).get("notes") or [] if f"段 {seg.index}" in n]
        if mine:
            head.append("上次 Queue 已回写: " + "; ".join(mine))
        batch = _batch_images([r.tensor for r in refs], what=f"段 {seg.index} 参考图")
        return (prompt, str(getattr(seg, "task_key", "") or ""),
                len(segments), int(seg.frame_count), int(seg.index),
                frames if frames is not None else empty,
                batch if batch is not None else empty.clone(),
                "\n".join(head) + "\n\n" + prompt)


class H3DirectorSegmentList:
    """一次吐出全部分段的列表 —— 下游 LLM 节点会被 ComfyUI 自动按段展开执行。"""

    @classmethod
    def INPUT_TYPES(cls):
        install()
        return {
            "required": {
                "max_frames_per_segment": ("INT", {"default": 8, "min": 0, "max": 512,
                    "tooltip": "每段最多返回多少帧；0 = 全部帧。喂 VLM 时建议 4-8"}),
            },
            "optional": {
                "director_node": ("STRING", {"default": "", "tooltip":
                    "留空 = 自动找本图里唯一的 MiniMaxH3Director；多台时填节点 id"}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "STRING", "INT", "IMAGE", "IMAGE", "INT", "STRING")
    RETURN_NAMES = ("prompts", "task_keys", "frame_counts", "segment_frames",
                    "ref_images", "segment_count", "segments_dump")
    OUTPUT_IS_LIST = (True, True, True, True, True, False, False)
    FUNCTION = "read"
    CATEGORY = "RYH/minimax"

    @classmethod
    def IS_CHANGED(cls, max_frames_per_segment=8, director_node="", **kwargs):
        return f"{_RUN_FINGERPRINT}|{max_frames_per_segment}|{director_node}"

    def read(self, max_frames_per_segment=8, director_node="", dynprompt=None, unique_id=None):
        plan, note = resolve_plan(dynprompt, director_node)
        segments = list(getattr(plan, "segments", None) or []) if plan is not None else []
        prompts, keys, counts, frames, refs = [], [], [], [], []
        placeholder = torch.zeros((1, 16, 16, 3))
        dump = [f"数据来源: {note}"]
        for seg in segments:
            prompts.append(str(getattr(seg, "prompt", "") or ""))
            keys.append(str(getattr(seg, "task_key", "") or ""))
            counts.append(int(seg.frame_count))
            clip, picked = _subsample(_segment_frames(plan, seg), max_frames_per_segment)
            frames.append(clip if clip is not None else placeholder.clone())
            seg_refs = sorted(getattr(seg, "refs", None) or [], key=lambda r: int(getattr(r, "index", 0)))
            batch = _batch_images([r.tensor for r in seg_refs], what=f"段 {seg.index} 参考图")
            refs.append(batch if batch is not None else placeholder.clone())
            dump.append(f"[{seg.index}] task={keys[-1]} frames={counts[-1]} "
                        f"refs={len(seg_refs)} @ {plan.frame_rate}fps\n"
                        f"    t={_time_labels(picked, plan.frame_rate) or '无画面'}\n"
                        f"{prompts[-1]}")
        if not segments:
            dump = [f"读不到分段：{note}"]
        return (prompts or [""], keys or [""], counts or [0], frames or [placeholder.clone()],
                refs or [placeholder.clone()], len(segments), "\n\n".join(dump))


class H3DirectorSegmentListPromptOverride:
    """吃下游展开回来的字符串列表，按顺序逐段回写。"""

    @classmethod
    def INPUT_TYPES(cls):
        install()
        return {
            "required": {
                "prompts": ("STRING", {"multiline": True, "tooltip":
                    "连线时以线为准：接 LLM 的输出列表，第 i 项写进段 i。\n"
                    "不连线时这里是手工输入口：把在别处生成的提示词粘进来，一行一段。\n"
                    "空行/空字符串的段保持原文不动"}),
            },
            "optional": {"bus": (BUS_TYPE, {"tooltip": "接另一个 override 节点叠加"})},
        }

    INPUT_IS_LIST = (True, False)
    RETURN_TYPES = (BUS_TYPE, "STRING")
    RETURN_NAMES = ("bus", "summary")
    FUNCTION = "collect"
    CATEGORY = "RYH/minimax"

    def collect(self, prompts, bus=None):
        # INPUT_IS_LIST 下未连线的 widget 值是原样传入的裸字符串（execution.py 的
        # input_data_all[x] = obj 分支），所以这里自己按行拆成列表。
        if isinstance(prompts, str):
            texts = prompts.splitlines()
        else:
            texts = list(prompts) if isinstance(prompts, (list, tuple)) else [prompts]
        out = dict(bus or {})
        overrides = dict(out.get("overrides") or {})
        skipped = 0
        for i, text in enumerate(texts):
            text = str(text or "").strip()
            if not text:
                skipped += 1
                continue
            overrides[i] = str(text)
        out["overrides"] = overrides
        summary = (f"{len(overrides)} 段回写"
                   + (f"（{skipped} 段空文本，保持原文）" if skipped else ""))
        return (out, summary)


class H3DirectorSegmentPromptOverride:
    """把下游生成的提示词攒成 bus，连到导演台的 h3_bus 口即可回写。"""

    @classmethod
    def INPUT_TYPES(cls):
        install()
        return {
            "required": {
                "mode": (["单段", "所有段用同一文本", "按行分配到各段"], {"default": "单段"}),
                "segment_index": ("INT", {"default": 0, "min": 0, "max": 255,
                    "tooltip": "mode=单段 时生效"}),
                "prompt": ("STRING", {"default": "", "multiline": True,
                    "tooltip": "新提示词；按行分配时每行一段"}),
                "force_director_rerun": ("BOOLEAN", {"default": False,
                    "tooltip": "打开后每次 Queue 都强制导演台重跑。LLM 输出不确定、"
                               "但上游 widget 没动时需要它，否则 ComfyUI 会命中导演台的"
                               "缓存而跳过执行，回写不生效"}),
            },
            "optional": {
                "bus": (BUS_TYPE, {"tooltip": "接上一个 override 节点，多个节点可叠加"}),
                "prompt_in": ("STRING", {"multiline": True, "tooltip":
                    "接上游 LLM 的输出；接了就以它为准，忽略 prompt 控件"}),
            },
        }

    RETURN_TYPES = (BUS_TYPE, "STRING")
    RETURN_NAMES = ("bus", "summary")
    FUNCTION = "collect"
    CATEGORY = "RYH/minimax"
    _counter = 0

    @classmethod
    def IS_CHANGED(cls, force_director_rerun=False, **kwargs):
        if force_director_rerun:
            cls._counter += 1
            return f"ryh-force-{cls._counter}"
        return False

    def collect(self, mode="单段", segment_index=0, prompt="", force_director_rerun=False,
                bus=None, prompt_in=None):
        text = prompt_in if (prompt_in or "").strip() else str(prompt or "")
        out = dict(bus or {})
        overrides = dict(out.get("overrides") or {})
        if mode == "按行分配到各段":
            for i, line in enumerate([ln for ln in text.splitlines() if ln.strip()]):
                overrides[i] = line.strip()
        elif mode == "所有段用同一文本":
            if text.strip():
                overrides[-1] = text
        elif text.strip():
            overrides[int(segment_index)] = text
        out["overrides"] = overrides
        summary = "、".join(f"{'全部段' if k == -1 else f'段{k}'}" for k in sorted(overrides)) or "（空）"
        return (out, f"{len(overrides)} 条 override：{summary}")


def install(*_args, **_kwargs) -> bool:
    """幂等；只在**已加载**的导演台模块上打补丁，绝不按裸名 import（会造出重复模块）。"""
    module = ref.find("nodes.director")
    if module is None:
        ref.warn_missing_once("H3 Segment Bus")
        return False
    cls = getattr(module, DIRECTOR_CLASS, None)
    if cls is None:
        log.warning("H3 Segment Bus: 模块里没有 %s，跳过", DIRECTOR_CLASS)
        return False
    fresh = not getattr(getattr(module, PLAN_FUNC, None), MARKER, False)

    # 每块独立 try：导演台改版时不能让一处失败留下"半套补丁"
    try:
        _patch_input_types(cls)
    except Exception as error:
        log.warning("H3 Segment Bus: INPUT_TYPES 补丁失败 (%s)", error)
    try:
        _patch_execute(cls)
    except Exception as error:
        log.warning("H3 Segment Bus: execute 补丁失败 (%s)", error)
    try:
        _patch_plan_func(module)
    except Exception as error:
        log.warning("H3 Segment Bus: %s 补丁失败 (%s)", PLAN_FUNC, error)
    if fresh:
        log.info("H3 Segment Bus: h3_bus 输入口已注入导演台；页面若在补丁之前打开，刷新一次浏览器即可看到")
    return True


def _patch_input_types(cls) -> None:
    if getattr(cls.INPUT_TYPES, MARKER, False):
        return
    raw_it = inspect.getattr_static(cls, "INPUT_TYPES")
    it_func = getattr(raw_it, "__func__", raw_it)

    def input_types(klass):
        spec = it_func(klass)
        spec.setdefault("optional", {})[BUS_INPUT] = (BUS_TYPE, {
            "tooltip": "接 H3 Prompt Override (RYH)：建完 plan 后、采样前回写各段提示词"})
        return spec

    setattr(input_types, MARKER, True)
    cls.INPUT_TYPES = classmethod(input_types)


def _patch_execute(cls) -> None:
    raw_exec = inspect.getattr_static(cls, "execute")
    if getattr(raw_exec, MARKER, False):
        return

    def exec_wrapper(self, *args, **kwargs):
        _RUN["bus"] = kwargs.pop(BUS_INPUT, None)
        _RUN["node_id"] = kwargs.get("unique_id")
        try:
            return raw_exec(self, *args, **kwargs)
        finally:
            _RUN["bus"] = None

    setattr(exec_wrapper, MARKER, True)
    cls.execute = exec_wrapper


def _patch_plan_func(module) -> None:
    original = getattr(module, PLAN_FUNC, None)
    if original is None:
        log.warning("H3 Segment Bus: %s 里没有 %s，快照不会生效",
                    getattr(module, "__name__", module), PLAN_FUNC)
        return
    if getattr(original, MARKER, False):
        return

    def plan_wrapper(*args, **kwargs):
        node_id = str(kwargs.get("unique_id") or _RUN.get("node_id") or "")
        pending = PENDING.pop(node_id, None)
        if pending is not None and pending["key"] == _plan_key(kwargs):
            plan = pending["plan"]          # 读节点已经建过同一份，直接复用
            log.info("H3 Segment Bus: 复用读节点的 plan（节点 %s，%d 段）",
                     node_id, len(getattr(plan, "segments", None) or []))
        else:
            plan = original(*args, **kwargs)
        try:
            bus = _RUN.get("bus")
            notes = _apply_overrides(plan, bus) if bus else []
            _snapshot(plan, kwargs.get("unique_id") or _RUN.get("node_id"), notes,
                      bus_seen=bool(bus),
                      override_count=len((bus or {}).get("overrides") or {}))
            if notes:
                log.info("H3 Segment Bus: 回写 %s", "; ".join(notes))
        except Exception as error:
            log.warning("H3 Segment Bus: 快照/回写失败 (%s)", error)
        return plan

    setattr(plan_wrapper, MARKER, True)
    setattr(module, PLAN_FUNC, plan_wrapper)
    log.info("H3 Segment Bus: 已接到 %s.%s + %s.%s",
             DIRECTOR_CLASS, "INPUT_TYPES/execute", getattr(module, "__name__", ""), PLAN_FUNC)


install()
ref.add_startup_hook(install)
ref.add_run_hook(capture_fingerprint)
ref.add_run_hook(install)
log.info("H3 Segment Bus: 已加载（导演台 %s）",
         "已就位，补丁已挂" if ref.loaded() else "尚未加载，服务启动前与每次 Queue 前各重试一次")


class H3DirectorBusDoctor:
    """自检：补丁挂在哪一步断了、快照里到底有什么。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "hidden": {"dynprompt": "DYNPROMPT"}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "check"
    CATEGORY = "RYH/minimax"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        snap = get_snapshot()
        return f"{_RUN_FINGERPRINT}|{len(SNAPSHOTS)}|{(snap or {}).get('time', '')}"

    def check(self, dynprompt=None):
        lines = []
        ids = director_node_ids(dynprompt)
        lines.append(f"本次 prompt 里的导演台节点: {ids or '（无）'} | 待复用 plan: {list(PENDING) or '（无）'}")
        module = ref.find("nodes.director")
        lines.append(f"find('nodes.director') -> {getattr(module, '__name__', None)}")
        if module is None:
            lines.append("  导演台模块没找到：确认 ComfyUI_MiniMaxH3_Director 已安装且未被禁用")
            return ("\n".join(lines),)
        cls = getattr(module, DIRECTOR_CLASS, None)
        lines.append(f"install() -> {install()}")
        try:
            it = inspect.getattr_static(cls, "INPUT_TYPES")
            it_fn = getattr(it, "__func__", it)
            lines.append(f"INPUT_TYPES 已打补丁: {getattr(it_fn, MARKER, False)}")
            lines.append(f"  h3_bus 在 optional 里: {'h3_bus' in cls.INPUT_TYPES().get('optional', {})}")
        except Exception as error:
            lines.append(f"INPUT_TYPES 检查失败: {error}")
        try:
            ex = inspect.getattr_static(cls, "execute")
            lines.append(f"execute 已打补丁: {getattr(getattr(ex, '__func__', ex), MARKER, False)}")
        except Exception as error:
            lines.append(f"execute 检查失败: {error}")
        plan = getattr(module, PLAN_FUNC, None)
        lines.append(f"{PLAN_FUNC} 已打补丁: {getattr(plan, MARKER, False) if plan is not None else '模块缺失'}")

        root = ref.find("")
        registered = getattr(root, "NODE_CLASS_MAPPINGS", {}).get(DIRECTOR_CLASS) if root else None
        lines.append(f"注册表里的类 is 同一个对象: {registered is cls}"
                     f"（注册={getattr(registered, '__module__', None)} / 补丁={getattr(cls, '__module__', None)}）")
        lines.append(f"快照数 {len(SNAPSHOTS)}: " + (", ".join(
            f"{k}={snap['count']}段@{snap['time']}" for k, snap in SNAPSHOTS.items()) or "（空）"))
        snap = get_snapshot()
        if snap:
            first = (getattr(snap["plan"], "segments", None) or [None])[0]
            lines.append(f"  auto 快照首段 prompt: {str(getattr(first, 'prompt', ''))[:60]!r}")
            lines.append(f"  那次执行 h3_bus 是否接线: {'是' if snap.get('bus_seen') else '否'}"
                         f" | 收到的 override 条数: {snap.get('override_count', 0)}")
            lines.append(f"  实际改写记录: {snap['notes'] or '（无）'}")
        else:
            lines.append("  没有快照：导演台在本进程内没有真正执行过"
                         "（被缓存跳过 / 重启后未再跑 / 或补丁晚于那次执行）")
        lines.append(f"_RUN（本轮结束后的残留状态，bus 每次执行完都会清空）: "
                     f"node_id={_RUN.get('node_id')}")
        return ("\n".join(lines),)


