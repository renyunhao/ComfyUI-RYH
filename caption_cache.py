"""图片反推结果缓存。

为多种第三方反推节点提供一层持久化缓存，当前已适配：

- comfyui-llama-TE 的 `QwenTE图像推理`
- XB_ToolBox 的 `XB_llamaInstruct`

工作方式：

- 命中缓存：直接返回上次反推得到的文本，**完全不调用模型**（配合懒加载，
  连 GGUF 都不会被加载进显存）。
- 未命中：正常执行原反推逻辑，并把结果写入磁盘缓存。

实现方式是对第三方节点做 monkey-patch，不修改任何第三方源文件，因此对方
升级不会丢失本功能（只要类名 / 方法名保持稳定）。新增反推节点时，在文件末尾
`_TARGETS` 注册表里加一条（find 定位模块 + install 安装 patch）即可。

缓存 key = 图片内容哈希 + 影响输出文本的全部参数 + 模型配置指纹。
内容哈希保证「同内容不同路径」也能命中，「同路径换了图」不会误命中，
因此比单纯按文件路径查更可靠，这里不再依赖路径。
"""

import hashlib
import json
import os
import threading

import folder_paths

LOG_PREFIX = "[RYH CaptionCache]"

# 缓存文件名与所在目录（ComfyUI user 目录下，跨工作流共享、随插件配置走）
_CACHE_SUBDIR = "ComfyUI-RYH"
_CACHE_FILENAME = "caption_cache.json"
_CACHE_VERSION = 1
# 简单容量上限，超出后按写入时间淘汰最旧条目，避免无限增长
_MAX_ENTRIES = 5000

_lock = threading.Lock()
_cache = None  # 运行期常驻的内存缓存：{"version":int,"entries":{key:{...}}}


def _cache_path():
    d = os.path.join(folder_paths.get_user_directory(), _CACHE_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _CACHE_FILENAME)


