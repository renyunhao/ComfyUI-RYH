"""RYH 共用：定位「已被 ComfyUI 加载的」ComfyUI_MiniMaxH3_Director 模块对象 + 每次运行的前置钩子。

两个坑：
1. ComfyUI 的 load_custom_node 用 sys_module_name = 模块路径.replace(".", "_x_") 注册包
   （nodes.py:2258-2272），所以按裸名 import_module("ComfyUI_MiniMaxH3_Director...")
   会另建一个重复的模块对象，补丁打在孤立对象上、对真正注册的节点类毫无作用。
2. custom_nodes 的加载顺序是 os.listdir 的原始顺序（nodes.py:2372），不保证谁先谁后，
   所以既可能「导演台还没加载」，也可能「已经加载完」。
"""

from __future__ import annotations

import inspect
import logging

log = logging.getLogger("RYH.director_ref")

PKG = "ComfyUI_MiniMaxH3_Director"
HOOK_MARK = "_ryh_run_hook"
_HOOKS: list = []
_warned_missing = False


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/")


def _scan(sub: str = ""):
    import sys

    rel = sub.replace(".", "/")
    want = f"/{PKG}/__init__.py" if not rel else f"/{PKG}/{rel}.py"
    hits = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if not _norm(getattr(mod, "__file__", "")).endswith(want):
            continue
        hits.append((name, mod))
    if not hits:
        return None
    # 优先 ComfyUI 加载器注册的那个（键以路径派生，不是裸包名）
    for name, mod in hits:
        if not name.startswith(f"{PKG}.") and name != PKG:
            return mod
    return hits[0][1]


def find(sub: str = ""):
    """sub 例 "nodes.director" / "director.plan"；空 = 包根本身。

    子模块可能是懒加载的（还没进 sys.modules）——这时用**包根在 sys.modules 里的
    真实键**去 import，Python 会走它的 __path__ 解析，注册的还是同一套键，
    不会像按裸名 import 那样造出重复的包实例。
    """
    import importlib

    module = _scan(sub)
    if module is not None or not sub:
        return module
    root = _scan("")
    if root is None:
        return None
    try:
        return importlib.import_module(f"{root.__name__}.{sub}")
    except Exception as error:
        log.warning("RYH: 导入 %s.%s 失败 (%s)", root.__name__, sub, error)
        return None


def loaded() -> bool:
    return find() is not None


def warn_missing_once(context: str) -> None:
    global _warned_missing
    if _warned_missing:
        return
    _warned_missing = True
    log.warning("RYH: %s 找不到已加载的 %s —— 服务启动前与每次 Queue 前会各重试一次", context, PKG)


def add_startup_hook(fn) -> bool:
    """在 HTTP 服务启动前装补丁 —— 那时所有 custom_nodes 都已加载完，
    而前端还没请求 /object_info，所以新输入口第一次打开页面就在。

    注意：aiohttp 的 on_startup 是 aiosignal，会 await 每个回调的返回值，
    所以必须注册协程函数且不能返回非 awaitable（本包 __init__.py:149 就是这么做的）。
    """
    try:
        from server import PromptServer

        app = getattr(getattr(PromptServer, "instance", None), "app", None)
        if app is None:
            raise RuntimeError("PromptServer.instance.app 不可用")

        async def _hook(_app, _fn=fn):
            _run(_fn)

        app.on_startup.append(_hook)
        return True
    except Exception as error:
        log.warning("RYH: on_startup 注册失败，改为立即尝试 (%s)", error)
        return _run(fn)


def _accepts_positional(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is p.VAR_POSITIONAL for p in params.values()):
        return True
    return sum(1 for p in params.values()
               if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)) >= 1


def _run(fn, arg=None):
    try:
        fn(arg) if _accepts_positional(fn) else fn()
        return True
    except Exception as error:
        log.warning("RYH: 钩子 %s 失败 (%s)", getattr(fn, "__name__", fn), error)
        return False


def add_run_hook(fn) -> bool:
    """把 fn 挂到每次执行开始前（本版本 ComfyUI 没有 add_onEventHandler 事件 API）。"""
    if fn not in _HOOKS:
        _HOOKS.append(fn)
    try:
        import execution
    except Exception as error:
        log.warning("RYH: 无法导入 execution，运行钩子未安装 (%s)", error)
        return False
    cls = getattr(execution, "PromptExecutor", None)
    raw = getattr(cls, "execute", None) if cls is not None else None
    if raw is None:
        log.warning("RYH: 找不到 PromptExecutor.execute，运行钩子未安装")
        return False
    if getattr(raw, HOOK_MARK, False):
        return True

    def wrapper(self, *args, **kwargs):
        # args[0] 就是本次 Queue 的 prompt 字典 —— 钩子需要在缓存判定之前看到它
        prompt = args[0] if args else None
        for hook in list(_HOOKS):
            _run(hook, prompt)
        return raw(self, *args, **kwargs)

    setattr(wrapper, HOOK_MARK, True)
    cls.execute = wrapper
    log.info("RYH: 运行前置钩子已挂到 execution.PromptExecutor.execute")
    return True
