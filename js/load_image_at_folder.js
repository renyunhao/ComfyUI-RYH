import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ============================================================
// ComfyUI-RYH · LoadImageAtFolder 前端扩展
// 功能：
//  1. [📁 目录] 按钮弹窗选择目录（或直接在输入框填写路径）
//  2. 下拉列表选择一张图片；◀ ▶ 按钮快速切换上/下一张；支持 none（不选）
//  3. 节点内预览，在节点当前视图范围内等比缩放（object-fit: contain）
// ============================================================

const EXTENSION_NAME = "ComfyUI-RYH.LoadImageAtFolder";
const NODE_CLASS = "LoadImageAtFolder";

const isZH = navigator.language.startsWith("zh");

// ---------- 通用小工具 ----------

function chainCallback(object, property, callback) {
    if (property in object && object[property]) {
        const orig = object[property];
        object[property] = function (...args) {
            const r = orig.apply(this, args);
            return callback.apply(this, args) ?? r;
        };
    } else {
        object[property] = callback;
    }
}

// 允许从 DOM 预览区域拖拽移动节点
function allowDragFromWidget(widget) {
    widget.onPointerDown = function (pointer, node) {
        pointer.onDragStart = () => {
            app.canvas.emitBeforeChange();
            app.canvas.graph?.beforeChange();
            pointer.finally = () => {
                app.canvas.isDragging = false;
                app.canvas.graph?.afterChange();
                app.canvas.emitAfterChange();
            };
            app.canvas.processSelect(node, pointer.eDown, true);
            app.canvas.isDragging = true;
        };
        pointer.onDragEnd = (e) => {
            if (e.shiftKey || LiteGraph.alwaysSnapToGrid)
                app.canvas?.graph?.snapToGrid(app.canvas.selectedItems);
            app.canvas.dirty_canvas = true;
            app.canvas.dirty_bgcanvas = true;
            app.canvas.onNodeMoved?.(app.canvas.selectedItems.find((n) => n));
        };
        app.canvas.dirty_canvas = true;
        return true;
    };
}

// ---------- 节点 UI ----------

function setupNode(node) {
    if (node._ryhSetupDone) return;

    const folderWidget = node.widgets?.find((w) => w.name === "folder");
    const imageWidget = node.widgets?.find((w) => w.name === "image");
    if (!folderWidget || !imageWidget) {
        // 个别前端版本 widget 创建时序不同，稍后重试
        setTimeout(() => setupNode(node), 100);
        return;
    }
    node._ryhSetupDone = true;

    node._ryhImages = [];
    node._ryhFolderWidget = folderWidget;
    node._ryhImageWidget = imageWidget;

    // —— 预览区（DOM widget，可伸缩、随节点高度变化）——
    // 高度交给布局引擎自动分配：不覆盖 computeSize，保留内置 computeLayoutSize，
    // 布局引擎会把节点“剩余高度”分配给它；--comfy-widget-min-height 保证最小高度。
    const container = document.createElement("div");
    container.style.cssText =
        "width:100%;height:100%;display:flex;align-items:center;justify-content:center;overflow:hidden;background:rgba(0,0,0,0.25);--comfy-widget-min-height:60;";
    container.hidden = true;
    const img = document.createElement("img");
    img.style.cssText = "display:block;max-width:100%;max-height:100%;object-fit:contain;";
    img.alt = "";
    container.appendChild(img);

    const previewWidget = node.addDOMWidget("ryh_preview", "preview", container, {
        serialize: false,
        hideOnZoom: false,
    });
    allowDragFromWidget(previewWidget);
    node._ryhPreview = { container, img, widget: previewWidget };

    // —— 控制按钮栏：◀ ▶ 计数 [📁 目录] ——
    const bar = document.createElement("div");
    bar.style.cssText = "display:flex;gap:4px;align-items:center;width:100%;padding:2px 0;";
    const makeBtn = (label, cb) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.title = label;
        b.style.cssText = "flex:1;padding:3px 0;font-size:12px;cursor:pointer;line-height:1;";
        b.addEventListener("click", (e) => {
            e.stopPropagation();
            cb();
        });
        bar.appendChild(b);
        return b;
    };
    makeBtn("◀", () => stepImage(node, -1));
    makeBtn("▶", () => stepImage(node, 1));
    const counter = document.createElement("span");
    counter.style.cssText = "font-size:11px;color:#aaa;padding:0 2px;white-space:nowrap;";
    counter.textContent = "0/0";
    bar.appendChild(counter);
    makeBtn(isZH ? "📁 目录" : "📁 Folder", () => browseFolder(node));

    const controlsWidget = node.addDOMWidget("ryh_controls", "controls", bar, {
        serialize: false,
        hideOnZoom: false,
    });
    controlsWidget.computeSize = function (width) {
        return [width, 28];
    };
    node._ryhCounter = counter;

    // —— 目录变化 → 刷新图片列表 ——
    chainCallback(folderWidget, "callback", function (value) {
        if (value !== node._ryhLastFolder) refreshImageList(node);
    });

    // —— 下拉选择变化 → 刷新预览 ——
    chainCallback(imageWidget, "callback", function () {
        updatePreview(node);
    });

    refreshImageList(node);
}