def _load_disk():
    path = _cache_path()
    if not os.path.isfile(path):
        return {"version": _CACHE_VERSION, "entries": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"{LOG_PREFIX} 缓存文件损坏，已忽略并重建: {e}")
        return {"version": _CACHE_VERSION, "entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"version": _CACHE_VERSION, "entries": {}}
    if data.get("version") != _CACHE_VERSION:
        # 版本变化时旧条目 key 语义可能不同，直接丢弃
        return {"version": _CACHE_VERSION, "entries": {}}
    return data


def _ensure_loaded():
    global _cache
    if _cache is None:
        _cache = _load_disk()
    return _cache


def _save_disk():
    path = _cache_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get(key):
    """命中返回缓存文本，否则返回 None。"""
    with _lock:
        entries = _ensure_loaded()["entries"]
        item = entries.get(key)
        return item.get("result") if isinstance(item, dict) else None


def put(key, result, meta=None):
    """写入一条缓存；超出容量时按 created 淘汰最旧条目。"""
    with _lock:
        cache = _ensure_loaded()
        entries = cache["entries"]
        entry = {"result": result}
        if meta:
            entry["meta"] = meta
        entry["created"] = _now()
        entries[key] = entry
        if len(entries) > _MAX_ENTRIES:
            oldest = sorted(entries.items(), key=lambda kv: kv[1].get("created", 0))
            for k, _ in oldest[: len(entries) - _MAX_ENTRIES]:
                entries.pop(k, None)
        try:
            _save_disk()
        except OSError as e:
            print(f"{LOG_PREFIX} 缓存写盘失败: {e}")


def clear():
    """清空全部缓存（落盘）。"""
    with _lock:
        global _cache
        _cache = {"version": _CACHE_VERSION, "entries": {}}
        try:
            _save_disk()
        except OSError as e:
            print(f"{LOG_PREFIX} 清空缓存写盘失败: {e}")


def _now():
    import time

    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 缓存 key 计算
# ---------------------------------------------------------------------------

def _hash_tensor(tensor):
    """对单张 IMAGE tensor（[H, W, C] float）算稳定内容哈希。"""
    try:
        import numpy as np

        arr = tensor.detach().cpu().numpy()
    except Exception:
        # 非 tensor 兜底：转 bytes
        return hashlib.sha256(repr(tensor).encode("utf-8")).hexdigest()
    h = hashlib.sha256()
    h.update(str(arr.shape).encode("utf-8"))
    h.update(arr.astype(np.float32).tobytes())
    return h.hexdigest()


def _images_hash(kwargs, image_keys):
    """对图片输入逐个求哈希并合并，作为“同一组图”的标识。

    image_keys: 该反推节点承载 IMAGE 的输入名列表（QwenTE 是 图片..图片8，
    XB 是 images）。XB 的 images 是批量 [N,H,W,C]，逐帧哈希；QwenTE 每个口
    只取第 1 张，但这里对所有帧哈希更保守（多帧内容变化也能区分）。
    """
    parts = []
    for name in image_keys:
        t = kwargs.get(name)
        if t is None:
            continue
        try:
            n = int(t.shape[0])
        except Exception:
            n = 0
        if n <= 0:
            continue
        for i in range(n):
            parts.append(_hash_tensor(t[i]))
    if not parts:
        return "no-image"
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _params_fingerprint(kwargs, skip_keys):
    """把影响输出文本的参数规范化成指纹；skip_keys 内的键不参与。

    嵌套在 parameters 字典里的 state_uid 只控制对话状态、不影响单张图的反推
    文本，这里一并剔除。
    """
    fp = {}
    for k, v in kwargs.items():
        if k in skip_keys:
            continue
        if isinstance(v, dict):
            v = {kk: vv for kk, vv in v.items() if kk != "state_uid"}
        fp[k] = v
    return hashlib.sha256(
        json.dumps(fp, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _model_fingerprint(model):
    """从模型输入提取配置指纹，兼容三种形态：

    - 懒代理（QwenTE）：读 _ryh_config；
    - 真实模型对象（QwenTE）：读 settings；
    - 纯配置 dict / list / str（XB 本地 config 或在线 API JSON）：直接哈希。
    """
    cfg = getattr(model, "_ryh_config", None)
    if cfg is None:
        cfg = getattr(model, "settings", None)
    if cfg is None and isinstance(model, (dict, list, str)):
        cfg = model
    if cfg is None:
        return "unknown-model"
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def build_key_parts(kwargs, target):
    """返回 (完整key, 分量字典)。分量字典写入缓存 meta，便于排查 miss 原因。"""
    parts = {
        "image": _images_hash(kwargs, target["image_keys"]),
        "params": _params_fingerprint(kwargs, target["skip_keys"]),
        "model": _model_fingerprint(kwargs.get(target["model_key"])),
    }
    raw = "|".join((parts["image"], parts["params"], parts["model"]))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), parts


def build_key(kwargs, target):
    return build_key_parts(kwargs, target)[0]


# ---------------------------------------------------------------------------
# 懒加载代理：让“模型加载器”节点先返回占位对象，真正加载推迟到首次访问
# ---------------------------------------------------------------------------

class _LazyQwenModel:
    """包装 comfyui-llama-TE 的模型对象，延迟到真正需要时才加载 GGUF。

    - 缓存命中路径：`run` 里只读取 `_ryh_config`（普通实例属性，不触发加载）后
      直接返回，代理永远不会被解析，模型不进显存。
    - 其它任何属性访问（含第三方多轮对话等消费者）：自动解析为真实模型并委托，
      行为对上层透明。
    """

    def __init__(self, config, storage_cls, real_loader, prev_model=None):
        self.__dict__["_ryh_config"] = config
        self.__dict__["_ryh_storage_cls"] = storage_cls
        self.__dict__["_ryh_real_loader"] = real_loader
        self.__dict__["_ryh_real"] = None
        # 创建本代理时全局正在使用的真实模型（可能是 None）。
        # resolve 时把它放回 cls.model，让原 load 决定复用还是先 unload，
        # 避免“代理覆盖 cls.model 后旧模型失去引用而泄漏显存”。
        self.__dict__["_ryh_prev"] = prev_model

    def resolve(self):
        """真正加载并返回底层模型对象（幂等，且与 _QwenStorage.model 保持同步）。"""
        cls = self.__dict__["_ryh_storage_cls"]
        cfg = self.__dict__["_ryh_config"]
        real = self.__dict__["_ryh_real"]
        cur = cls.model
        # 已解析且仍是全局当前模型：直接复用
        if real is not None and cur is real:
            return real
        # 选出“上一个真实模型”交给原 load：配置一致则复用、不一致则原 load
        # 会先 unload 再加载，避免显存泄漏。
        if cur is self:
            prev = self.__dict__["_ryh_prev"]
        elif isinstance(cur, _LazyQwenModel):
            prev = cur._ryh_real if cur._ryh_real is not None else cur._ryh_prev
        else:
            prev = cur
        cls.model = prev
        self.__dict__["_ryh_real"] = self.__dict__["_ryh_real_loader"](cfg)
        return self.__dict__["_ryh_real"]

    def __getattr__(self, name):
        # 只有常规查找失败时才会走到这里（_ryh_* 都在 __dict__ 中，不会进来）
        return getattr(self.resolve(), name)


# ---------------------------------------------------------------------------
# monkey-patch 安装
# ---------------------------------------------------------------------------

_PATCHED_FLAG = "_ryh_caption_cache_patched"


def _find_module_with(*attr_names, require=None):
    """在 sys.modules 中查找同时具备指定类/方法形态的第三方模块。

    不能用 hasattr 宽松匹配：torch.ops 等对象的 __getattr__ 对任意属性名都会
    惰性返回 _OpNamespace。这里要求每个属性必须是真正的类，并额外校验
    require 中的 (类名, 方法名, 是否 classmethod) 形态。
    """
    import inspect
    import sys

    for name, mod in list(sys.modules.items()):
        if mod is None or name == "__main__":
            continue
        pkg = __package__ or ""
        if pkg and (name == pkg or name.startswith(pkg + ".")):
            continue
        try:
            classes = {}
            ok = True
            for attr in attr_names:
                cls = getattr(mod, attr, None)
                if not inspect.isclass(cls):
                    ok = False
                    break
                classes[attr] = cls
            if not ok:
                continue
            for cls_name, meth, is_classmethod in require or ():
                meth_obj = classes[cls_name].__dict__.get(meth, None)
                if is_classmethod:
                    if not isinstance(meth_obj, classmethod):
                        ok = False
                        break
                elif not callable(meth_obj):
                    ok = False
                    break
            if not ok:
                continue
        except Exception:
            # 个别模块的属性访问可能抛异常，跳过继续找
            continue
        return mod
    return None


# 各反推节点的 key 计算配置：图片输入名 / 模型输入名 / 不参与指纹的键
# 注意 seed 不参与 key：缓存的语义是「同图同提示词复用首次结果」，种子随机化
# 不应让缓存失效（否则每次队列都 miss）。
_QWEN_KEY_CFG = {
    "image_keys": ("图片", "图片2", "图片3", "图片4", "图片5", "图片6", "图片7", "图片8"),
    "model_key": "qwen模型",
    "skip_keys": {
        "qwen模型",
        "生成后自动卸载模型",
        "seed",
        "图片",
        "图片2",
        "图片3",
        "图片4",
        "图片5",
        "图片6",
        "图片7",
        "图片8",
    },
}

_XB_KEY_CFG = {
    "image_keys": ("images",),
    "model_key": "llama_model",
    "skip_keys": {
        "llama_model",
        "images",
        "seed",
        "force_offload",  # 只是卸载副作用
        "save_states",  # 参与缓存时恒为 False（见 cached_process）
        "queue_handler",  # 仅控制执行顺序
        "unique_id",  # 只影响 state_uid 输出，命中时重新计算
    },
}


# ---------------------------------------------------------------------------
# 目标 1：comfyui-llama-TE（QwenTE图像推理）
# ---------------------------------------------------------------------------

def _install_qwen_patch(mod):
    storage = mod._QwenStorage
    infer_cls = mod.QwenTE图像推理
    if getattr(infer_cls, _PATCHED_FLAG, False):
        return True

    # 1) 拦截 _QwenStorage.load：返回懒代理，真正的加载函数保存下来供 resolve 用。
    #    同时把代理记录到 cls.model，使直接读取 _QwenStorage.model 的消费者
    #    （如多轮对话的 _同步Qwen模型）行为不变：首次访问属性时自动触发真实加载。
    original_storage_load = storage.load.__func__  # classmethod 的底层函数

    def lazy_load(cls, config):
        cur = cls.model
        if cur is not None and not isinstance(cur, _LazyQwenModel):
            # 已有真实模型且配置一致：直接复用（等价于原 load 的去重语义）
            if getattr(cur, "settings", None) == config:
                return cur
            prev = cur
        elif isinstance(cur, _LazyQwenModel):
            # 当前是（可能尚未解析的）代理：沿链找到最近一个真实模型
            prev = cur._ryh_real if cur._ryh_real is not None else cur._ryh_prev
        else:
            prev = None
        proxy = _LazyQwenModel(config, cls, lambda cfg: original_storage_load(cls, cfg), prev)
        cls.model = proxy
        return proxy

    storage.load = classmethod(lazy_load)

    # 2) 包装 QwenTE图像推理.run：命中直接返回；未命中先解析代理再调原方法
    original_run = infer_cls.run

    def cached_run(self, *args, **kwargs):
        # ComfyUI 以关键字参数调用 FUNCTION，正常 kwargs 已含全部输入
        if "qwen模型" not in kwargs:
            return original_run(self, *args, **kwargs)
        model = kwargs.get("qwen模型")
        key, parts = build_key_parts(kwargs, _QWEN_KEY_CFG)
        cached = get(key)
        if cached is not None:
            print(f"{LOG_PREFIX} [QwenTE] 命中缓存，跳过反推（模型未加载）。")
            return (cached,)

        # 未命中：把懒代理解析成真实模型，避免原 run 内部触发二次加载
        if isinstance(model, _LazyQwenModel):
            kwargs["qwen模型"] = model.resolve()

        result = original_run(self, *args, **kwargs)
        if isinstance(result, tuple) and len(result) == 1 and isinstance(result[0], str):
            put(key, result[0], meta=parts)
            print(f"{LOG_PREFIX} [QwenTE] 未命中，已写入缓存。")
        return result

    infer_cls.run = cached_run
    setattr(infer_cls, _PATCHED_FLAG, True)
    print(f"{LOG_PREFIX} 已为 comfyui-llama-TE 安装反推缓存 patch。")
    return True


# ---------------------------------------------------------------------------
# 目标 2：XB_ToolBox（XB_llamaInstruct）
# ---------------------------------------------------------------------------

def _install_xb_patch(mod):
    storage = mod.LLAMA_CPP_STORAGE
    infer_cls = mod.XB_llamaInstruct
    loader_cls = mod.XB_llamaModelLoader
    if getattr(infer_cls, _PATCHED_FLAG, False):
        return True

    # 1) 模型加载器改为懒加载：原实现只要配置变化就立刻 load_model（eager）。
    #    这里只构造并返回 config dict（与原版字段完全一致，加载器输出本来就是
    #    纯 dict），真正加载推迟到 cached_process 未命中时执行。
    def lazy_loadmodel(self, **kwargs):
        return (dict(kwargs),)

    loader_cls.loadmodel = lazy_loadmodel

    # 2) 包装 XB_llamaInstruct.process：输出为 (out1, out2, state_uid) 三元组，
    #    缓存 out1/out2，uid 命中时按原规则重算。
    original_process = infer_cls.process

    def cached_process(self, *args, **kwargs):
        # 只对“带图片、非多轮状态”的反推请求做缓存：
        # - 纯文本模式可能与会话状态耦合；save_states=True 时输出依赖历史，
        #   缓存 key 无法表达，直接放行原逻辑。
        if "llama_model" not in kwargs:
            return original_process(self, *args, **kwargs)
        images = kwargs.get("images")
        try:
            has_images = images is not None and int(images.shape[0]) > 0
        except Exception:
            has_images = False
        if not has_images or kwargs.get("save_states"):
            return original_process(self, *args, **kwargs)

        # 关键：原 process 会对 parameters 字典做 pop（present_penalty / state_uid），
        # 而该字典是上游 XB_llamaParameters 节点的输出对象、跨次执行复用。若放任
        # 其被变异，下次执行算出的 params 指纹就少了键，导致同图同参也 miss。
        # 这里传副本进去，保护共享字典不被污染。
        params = kwargs.get("parameters")
        if isinstance(params, dict):
            kwargs["parameters"] = dict(params)

        key, parts = build_key_parts(kwargs, _XB_KEY_CFG)
        cached = get(key)
        if cached is not None:
            params = kwargs.get("parameters") or {}
            uid = params.get("state_uid", None)
            if uid in (None, -1):
                uid = str(kwargs.get("unique_id", "0")).rpartition(".")[-1]
            print(f"{LOG_PREFIX} [XB_llama] 命中缓存，跳过反推（模型未加载）。")
            return (cached.get("out1", ""), cached.get("out2", []), uid)

        # 未命中：懒加载模式下确保模型就绪。llama_model 为 dict/list 时是本地
        # 配置；为字符串时是在线 API 配置，无需加载。
        model = kwargs.get("llama_model")
        if isinstance(model, (dict, list)):
            if storage.llm is None or storage.current_config != model:
                storage.load_model(model)

        result = original_process(self, *args, **kwargs)
        if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[0], str):
            out2 = result[1]
            put(
                key,
                {
                    "out1": result[0],
                    "out2": list(out2) if isinstance(out2, (list, tuple)) else out2,
                },
                meta=parts,
            )
            print(f"{LOG_PREFIX} [XB_llama] 未命中，已写入缓存。")
        return result

    infer_cls.process = cached_process
    setattr(infer_cls, _PATCHED_FLAG, True)
    print(f"{LOG_PREFIX} 已为 XB_ToolBox 安装反推缓存 patch。")
    return True


# ---------------------------------------------------------------------------
# 目标注册表：新增反推节点时在此登记
# ---------------------------------------------------------------------------

_TARGETS = (
    {
        "name": "comfyui-llama-TE",
        "find": lambda: _find_module_with(
            "QwenTE图像推理",
            "_QwenStorage",
            require=(("_QwenStorage", "load", True), ("QwenTE图像推理", "run", False)),
        ),
        "install": _install_qwen_patch,
    },
    {
        "name": "XB_ToolBox",
        "find": lambda: _find_module_with(
            "XB_llamaInstruct",
            "XB_llamaModelLoader",
            "LLAMA_CPP_STORAGE",
            require=(
                ("LLAMA_CPP_STORAGE", "load_model", True),
                ("XB_llamaInstruct", "process", False),
                ("XB_llamaModelLoader", "loadmodel", False),
            ),
        ),
        "install": _install_xb_patch,
    },
)


def apply_patch():
    """为所有已安装的反推插件目标安装缓存 patch。幂等；缺失的目标静默跳过。"""
    applied = False
    for target in _TARGETS:
        try:
            mod = target["find"]()
            if mod is None:
                print(f"{LOG_PREFIX} 未检测到 {target['name']}，跳过该目标。")
                continue
            if target["install"](mod):
                applied = True
        except Exception as e:
            print(f"{LOG_PREFIX} {target['name']} patch 安装失败，已跳过: {e}")
    return applied
