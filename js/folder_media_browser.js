import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ============================================================
// ComfyUI-RYH · 目录媒体浏览器（Load Image / Video / Audio At Folder 共用）
// 目录选择、◀ ▶ 切换、缩略图画廊、尺寸滑块、预览播放与删除都实现在这里；
// 三个节点各自只提供 nodeClass / kind / 文案，行为保持一致。
// ============================================================

const isZH = navigator.language.startsWith("zh");

const THUMB_SIZE_MIN = 32;
const THUMB_SIZE_MAX = 256;
const THUMB_SIZE_STEP = 8;
const THUMB_SIZE_DEFAULT = 96;
const THUMB_SIZE_STORE_KEY = "ComfyUI-RYH.folderMedia.thumbSize";

const LABEL = {
    gallery: isZH ? "▦ 画廊" : "▦ Gallery",
    preview: isZH ? "🖼 预览" : "🖼 Preview",
    folder: isZH ? "📁 目录" : "📁 Folder",
    remove: isZH ? "🗑 删除" : "🗑 Delete",
    thumb: isZH ? "缩略图" : "Thumb",
    galleryHint: isZH ? "单击选中，◀ ▶ 切换" : "Click to select",
};

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
function allowDragFromWidget(widget, allow) {
    widget.onPointerDown = function (pointer, node) {
        // 返回 false 表示不接管这次 pointerdown：画廊里缩略图的点击、
        // 尺寸滑块的拖动、以及 <video>/<audio> 的原生控制条都必须走原生交互。
        if (allow && !allow(pointer, node)) return false;
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

// 缩略图尺寸：滑块档位少一些，服务端/浏览器缓存命中率更高
function clampThumbSize(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return THUMB_SIZE_DEFAULT;
    const clamped = Math.min(THUMB_SIZE_MAX, Math.max(THUMB_SIZE_MIN, num));
    return Math.round(clamped / THUMB_SIZE_STEP) * THUMB_SIZE_STEP;
}

// 节点 properties 随工作流保存但不进 prompt，正好适合纯显示参数；
// localStorage 兜底，让新建节点沿用上次用的尺寸。
function readThumbSize(node) {
    return clampThumbSize(node.properties?.ryh_thumb_size ?? localStorage.getItem(THUMB_SIZE_STORE_KEY));
}

function saveThumbSize(node, size) {
    node.setProperty?.("ryh_thumb_size", size);
    localStorage.setItem(THUMB_SIZE_STORE_KEY, String(size));
}

function apiUrl(path) {
    return api.apiURL ? api.apiURL(path) : path;
}

function thumbnailUrl(folder, name, size) {
    const params = new URLSearchParams({ folder, file: name, size: String(size) });
    return apiUrl(`/ryh/thumbnail?${params}`);
}

// v=mtime：文件被替换后 URL 变化让浏览器重新取，同时命中缓存时又能省下整包下载
function mediaUrl(folder, name, mtime) {
    const params = new URLSearchParams({ folder, file: name, v: String(mtime ?? 0) });
    return apiUrl(`/ryh/media?${params}`);
}

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return "";
    const total = Math.round(seconds);
    if (total < 60) return `${total}s`;
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function formatMeta(kind, file) {
    if (kind === "image") {
        return file.width && file.height ? `${file.width}×${file.height}` : "";
    }
    const time = formatDuration(file.duration);
    if (kind === "video") {
        return [time, file.frame_count ? `${file.frame_count}f` : ""].filter(Boolean).join(" ");
    }
    const rate = file.sample_rate ? `${Math.round(file.sample_rate / 100) / 10}k` : "";
    return [time, rate].filter(Boolean).join(" ");
}

// ---------- 节点 UI ----------

function setupNode(node, config) {
    if (node._ryhSetupDone) return;

    const folderWidget = node.widgets?.find((w) => w.name === "folder");
    const fileWidget = node.widgets?.find((w) => w.name === config.fileWidget);
    if (!folderWidget || !fileWidget) {
        // 个别前端版本 widget 创建时序不同，稍后重试
        setTimeout(() => setupNode(node, config), 100);
        return;
    }
    node._ryhSetupDone = true;
    node._ryhConfig = config;
    node._ryhFiles = [];
    node._ryhFolderWidget = folderWidget;
    node._ryhFileWidget = fileWidget;
    node._ryhMode = node.properties?.ryh_view_mode === "gallery" ? "gallery" : "preview";
    node._ryhThumbSize = readThumbSize(node);

    // —— 控制按钮栏：◀ ▶ 计数 [▦ 画廊] [🗑 删除] [📁 目录] ——
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
    makeBtn("◀", () => stepFile(node, -1));
    makeBtn("▶", () => stepFile(node, 1));
    const counter = document.createElement("span");
    counter.style.cssText = "font-size:11px;color:#aaa;padding:0 2px;white-space:nowrap;";
    counter.textContent = "0/0";
    bar.appendChild(counter);
    const modeBtn = makeBtn("", () => setMode(node, node._ryhMode === "gallery" ? "preview" : "gallery"));
    modeBtn.style.flex = "0 0 auto";
    node._ryhModeBtn = modeBtn;
    const removeBtn = makeBtn(LABEL.remove, () => removeFile(node));
    removeBtn.style.color = "#e06c6c";
    node._ryhRemoveBtn = removeBtn;
    makeBtn(LABEL.folder, () => browseFolder(node));

    const controlsWidget = node.addDOMWidget("ryh_controls", "controls", bar, {
        serialize: false,
        hideOnZoom: false,
    });
    controlsWidget.computeSize = function (width) {
        return [width, 28];
    };
    node._ryhCounter = counter;

    // —— 预览区 / 缩略图画廊（同一个 DOM widget，两种模式互斥显示）——
    // 高度交给布局引擎自动分配：不覆盖 computeSize，保留内置 computeLayoutSize，
    // 布局引擎会把节点“剩余高度”分配给它；--comfy-widget-min-height 保证最小高度。
    const root = document.createElement("div");
    root.style.cssText =
        "width:100%;height:100%;flex-direction:column;overflow:hidden;background:rgba(0,0,0,0.25);--comfy-widget-min-height:60;display:none;";

    const previewBox = document.createElement("div");
    previewBox.style.cssText = "flex:1;min-height:0;align-items:center;justify-content:center;overflow:hidden;display:none;";
    const mediaEl = createMediaElement(config.kind);
    previewBox.appendChild(mediaEl);

    const galleryBar = document.createElement("div");
    galleryBar.style.cssText = "flex:0 0 auto;align-items:center;gap:6px;padding:2px 4px;font-size:11px;color:#aaa;display:none;";
    const sizeCaption = document.createElement("span");
    sizeCaption.textContent = LABEL.thumb;
    const sizeSlider = document.createElement("input");
    sizeSlider.type = "range";
    sizeSlider.min = String(THUMB_SIZE_MIN);
    sizeSlider.max = String(THUMB_SIZE_MAX);
    sizeSlider.step = String(THUMB_SIZE_STEP);
    sizeSlider.value = String(node._ryhThumbSize);
    sizeSlider.title = isZH ? "缩略图大小" : "Thumbnail size";
    sizeSlider.style.cssText = "flex:1;min-width:40px;height:14px;cursor:pointer;accent-color:#6aa9ff;";
    const sizeValue = document.createElement("span");
    sizeValue.style.cssText = "width:38px;text-align:right;white-space:nowrap;";
    sizeValue.textContent = `${node._ryhThumbSize}px`;
    sizeSlider.addEventListener("input", () => {
        // 拖动过程中只改数字：每挪一档就重画整屏缩略图会明显掉帧
        sizeValue.textContent = `${clampThumbSize(sizeSlider.value)}px`;
    });
    sizeSlider.addEventListener("change", () => {
        const size = clampThumbSize(sizeSlider.value);
        sizeSlider.value = String(size);
        sizeValue.textContent = `${size}px`;
        setThumbSize(node, size);
    });
    galleryBar.append(sizeCaption, sizeSlider, sizeValue);

    const gallery = document.createElement("div");
    // data-capture-wheel + 可聚焦：新版前端只有在「滚动容器里已有焦点」时才把
    // 滚轮留给 DOM，否则会被画布拿去做缩放，画廊就滚不动。
    gallery.dataset.captureWheel = "true";
    gallery.tabIndex = 0;
    gallery.style.cssText =
        "flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;padding:4px;outline:none;--ryh-thumb:96px;display:none;" +
        "grid-template-columns:repeat(auto-fill,var(--ryh-thumb));justify-content:center;gap:6px;align-content:start;";
    gallery.addEventListener("pointerdown", () => gallery.focus({ preventScroll: true }));

    root.append(previewBox, galleryBar, gallery);

    const mediaWidget = node.addDOMWidget("ryh_media", "preview", root, {
        serialize: false,
        hideOnZoom: false,
    });
    // 图片预览拖节点很顺手；视频/音频预览区要让给原生控制条（播放/进度）
    const draggablePreview = config.kind === "image";
    allowDragFromWidget(mediaWidget, () => draggablePreview && node._ryhMode === "preview");
    node._ryhMedia = {
        root,
        previewBox,
        mediaEl,
        gallery,
        galleryBar,
        sizeSlider,
        sizeValue,
        widget: mediaWidget,
        files: null,
        folder: null,
        size: null,
        cells: new Map(),
    };

    updateModeButton(node);

    // —— 目录变化 → 刷新文件列表 ——
    chainCallback(folderWidget, "callback", function (value) {
        if (value !== node._ryhLastFolder) refreshFileList(node);
    });

    // —— 下拉选择变化 → 刷新预览 ——
    chainCallback(fileWidget, "callback", function () {
        updatePreview(node);
    });

    refreshFileList(node);
}

function createMediaElement(kind) {
    if (kind === "video") {
        const el = document.createElement("video");
        el.controls = true;
        el.preload = "metadata";
        el.style.cssText = "display:block;width:100%;height:100%;object-fit:contain;background:#000;";
        return el;
    }
    if (kind === "audio") {
        const el = document.createElement("audio");
        el.controls = true;
        el.preload = "metadata";
        el.style.cssText = "display:block;width:100%;max-width:100%;";
        return el;
    }
    const el = document.createElement("img");
    el.style.cssText = "display:block;max-width:100%;max-height:100%;object-fit:contain;";
    el.alt = "";
    return el;
}

// 换文件或收起预览时调用：视频/音频不会因为 src 变化自动停止发声
function stopMedia(mediaEl) {
    if (mediaEl.pause) mediaEl.pause();
}

async function refreshFileList(node, preferred) {
    const folder = node._ryhFolderWidget?.value ?? "";
    node._ryhLastFolder = folder;

    let files = [];
    try {
        const res = await api.fetchApi("/ryh/list_media", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder, kind: node._ryhConfig.kind }),
        });
        if (res.ok) {
            const data = await res.json();
            files = Array.isArray(data.files) ? data.files : [];
        }
    } catch (e) {
        console.warn(`[${node._ryhConfig.nodeClass}] 获取文件列表失败`, e);
    }
    // 期间目录已变更，丢弃过期结果
    if (node._ryhFolderWidget?.value !== folder) return;

    node._ryhFiles = files;
    const names = files.map((f) => f.name);

    const w = node._ryhFileWidget;
    if (w.options) w.options.values = ["none", ...names];
    if (preferred && names.includes(preferred)) {
        w.value = preferred;
    } else if (!(w.value === "none" || names.includes(w.value))) {
        w.value = "none";
    }
    updateCounter(node);
    node.setDirtyCanvas?.(true, true);
    renderGallery(node);
    updatePreview(node);
}

