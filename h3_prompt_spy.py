"""H3 Prompt Spy — 抓到官方 MiniMax H3 条件节点真正收到的 prompt。

不复刻 AIMixer 导演台的拼装逻辑（concat_common_segment_prompt / reinforce_* 等），
而是包一层官方 MiniMaxH3ImageToVideo / MiniMaxH3ReferenceToVideo.execute，
所以 UI 时间轴路径和外部接线（i2v_groups / r2v_groups）路径都能抓到最终文本。

同时把每次采样写进 user/default/ComfyUI-RYH/h3_prompt_spy/<日期>.log。
"""

from __future__ import annotations

import datetime
import inspect
import collections
import logging
import os

import folder_paths

try:
    from . import ryh_director as ref
except ImportError:  # 直接按目录导入（离线测试用）
    import ryh_director as ref

log = logging.getLogger("RYH.h3_prompt_spy")
if not log.handlers:
    log.setLevel(logging.INFO)


class AnyType(str):
    def __eq__(self, _) -> bool:
        return True

    def __ne__(self, __value: object) -> bool:
        return True


_any = AnyType("*")

TARGETS = ("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo")
MARKER = "_ryh_prompt_spy"
MAX_RECORDS = 256
MAX_EXTRA_RUNS = 5     # extra_runs 上限，也决定归档桶数

records: list[dict] = []
RUNS: collections.deque = collections.deque(maxlen=MAX_EXTRA_RUNS)
_patched: list = []
_seq = 0
RUN_TOKEN = [0]          # 每轮 +1，供 IS_CHANGED 打破本节点的输出缓存


def start_run(prompt=None) -> None:
    """每次 Queue 开始时分桶，否则 log 会把上几轮的捕获混进来。"""
    global records, _seq
    if records:
        RUNS.append(records)
    records = []
    _seq = 0
    RUN_TOKEN[0] += 1


def _log_dir() -> str:
    path = os.path.join(
        folder_paths.get_user_directory(), "default", "ComfyUI-RYH", "h3_prompt_spy"
    )
    os.makedirs(path, exist_ok=True)
    return path


def _shape(obj) -> tuple | None:
    shape = getattr(obj, "shape", None)
    return tuple(shape) if shape is not None else None


def _count(value):
    if value is None:
        return 0
    if isinstance(value, dict):
        return len([v for v in value.values() if v is not None])
    if isinstance(value, (list, tuple)):
        return len(value)
    return 1


def _is_conditioning(obj) -> bool:
    # ComfyUI conditioning == list of [tensor, ref_options_dict]
    return isinstance(obj, (list, tuple)) and bool(obj) and isinstance(obj[0], (list, tuple)) \
        and len(obj[0]) > 1 and isinstance(obj[0][1], dict)


def _cond_summary(result) -> dict:
    """导演台注入的 minimax_keyframes / minimax_refs 是否真的进了 conditioning。"""
    out = {"keyframes": 0, "refs": [], "ctx_markers": 0}
    # io.NodeOutput.result -> args tuple, 即 (positive, latent)；见 comfy_api/latest/_io.py:2510
    payload = getattr(result, "result", None)
    if not isinstance(payload, (list, tuple)):
        return out
    cond = next((p for p in payload if _is_conditioning(p)), None)
    if cond is None:
        return out
    for entry in cond:
        meta = entry[1]
        kfs = meta.get("minimax_keyframes") or []
        out["keyframes"] = max(out["keyframes"], len(kfs))
        refs = meta.get("minimax_refs") or []
        kinds = [str(r.get("kind")) for r in refs if isinstance(r, dict)]
        if kinds:
            out["refs"] = kinds
        out["ctx_markers"] += sum(
            1 for k in kfs if isinstance(k, dict) and "director_context_index" in k
        )
    return out


def _capture(node_name: str, original, args, kwargs, result) -> None:
    values = dict(kwargs)
    try:
        bound = inspect.signature(original).bind(*args, **kwargs)
        bound.apply_defaults()
        values.update(bound.arguments)
    except (TypeError, ValueError):
        pass
    prompt = str(values.get("prompt") or "")
    summary = _cond_summary(result)
    record = {
        "seq": len(records) + 1,
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "node": node_name,
        "length": values.get("length"),
        "width": values.get("width"),
        "height": values.get("height"),
        "ref_image_size": values.get("ref_image_size"),
        "ref_images": _count(values.get("ref_images")),
        "ref_videos": _count(values.get("ref_videos")),
        "ref_audios": _count(values.get("ref_audios")) + _count(values.get("ref_video_audios")),
        "first_frame": _shape(values.get("first_frame")) is not None,
        "last_frame": _shape(values.get("last_frame")) is not None,
        "keyframes": summary["keyframes"],
        "ctx_keyframes": summary["ctx_markers"],
        "ref_kinds": summary["refs"],
        "prompt_chars": len(prompt),
        "prompt": prompt,
    }
    records.append(record)
    if len(records) > MAX_RECORDS:
        del records[: len(records) - MAX_RECORDS]
    _append_log(record)
    log.info(
        "H3 spy #%d %s len=%s %sx%s | %d chars | %r",
        record["seq"],
        node_name,
        record["length"],
        record["width"],
        record["height"],
        record["prompt_chars"],
        record["prompt"][:160],
    )


