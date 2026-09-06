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
const PREVIEW_MAX_H = 280; // 预览区最大高度（节点宽度不变时，超高图片等比缩放上限）

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

function fitHeight(node) {
    node.setSize([node.size[0], node.computeSize([node.size[0], node.size[1]])[1]]);
    node?.graph?.setDirtyCanvas(true);
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

    // —— 预览区（DOM widget，等比缩放）——
    const container = document.createElement("div");
    container.style.cssText =
        "width:100%;height:100%;display:flex;align-items:center;justify-content:center;overflow:hidden;background:rgba(0,0,0,0.25);";
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
    previewWidget.computeSize = function (width) {
        if (container.hidden || !img.naturalWidth) return [width, -4];
        const ratio = img.naturalWidth / img.naturalHeight;
        const h = Math.min((node.size[0] - 20) / ratio + 10, PREVIEW_MAX_H + 10);
        return [width, Math.max(h, 40)];
    };
    img.onload = () => fitHeight(node);
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
        fitHeight(node);
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
    fitHeight(node);
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
            console.warn("[LoadImageAtFolder]", data.error);
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

    // 兼容旧前端：onNodeCreated 原型钩子
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_CLASS) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function (...args) {
            const r = onNodeCreated ? onNodeCreated.apply(this, args) : undefined;
            setupNode(this);
            return r;
        };
    },

    // 工作流加载完成（widget 值已恢复）后再刷新一次，确保目录/图片正确
    afterConfigureGraph(graph) {
        const g = graph || app.graph;
        for (const node of g?._nodes ?? []) {
            if (node.comfyClass !== NODE_CLASS) continue;
            setupNode(node);
            refreshImageList(node);
        }
    },
});