function updateCounter(node) {
    const names = fileNames(node);
    const cur = node._ryhFileWidget?.value;
    const idx = cur && cur !== "none" ? names.indexOf(cur) : -1;
    if (node._ryhCounter) {
        node._ryhCounter.textContent = `${idx === -1 ? 0 : idx + 1}/${names.length}`;
    }
    if (node._ryhRemoveBtn) {
        node._ryhRemoveBtn.disabled = idx === -1;
    }
}

function fileNames(node) {
    return (node._ryhFiles || []).map((f) => f.name);
}

function findFile(node, name) {
    return (node._ryhFiles || []).find((f) => f.name === name);
}

async function removeFile(node) {
    const w = node._ryhFileWidget;
    const folder = node._ryhFolderWidget?.value ?? "";
    const name = w?.value ?? "none";
    if (!name || name === "none") return;
    const confirmed = window.confirm(
        isZH ? `确定删除文件 "${name}" ？\n该操作会直接从磁盘移除文件，不可恢复！`
             : `Delete file "${name}"?\nThis permanently removes the file from disk.`
    );
    if (!confirmed) return;
    // 删除后优先显示下一个，没有则显示上一个，都为空时回到 none
    const names = fileNames(node);
    const idx = names.indexOf(name);
    const preferred = idx === -1 ? "none" : (names[idx + 1] ?? names[idx - 1] ?? "none");
    try {
        const res = await api.fetchApi("/ryh/delete_file", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder, file: name }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            showError(node, data.error || `删除失败 (HTTP ${res.status})`);
            return;
        }
    } catch (e) {
        showError(node, `删除请求失败: ${e}`);
        return;
    }
    await refreshFileList(node, preferred);
}