def _append_log(record: dict) -> None:
    name = datetime.datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(_log_dir(), f"{name}.log")
    block = [
        f"=== #{record['seq']}  {record['time']}  {record['node']}",
        (
            f"    length={record['length']} {record['width']}x{record['height']}"
            f"  refs(img/vid/aud)={record['ref_images']}/{record['ref_videos']}/{record['ref_audios']}"
            f"  keyframes={record['keyframes']} (ctx={record['ctx_keyframes']})"
            f"  ref_kinds={record['ref_kinds']}"
        ),
        "    " + record["prompt"].replace("\n", "\n    "),
        "",
    ]
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(block))
    except OSError as error:
        log.warning("H3 spy: 写日志失败 %s (%s)", path, error)


def install() -> None:
    try:
        import comfy_extras.nodes_minimax_h3 as mm
    except ImportError:
        log.warning("H3 spy: 当前 ComfyUI 没有 nodes_minimax_h3，探针未安装")
        return

    for name in TARGETS:
        cls = getattr(mm, name, None)
        if cls is None or not hasattr(cls, "execute"):
            continue
        raw = inspect.getattr_static(cls, "execute")
        fn = getattr(raw, "__func__", raw)
        if getattr(fn, MARKER, False):
            continue

        def make(original, node_name):
            head = list(inspect.signature(original).parameters)
            takes_cls = bool(head) and head[0] in ("cls", "self")

            def wrapper(cls_ref, *args, **kwargs):
                full = (cls_ref,) + args if takes_cls else args
                result = original(*full, **kwargs)
                try:
                    _capture(node_name, original, full, kwargs, result)
                except Exception as error:  # 探针永不影响出片
                    log.warning("H3 spy: 采集失败 %s", error)
                return result

            wrapper.__name__ = "execute"
            setattr(wrapper, MARKER, True)
            return classmethod(wrapper)

        setattr(cls, "execute", make(fn, name))
        _patched.append((cls, fn))


install()
ref.add_run_hook(start_run)


def format_records(buckets: list, include_refs: bool) -> str:
    """buckets: [(轮次标签, 记录列表)]，由旧到新，最后一个是本轮。"""
    pairs = [(label, r) for label, rows in buckets for r in rows]
    if not pairs:
        return "（还没有捕获：确认导演台真的采样了，且 trigger 接在导演台之后）"
    lines = []
    last_label = None
    for label, r in pairs:
        if len(buckets) > 1 and label != last_label:
            lines.append(f"───── {label} ─────")
            last_label = label
        head = (
            f"#{r['seq']} {r['node']}  length={r['length']}  {r['width']}x{r['height']}"
            f"  prompt={r['prompt_chars']}chars"
        )
        if include_refs:
            head += (
                f"\n    refs img/vid/aud = {r['ref_images']}/{r['ref_videos']}/{r['ref_audios']}"
                f" | keyframes={r['keyframes']} 段间引导帧={r['ctx_keyframes']}"
                f" | first/last={int(r['first_frame'])}/{int(r['last_frame'])}"
                f" | ref_kinds={r['ref_kinds']}"
                f" | ref_image_size={r['ref_image_size']}"
            )
        body = "\n".join(f"      {line}" for line in r["prompt"].splitlines())
        lines.append(f"{head}\n{body}")
    return "\n\n".join(lines)


class H3DirectorPromptSpy:
    """显示最近 N 次官方 H3 条件节点收到的最终 prompt。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "extra_runs": ("INT", {"default": 0, "min": 0, "max": MAX_EXTRA_RUNS,
                    "tooltip": "除本轮外再往前显示几轮的记录；0 = 只看本轮。"
                               "选中的轮次里每条都显示，不再二次截断"}),
                "show_refs": ("BOOLEAN", {"default": True}),
            },
            "optional": {"trigger": (_any, {"tooltip":
                "只为定序：接导演台的 report（或 images），强制本节点在导演台之后执行，"
                "否则 OUTPUT_NODE 会被提前调度、显示的是上一轮的记录"})},
        }

    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("log", "captured")
    FUNCTION = "report"
    CATEGORY = "RYH/minimax"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, extra_runs=0, show_refs=True, **kwargs):
        # 内容随本轮采样变化，必须每轮重渲染（本节点是终端显示节点，不会连带下游重跑）
        return f"spy-{RUN_TOKEN[0]}-{extra_runs}-{show_refs}"

    def report(self, extra_runs=0, show_refs=True, trigger=None):
        buckets = collect_buckets(extra_runs)
        rows = [r for _label, part in buckets for r in part]
        text = format_records(buckets, show_refs)
        return {"ui": {"text": [text]}, "result": (text, len(rows))}


def collect_buckets(extra_runs) -> list:
    """[(标签, 记录)]，由旧到新，最后一个是本轮。"""
    try:
        n = max(0, min(int(extra_runs), MAX_EXTRA_RUNS))
    except (TypeError, ValueError):
        n = 0
    done = list(RUNS)[-n:] if n else []
    out = [(f"倒数第 {len(done) - i} 轮", rows) for i, rows in enumerate(done)]
    out.append(("本轮", records))
    return out


