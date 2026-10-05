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
        print(f"{LOG_PREFIX} 缓存文件损坏，已忽略并重建: {e}", flush=True)
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
            print(f"{LOG_PREFIX} 缓存写盘失败: {e}", flush=True)


def clear():
    """清空全部缓存（落盘）。"""
    with _lock:
        global _cache
        _cache = {"version": _CACHE_VERSION, "entries": {}}
        try:
            _save_disk()
        except OSError as e:
            print(f"{LOG_PREFIX} 清空缓存写盘失败: {e}", flush=True)


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

    返回 (指纹哈希, 规范化后的参数字典)；字典供写入缓存 meta 时原样留档。
    """
    fp = {}
    for k, v in kwargs.items():
        if k in skip_keys:
            continue
        if isinstance(v, dict):
            v = {kk: vv for kk, vv in v.items() if kk != "state_uid"}
        fp[k] = v
    digest = hashlib.sha256(
        json.dumps(fp, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return digest, fp


def _model_config(model):
    """归一化模型输入的“配置”形态，兼容：

    - 懒代理（QwenTE）：读 _ryh_config；
    - 真实模型对象（QwenTE）：读 settings；
    - 纯配置 dict / list / str（XB 本地 config 或在线 API JSON）：原样返回。
    无法识别时返回 None。
    """
    cfg = getattr(model, "_ryh_config", None)
    if cfg is None:
        cfg = getattr(model, "settings", None)
    if cfg is None and isinstance(model, (dict, list, str)):
        cfg = model
    return cfg


def _model_fingerprint(model):
    """模型配置指纹哈希；无法识别时返回 "unknown-model"。"""
    cfg = _model_config(model)
    if cfg is None:
        return "unknown-model"
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _model_name(model):
    """尽力提取模型名供 meta 人工核对：本地取 GGUF 文件名，在线 API 取
    provider/model；提取不到时返回 None（meta 中省略该字段）。"""
    cfg = _model_config(model)
    if isinstance(cfg, str):
        # XB 在线 API 配置是 JSON 字符串
        try:
            cfg = json.loads(cfg)
        except (ValueError, TypeError):
            return None
    if isinstance(cfg, dict):
        name = cfg.get("model")
        if name:
            provider = cfg.get("provider")
            return f"{provider}/{name}" if provider else str(name)
    return None


def _images_paths(kwargs, image_keys):
    """收集图片 tensor 上挂载的来源路径（由本项目 LoadImageAtFolder 写入）。

    仅作备份记录写入缓存 meta，不参与 key 计算；tensor 没有该属性时跳过。
    """
    paths = []
    for name in image_keys:
        t = kwargs.get(name)
        if t is None:
            continue
        p = getattr(t, "ryh_source_path", None)
        if p:
            paths.append(p)
    return paths


def build_key_parts(kwargs, target):
    """返回 (完整key, meta字典)。meta 写入缓存条目，便于人工核对与排查 miss。

    meta 结构（三段均为对象）：
    - image：{ hash: 内容哈希, paths: [来源文件路径, ...] }（paths 由
      LoadImageAtFolder 挂载，无则省略该字段）；
    - params：参与指纹的参数字典原样留档，并含 hash 键（params 指纹哈希）；
    - model：{ hash: 配置指纹, name: 模型名 }（name 供人工核对，提取不到
      时省略该字段）。
    注意：key 的计算方式与此处 meta 无关，仍为 image哈希|params哈希|model哈希。
    """
    image_hash = _images_hash(kwargs, target["image_keys"])
    params_hash, params_raw = _params_fingerprint(kwargs, target["skip_keys"])
    model = kwargs.get(target["model_key"])
    model_hash = _model_fingerprint(model)
    raw = "|".join((image_hash, params_hash, model_hash))

    image_section = {"hash": image_hash}
    paths = _images_paths(kwargs, target["image_keys"])
    if paths:
        image_section["paths"] = paths
    meta = {"image": image_section}
    # 保证可 JSON 落盘：与指纹哈希相同的序列化口径做一次 round-trip
    try:
        params_section = json.loads(
            json.dumps(params_raw, sort_keys=True, ensure_ascii=False, default=str)
        )
    except (TypeError, ValueError):
        params_section = {k: str(v) for k, v in params_raw.items()}
    params_section["hash"] = params_hash
    meta["params"] = params_section
    model_section = {"hash": model_hash}
    name = _model_name(model)
    if name:
        model_section["name"] = name
    meta["model"] = model_section
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), meta


def build_key(kwargs, target):
    return build_key_parts(kwargs, target)[0]


# ---------------------------------------------------------------------------
# 懒加载代理：让“模型加载器”节点先返回占位对象，真正加载推迟到首次访问
# ---------------------------------------------------------------------------

# 懒代理的属性访问策略（白名单）：
# ComfyUI 内部多处会对节点输出做鸭子类型探测（如 model_patcher.PromptModelTracker
# 的 getattr(output, "patcher", None)、caching.all_outputs_dynamic 的
# hasattr(output, "is_dynamic")）。真实模型对象上并不存在这些属性，探测本应拿到
# 默认值；若让它们走到 resolve()，就会在“缓存命中、本不该加载”时白白触发 GGUF
# 进显存。因此：未解析的代理只允许访问真实模型类上确定存在的属性（dataclass 字段
# + 类方法/dunder），其余一律抛 AttributeError，使探测安全地返回默认值。
# 一旦代理已解析（模型已真实加载），则无条件委托，行为与原版一致。

class _LazyQwenModel:
    """包装 comfyui-llama-TE 的模型对象，延迟到真正需要时才加载 GGUF。

    - 缓存命中路径：`run` 里只读取 `_ryh_config`（普通实例属性，不触发加载）后
      直接返回，代理永远不会被解析，模型不进显存。
    - 第三方消费者（多轮对话等）访问 `llm`/`settings` 等真实字段：自动解析并委托。
    - ComfyUI 记账/回收的探测属性（`patcher`/`is_dynamic` 等）：不解析、抛
      AttributeError，探测拿到默认值。
    """

    def __init__(self, config, storage_cls, real_loader, prev_model=None, real_cls=None):
        self.__dict__["_ryh_config"] = config
        self.__dict__["_ryh_storage_cls"] = storage_cls
        self.__dict__["_ryh_real_loader"] = real_loader
        self.__dict__["_ryh_real"] = None
        # 创建本代理时全局正在使用的真实模型（可能是 None）。
        # resolve 时把它放回 cls.model，让原 load 决定复用还是先 unload，
        # 避免“代理覆盖 cls.model 后旧模型失去引用而泄漏显存”。
        self.__dict__["_ryh_prev"] = prev_model
        # 允许触发解析的属性集合：仅真实模型类的 dataclass 字段（如 llm/settings/
        # chat_handler）。不纳入 dir() 的 dunder/继承名，避免 str()/repr/序列化等
        # 探测误触发加载。拿不到真实类时退回到已知字段名，避免白名单为空导致
        # 合法消费者（多轮对话等）拿不到模型。
        allowed = set(getattr(real_cls, "__annotations__", ()))
        self.__dict__["_ryh_allowed"] = allowed or {"llm", "settings", "chat_handler"}

    def resolve(self):
        """真正加载并返回底层模型对象（幂等，且与 _QwenStorage.model 保持同步）。"""
        cls = self.__dict__["_ryh_storage_cls"]
        cfg = self.__dict__["_ryh_config"]
        real = self.__dict__["_ryh_real"]
        cur = cls.model
        # 已解析且仍是全局当前模型：直接复用
        if real is not None and cur is real:
            return real
        # 诊断：走到这里说明确实要触发真实加载，打印调用栈方便定位触发者
        import traceback

        frames = traceback.extract_stack()[:-2]
        tail = " <- ".join(f"{fr.filename.rsplit(os.sep, 1)[-1]}:{fr.lineno} {fr.name}" for fr in frames[-4:])
        print(f"{LOG_PREFIX} [诊断] 代理被解析，触发真实模型加载。调用链: {tail}", flush=True)
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
        # _ryh_* 都在 __dict__ 中，不会走到这里。
        # 未解析状态：只有真实模型的 dataclass 字段（llm/settings/chat_handler）才
        # 触发加载——这是多轮对话等真实消费者的访问方式。ComfyUI 内部对节点输出的
        # 鸭子探测（patcher / get_models / is_dynamic 等）不属于这些字段，一律抛
        # AttributeError 让探测安全拿到默认值，从而“缓存命中时不加载模型”。
        if name in self.__dict__["_ryh_allowed"]:
            return getattr(self.resolve(), name)
        # 已解析（模型确已加载）：无条件委托真实对象，行为与原版一致。真实对象
        # 没有的属性（含上述探测名）自然抛 AttributeError，不会二次加载。
        real = self.__dict__["_ryh_real"]
        if real is not None:
            return getattr(real, name)
        raise AttributeError(name)


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
#
# skip_keys 的判定原则：凡是不影响“输出文本”的输入都不进 params 指纹。
# 它们要么由 key 的另外两段单独表示（图片→内容哈希、模型→配置指纹），
# 要么是纯副作用/执行元数据（卸载、种子、队列 id 等），变化了也不代表
# 需要重新反推。
_QWEN_KEY_CFG = {
    "image_keys": ("图片", "图片2", "图片3", "图片4", "图片5", "图片6", "图片7", "图片8"),
    "model_key": "qwen模型",
    "skip_keys": {
        "qwen模型",  # 模型输入：由 model 段（配置指纹）单独表示；且它是代理/模型
                     # 对象，不可 JSON 序列化，混进 params 会退化成 repr 哈希
        "生成后自动卸载模型",  # 纯副作用：跑完是否释放显存，不改变输出文本
        "seed",  # 随机种子：缓存语义=同图同提示词复用首次结果；ComfyUI 种子控件
                 # 默认“生成后递增/随机”，若参与 key 则每次队列必 miss
        "图片", "图片2", "图片3", "图片4",  # 图片输入：由 image 段（内容哈希）单独
        "图片5", "图片6", "图片7", "图片8",  # 表示，且 tensor 不可 JSON 序列化
    },
}

_XB_KEY_CFG = {
    "image_keys": ("images",),
    "model_key": "llama_model",
    "skip_keys": {
        "llama_model",  # 模型输入：由 model 段单独表示（本地 config dict 或在线
                        # API JSON 的哈希）
        "images",  # 图片输入：由 image 段（逐帧内容哈希）单独表示
        "seed",  # 同 QwenTE：种子随机化不应让缓存失效
        "force_offload",  # 纯副作用：推理后是否卸载模型，不改变输出文本
        "save_states",  # 不参与缓存路径：True 时输出依赖会话历史，cached_process
                        # 直接放行原逻辑；能进 key 的恒为 False，属常量
        "queue_handler",  # 执行元数据：仅控制 XB 队列里的执行顺序，与文本无关
        "unique_id",  # ComfyUI 每次队列分配的运行 id（如 "12.3"），逐次变化；
                      # 只影响 state_uid 输出，命中时按原规则重算即可
        # 注：parameters 字典里嵌套的 state_uid 同样不影响文本，由
        # _params_fingerprint 统一剔除，不在此集合内。
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
    # 真实模型类：其 dataclass 注解字段即“允许触发加载”的属性白名单。
    real_cls = getattr(mod, "_QwenModel", None)

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
        proxy = _LazyQwenModel(
            config, cls, lambda cfg: original_storage_load(cls, cfg), prev, real_cls=real_cls
        )
        cls.model = proxy
        return proxy

    storage.load = classmethod(lazy_load)

    # 2) 包装 _QwenStorage.unload：原实现第一行就访问 cls.model.llm 来 close。
    #    若此时 cls.model 仍是未解析的懒代理（例如工作流里的“卸载显存模型”节点
    #    在缓存命中运行中被执行），访问 .llm 会触发 resolve，出现“为卸载而加载”
    #    的反效果。这里先把代理还原成它背后真正持有（或曾持有）的模型再卸载；
    #    从未解析过的代理没有真实模型可卸，置 None 后原 unload 自然变成空操作。
    original_storage_unload = storage.unload.__func__

    def safe_unload(cls):
        cur = cls.model
        if isinstance(cur, _LazyQwenModel):
            cls.model = cur._ryh_real if cur._ryh_real is not None else cur._ryh_prev
        original_storage_unload(cls)

    storage.unload = classmethod(safe_unload)

    # 3) 包装 QwenTE图像推理.run：命中直接返回；未命中先解析代理再调原方法
    original_run = infer_cls.run

    def cached_run(self, *args, **kwargs):
        # ComfyUI 以关键字参数调用 FUNCTION，正常 kwargs 已含全部输入
        if "qwen模型" not in kwargs:
            return original_run(self, *args, **kwargs)
        model = kwargs.get("qwen模型")
        key, meta = build_key_parts(kwargs, _QWEN_KEY_CFG)
        cached = get(key)
        if cached is not None:
            print(f"{LOG_PREFIX} [QwenTE] 命中缓存，跳过反推（模型未加载）。", flush=True)
            return (cached,)

        # 未命中：把懒代理解析成真实模型，避免原 run 内部触发二次加载
        if isinstance(model, _LazyQwenModel):
            kwargs["qwen模型"] = model.resolve()

        result = original_run(self, *args, **kwargs)
        if isinstance(result, tuple) and len(result) == 1 and isinstance(result[0], str):
            put(key, result[0], meta=meta)
            print(f"{LOG_PREFIX} [QwenTE] 未命中，已写入缓存。", flush=True)
        return result

    infer_cls.run = cached_run
    setattr(infer_cls, _PATCHED_FLAG, True)
    print(f"{LOG_PREFIX} 已为 comfyui-llama-TE 安装反推缓存 patch。", flush=True)
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

        key, meta = build_key_parts(kwargs, _XB_KEY_CFG)
        cached = get(key)
        if cached is not None:
            params = kwargs.get("parameters") or {}
            uid = params.get("state_uid", None)
            if uid in (None, -1):
                uid = str(kwargs.get("unique_id", "0")).rpartition(".")[-1]
            print(f"{LOG_PREFIX} [XB_llama] 命中缓存，跳过反推（模型未加载）。", flush=True)
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
                meta=meta,
            )
            print(f"{LOG_PREFIX} [XB_llama] 未命中，已写入缓存。", flush=True)
        return result

    infer_cls.process = cached_process
    setattr(infer_cls, _PATCHED_FLAG, True)
    print(f"{LOG_PREFIX} 已为 XB_ToolBox 安装反推缓存 patch。", flush=True)
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
                print(f"{LOG_PREFIX} 未检测到 {target['name']}，跳过该目标。", flush=True)
                continue
            if target["install"](mod):
                applied = True
        except Exception as e:
            print(f"{LOG_PREFIX} {target['name']} patch 安装失败，已跳过: {e}", flush=True)
    return applied