// 下拉、◀ ▶、缩略图点击都走这里，保证状态更新只有一套逻辑
function applySelection(node, name) {
    const w = node._ryhFileWidget;
    if (!w || w.value === name) return;
    w.value = name;
    node.setDirtyCanvas?.(true, true);
    updateCounter(node);
    app.canvas.graph?.afterChange?.();
    w.callback?.(w.value); // 触发上面挂接的回调 → 刷新预览/画廊高亮
}

function stepFile(node, dir) {
    const names = fileNames(node);
    if (!names.length) {
        applySelection(node, "none");
        return;
    }
    const w = node._ryhFileWidget;
    let idx = names.indexOf(w.value);
    if (idx === -1) idx = dir > 0 ? -1 : 0;
    applySelection(node, names[(idx + dir + names.length) % names.length]);
}

function updatePreview(node) {
    const m = node._ryhMedia;
    if (!m) return;
    // 画廊模式只看缩略图，不下载原文件
    if (node._ryhMode === "gallery") {
        stopMedia(m.mediaEl);
        updateMediaVisibility(node);
        updateGallerySelection(node);
        return;
    }
    const folder = node._ryhFolderWidget?.value ?? "";
    const name = node._ryhFileWidget?.value ?? "none";
    if (!name || name === "none") {
        stopMedia(m.mediaEl);
        m.mediaEl.removeAttribute("src");
        updateMediaVisibility(node);
        return;
    }
    const file = findFile(node, name);
    const url = mediaUrl(folder, name, file?.mtime);
    // src 相同就别重新赋值：那会让正在播放的视频从头开始
    if (m.mediaEl.getAttribute("src") !== url) m.mediaEl.src = url;
    updateMediaVisibility(node);
}