async function refreshImageList(node) {
    const folder = node._ryhFolderWidget?.value ?? "";
    node._ryhLastFolder = folder;

    let images = [];
    try {
        const res = await api.fetchApi("/ryh/list_images", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder }),
        });
        if (res.ok) {
            const data = await res.json();
            images = Array.isArray(data.images) ? data.images : [];
        }
    } catch (e) {
        console.warn("[LoadImageAtFolder] 获取图片列表失败", e);
    }
    // 期间目录已变更，丢弃过期结果
    if (node._ryhFolderWidget?.value !== folder) return;

    node._ryhImages = images;

    const w = node._ryhImageWidget;
    if (w.options) w.options.values = ["none", ...images];
    if (!(w.value === "none" || images.includes(w.value))) {
        w.value = "none";
    }
    updateCounter(node);
    node.setDirtyCanvas?.(true, true);
    await updatePreview(node);
}

function updateCounter(node) {
    const imgs = node._ryhImages || [];
    const cur = node._ryhImageWidget?.value;
    const idx = cur && cur !== "none" ? imgs.indexOf(cur) : -1;
    if (node._ryhCounter) {
        node._ryhCounter.textContent = `${idx === -1 ? 0 : idx + 1}/${imgs.length}`;
    }
}

function stepImage(node, dir) {
    const w = node._ryhImageWidget;
    const imgs = node._ryhImages || [];
    if (!imgs.length) {
        w.value = "none";
        node.setDirtyCanvas?.(true, true);
        updateCounter(node);
        w.callback?.(w.value);
        return;
    }
    let idx = imgs.indexOf(w.value);
    if (idx === -1) idx = dir > 0 ? -1 : 0;
    const ni = (idx + dir + imgs.length) % imgs.length;
    w.value = imgs[ni];
    node.setDirtyCanvas?.(true, true);
    updateCounter(node);
    w.callback?.(w.value); // 触发上面挂接的回调 → 刷新预览
}

async function updatePreview(node) {
    const pv = node._ryhPreview;
    if (!pv) return;
    const folder = node._ryhFolderWidget?.value ?? "";
    const image = node._ryhImageWidget?.value ?? "none";

    if (!image || image === "none") {
        pv.img.removeAttribute("src");
        pv.container.style.display = "none";
        pv.container.hidden = true;
        return;
    }
    try {
        const url = `/ryh/image?folder=${encodeURIComponent(folder)}&image=${encodeURIComponent(image)}&t=${Date.now()}`;
        const res = await api.fetchApi(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        // 期间选择已变更，丢弃过期结果
        if (node._ryhImageWidget?.value !== image || node._ryhFolderWidget?.value !== folder) return;
        const blob = await res.blob();
        if (pv._objUrl) URL.revokeObjectURL(pv._objUrl);
        pv._objUrl = URL.createObjectURL(blob);
        pv.img.src = pv._objUrl;
        pv.container.style.display = "flex";
        pv.container.hidden = false;
    } catch (e) {
        pv.img.removeAttribute("src");
        pv.container.style.display = "none";
        pv.container.hidden = true;
    }
}

// 可见地提示错误（优先 ComfyUI toast，失败则用浏览器弹窗）
function showError(msg) {
    console.warn("[LoadImageAtFolder]", msg);
    try {
        const toast = window.app?.extensionManager?.toast;
        if (toast && typeof toast.add === "function") {
            toast.add({ severity: "error", summary: "LoadImageAtFolder", detail: msg, life: 6000 });
            return;
        }
    } catch (e) {
        /* 忽略 toast 兼容问题，回退弹窗 */
    }
    try {
        window.alert(msg);
    } catch (e) {
        /* ignore */
    }
}

async function browseFolder(node) {
    try {
        const res = await api.fetchApi("/ryh/choose_folder", { method: "POST" });
        const data = await res.json();
        if (data?.path) {
            node._ryhFolderWidget.value = data.path;
            node.setDirtyCanvas?.(true, true);
            await refreshImageList(node);
        } else if (data?.error) {
            showError(data.error);
        }
    } catch (e) {
        console.warn("[LoadImageAtFolder] 目录选择失败", e);
    }
}

// ---------- 注册扩展 ----------

app.registerExtension({
    name: EXTENSION_NAME,

    // 新前端官方钩子：节点实例创建时调用（菜单新建 / 工作流加载均会触发）
    nodeCreated(node) {
        if (node.comfyClass === NODE_CLASS) setupNode(node);
        return node;
    },

    // 工作流中节点配置完成（widget 值已恢复）后调用 —— 刷新图片列表的最可靠时机
    loadedGraphNode(node) {
        if (node.comfyClass !== NODE_CLASS) return node;
        setupNode(node);
        refreshImageList(node);
        return node;
    },

    // 工作流加载完成后再兜底刷新一次。
    // 注意：该钩子的第一个参数是 missingNodeTypes 数组，不是 graph！
    afterConfigureGraph() {
        const g = app.graph;
        for (const node of g?._nodes ?? []) {
            if (node.comfyClass !== NODE_CLASS) continue;
            setupNode(node);
            refreshImageList(node);
        }
    },
});