function updateModeButton(node) {
    const btn = node._ryhModeBtn;
    if (!btn) return;
    btn.textContent = node._ryhMode === "gallery" ? LABEL.preview : LABEL.gallery;
    btn.title = btn.textContent;
}

// 预览 / 画廊互斥显示：画廊要有文件才有意义，预览要选中了文件才显示
function updateMediaVisibility(node) {
    const m = node._ryhMedia;
    if (!m) return;
    const galleryOn = node._ryhMode === "gallery" && (node._ryhFiles || []).length > 0;
    const name = node._ryhFileWidget?.value ?? "none";
    const previewOn = !galleryOn && name !== "none";
    m.root.style.display = galleryOn || previewOn ? "flex" : "none";
    m.gallery.style.display = galleryOn ? "grid" : "none";
    m.galleryBar.style.display = galleryOn ? "flex" : "none";
    m.previewBox.style.display = previewOn ? "flex" : "none";
    if (!previewOn) stopMedia(m.mediaEl);
}

function setMode(node, mode) {
    if (node._ryhMode === mode) return;
    node._ryhMode = mode;
    node.setProperty?.("ryh_view_mode", mode);
    updateModeButton(node);
    if (mode === "gallery") renderGallery(node);
    updateMediaVisibility(node);
    if (mode === "preview") updatePreview(node);
}

function setThumbSize(node, size) {
    node._ryhThumbSize = size;
    saveThumbSize(node, size);
    renderGallery(node);
}

// 缩略图画廊：列表变化时重建格子，只有尺寸变化时仅换 src
function renderGallery(node) {
    const m = node._ryhMedia;
    if (!m) return;
    const config = node._ryhConfig;
    const folder = node._ryhFolderWidget?.value ?? "";
    const files = node._ryhFiles || [];
    const size = node._ryhThumbSize || THUMB_SIZE_DEFAULT;

    if (m.files !== files || m.folder !== folder) {
        m.files = files;
        m.folder = folder;
        m.size = null; // 强制重刷一遍 src
        m.cells.clear();
        m.gallery.replaceChildren();
        for (const file of files) {
            const cell = buildCell(node, config, file);
            m.cells.set(file.name, cell);
            m.gallery.appendChild(cell);
        }
    }

    m.gallery.style.setProperty("--ryh-thumb", `${size}px`);
    if (m.size !== size) {
        m.size = size;
        for (const [name, cell] of m.cells) {
            cell.firstChild.src = thumbnailUrl(folder, name, size);
        }
    }
    updateGallerySelection(node);
}

function buildCell(node, config, file) {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.title = `${file.name}\n${LABEL.galleryHint}`;
    cell.style.cssText = "margin:0;padding:0;border:0;background:none;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:2px;";

    const thumb = document.createElement("img");
    thumb.alt = file.name;
    thumb.loading = "lazy";
    thumb.draggable = false;
    // 用 outline 而不是 border：高亮不改变盒子尺寸，选中时格子不会抖
    thumb.style.cssText =
        "display:block;width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:3px;" +
        "background:rgba(255,255,255,0.06);outline:2px solid transparent;outline-offset:-2px;";
    thumb.addEventListener("error", () => thumb.removeAttribute("src"));

    const caption = document.createElement("div");
    caption.textContent = formatMeta(config.kind, file);
    caption.style.cssText = "font-size:10px;line-height:1;color:#9ab;white-space:nowrap;overflow:hidden;max-width:100%;";

    cell.append(thumb, caption);
    cell.addEventListener("click", (e) => {
        e.stopPropagation();
        applySelection(node, file.name);
    });
    return cell;
}

function updateGallerySelection(node) {
    const m = node._ryhMedia;
    if (!m?.cells.size) return;
    const current = node._ryhFileWidget?.value;
    for (const [name, cell] of m.cells) {
        cell.firstChild.style.outlineColor = name === current ? "#6aa9ff" : "transparent";
    }
    const selected = m.cells.get(current);
    // ◀ ▶ 切换时把选中项带进视野；nearest 在已可见时不动，不会误滚画布
    if (selected && m.gallery.style.display !== "none") {
        selected.scrollIntoView({ block: "nearest" });
    }
}

// 可见地提示错误（优先 ComfyUI toast，失败则用浏览器弹窗）
function showError(node, msg) {
    console.warn(`[${node._ryhConfig?.nodeClass ?? "FolderMedia"}]`, msg);
    try {
        const toast = window.app?.extensionManager?.toast;
        if (toast && typeof toast.add === "function") {
            toast.add({ severity: "error", summary: node._ryhConfig?.nodeClass, detail: msg, life: 6000 });
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
            await refreshFileList(node);
        } else if (data?.error) {
            showError(node, data.error);
        }
    } catch (e) {
        console.warn(`[${node._ryhConfig.nodeClass}] 目录选择失败`, e);
    }
}

// ---------- 注册入口 ----------

export function registerFolderMediaBrowser(config) {
    const { nodeClass, fileWidget } = config;

    app.registerExtension({
        name: `ComfyUI-RYH.${nodeClass}`,

        // 新前端官方钩子：节点实例创建时调用（菜单新建 / 工作流加载均会触发）
        nodeCreated(node) {
            if (node.comfyClass === nodeClass) setupNode(node, config);
            return node;
        },

        // 工作流中节点配置完成（widget 值已恢复）后调用 —— 刷新列表的最可靠时机
        loadedGraphNode(node) {
            if (node.comfyClass !== nodeClass) return node;
            setupNode(node, config);
            refreshFileList(node);
            return node;
        },

        // 工作流加载完成后再兜底刷新一次。
        // 注意：该钩子的第一个参数是 missingNodeTypes 数组，不是 graph！
        afterConfigureGraph() {
            const g = app.graph;
            for (const node of g?._nodes ?? []) {
                if (node.comfyClass !== nodeClass) continue;
                setupNode(node, config);
                refreshFileList(node);
            }
        },
    });
}
