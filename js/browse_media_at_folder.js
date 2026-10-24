import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { mediaKindOf, openMediaPicker } from "./media_picker.js";

// ============================================================
// ComfyUI-RYH · Browse Media At Folder
// 节点本体只负责：根目录、类型过滤、内嵌预览，以及打开全屏选择器。
// 选择器交互全部在 media_picker.js，三个简单节点（LoadXXXAtFolder）保持不变。
// ============================================================

const isZH = navigator.language.startsWith("zh");

const LABEL = {
    browse: isZH ? "🗂 浏览" : "🗂 Browse",
    root: isZH ? "📁 根目录" : "📁 Root",
    clear: isZH ? "✕ 清除" : "✕ Clear",
    hint: isZH ? "点「🗂 浏览」在目录树里挑一个媒体" : "Click Browse to pick media",
    missing: isZH ? "找不到文件，检查根目录是否改动" : "File not found — check the root folder",
    noRoot: isZH ? "请先填写根目录" : "Root folder is required",
    title: isZH ? "选择媒体" : "Choose media",
};

const KIND_LABEL = {
    image: isZH ? "图片" : "image",
    video: isZH ? "视频" : "video",
    audio: isZH ? "音频" : "audio",
    all: isZH ? "全部" : "all",
};

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

function apiUrl(path) {
    return api.apiURL ? api.apiURL(path) : path;
}

function splitAbs(path) {
    const norm = String(path || "").replace(/\\/g, "/");
    const slash = norm.lastIndexOf("/");
    if (slash <= 0) return { folder: norm, name: norm };
    return { folder: norm.slice(0, slash), name: norm.slice(slash + 1) };
}

function mediaUrl(absPath) {
    const { folder, name } = splitAbs(absPath);
    if (!name) return "";
    const params = new URLSearchParams({ folder, file: name });
    return apiUrl(`/ryh/media?${params.toString()}`);
}

function showError(msg) {
    try {
        const toast = window.app?.extensionManager?.toast;
        if (toast && typeof toast.add === "function") {
            toast.add({ severity: "error", summary: "Browse Media At Folder", detail: msg, life: 6000 });
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

function previewRoot() {
    const root = document.createElement("div");
    root.dataset.ryhMediaPreview = "1";
    root.style.cssText =
        "width:100%;height:100%;display:flex;flex-direction:column;gap:2px;overflow:hidden;" +
        "background:rgba(0,0,0,0.25);--comfy-widget-min-height:120;";
    const box = document.createElement("div");
    box.style.cssText = "flex:1;min-height:0;display:flex;align-items:center;justify-content:center;overflow:hidden;";
    const caption = document.createElement("div");
    caption.style.cssText =
        "flex:0 0 auto;font-size:10px;line-height:1.35;color:#9ab;padding:1px 3px;" +
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
    root.append(box, caption);
    return { root, box, caption };
}

function updatePreview(node) {
    const view = node._ryhBrowseView;
    const fileWidget = node._ryhFileWidget;
    if (!view || !fileWidget) return;

    const root = String(node._ryhFolderWidget?.value || "").trim();
    const value = fileWidget.value;
    const rel = value && value !== "none" ? String(value).replace(/\\/g, "/").replace(/^\/+/, "") : "";

    view.box.textContent = "";
    view.caption.textContent = "";
    view.caption.title = "";
    if (!rel) {
        const hint = document.createElement("div");
        hint.textContent = LABEL.hint;
        hint.style.cssText = "padding:12px;color:#777;font-size:11px;text-align:center;";
        view.box.appendChild(hint);
        return;
    }

    const kind = mediaKindOf(rel);
    if (!root) {
        view.caption.textContent = LABEL.noRoot;
        return;
    }
    const absPath = `${root.replace(/[\\/]+$/, "")}/${rel}`;
    const src = mediaUrl(absPath);
    const fail = () => {
        view.box.textContent = "";
        view.caption.textContent = `${LABEL.missing} · ${rel}`;
        view.caption.title = absPath;
    };

    if (kind === "video") {
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "metadata";
        video.style.cssText = "display:block;width:100%;height:100%;object-fit:contain;background:#000;";
        video.addEventListener("error", fail);
        video.src = src;
        view.box.appendChild(video);
    } else if (kind === "audio") {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "metadata";
        audio.style.cssText = "display:block;width:calc(100% - 16px);";
        audio.addEventListener("error", fail);
        audio.src = src;
        view.box.appendChild(audio);
    } else if (kind === "image") {
        const img = document.createElement("img");
        img.alt = "";
        img.style.cssText = "display:block;max-width:100%;max-height:100%;object-fit:contain;";
        img.addEventListener("error", fail);
        img.src = src;
        view.box.appendChild(img);
    } else {
        fail();
        return;
    }
    view.caption.textContent = `${rel} · ${KIND_LABEL[kind] || ""}`;
    view.caption.title = absPath;
}

function syncFileOptions(node) {
    const widget = node._ryhFileWidget;
    if (!widget?.options) return;
    const value = widget.value;
    widget.options.values = value && value !== "none" ? ["none", value] : ["none"];
}

// 当前文件所在目录（相对根目录）+ 类型，作为同目录列表的缓存键
function siblingContext(node) {
    const root = String(node._ryhFolderWidget?.value || "").trim();
    const value = node._ryhFileWidget?.value;
    if (!root || !value || value === "none") return null;
    const rel = String(value).replace(/\\/g, "/");
    const slash = rel.lastIndexOf("/");
    const dir = slash < 0 ? "" : rel.slice(0, slash);
    const kind = node._ryhKindWidget?.value || "all";
    return { root, kind, dir, key: `${root}\n${kind}\n${dir}` };
}

async function loadSiblings(node) {
    const ctx = siblingContext(node);
    if (!ctx) return null;
    if (node._ryhSiblingsKey === ctx.key) return node._ryhSiblings;
    const params = new URLSearchParams({ root: ctx.root, kind: ctx.kind });
    if (ctx.dir) params.set("dir", ctx.dir);
    const resp = await api.fetchApi(`/ryh/browse_media?${params.toString()}`);
    if (!resp.ok) return null;
    const data = await resp.json();
    const files = (Array.isArray(data?.files) ? data.files : []).map((file) => file.relPath);
    // 期间根目录/类型/所在目录又变了，别把旧结果写进缓存
    if (siblingContext(node)?.key !== ctx.key) return files;
    node._ryhSiblings = files;
    node._ryhSiblingsKey = ctx.key;
    return files;
}

function updateNav(node) {
    const value = node._ryhFileWidget?.value;
    const active = !!value && value !== "none";
    if (node._ryhPrevBtn) node._ryhPrevBtn.disabled = !active;
    if (node._ryhNextBtn) node._ryhNextBtn.disabled = !active;
    if (!node._ryhCounter) return;
    const ctx = siblingContext(node);
    const files = ctx && node._ryhSiblingsKey === ctx.key ? node._ryhSiblings : null;
    const idx = files ? files.indexOf(value) : -1;
    node._ryhCounter.textContent = idx >= 0 ? `${idx + 1}/${files.length}` : "–";
}

async function stepFile(node, delta) {
    const value = node._ryhFileWidget?.value;
    if (!value || value === "none") return;
    let files = null;
    try {
        files = await loadSiblings(node);
    } catch (e) {
        console.warn("[BrowseMediaAtFolder] 读取同目录文件失败", e);
        return;
    }
    if (!files?.length) return;
    const idx = files.indexOf(value);
    const base = idx < 0 ? 0 : idx + delta;
    const next = files[Math.max(0, Math.min(files.length - 1, base))];
    if (!next || next === value) {
        updateNav(node);
        return;
    }
    setFile(node, next);
}

function setFile(node, relPath) {
    const widget = node._ryhFileWidget;
    if (!widget) return;
    const value = relPath && relPath !== "none" ? relPath : "none";
    widget.value = value;
    syncFileOptions(node);
    updatePreview(node);
    updateNav(node);
    node.setDirtyCanvas?.(true, true);
    app.graph?.setDirtyCanvas(true, true);
}

async function browseRoot(node) {
    try {
        const res = await api.fetchApi("/ryh/choose_folder", { method: "POST" });
        const data = await res.json();
        if (data?.path) {
            node._ryhFolderWidget.value = data.path;
            node._ryhLastRoot = data.path;
            node.setDirtyCanvas?.(true, true);
            updatePreview(node);
        } else if (data?.error) {
            showError(data.error);
        }
    } catch (e) {
        console.warn("[BrowseMediaAtFolder] 根目录选择失败", e);
    }
}

async function pickMedia(node) {
    const root = String(node._ryhFolderWidget?.value || "").trim();
    if (!root) {
        showError(LABEL.noRoot);
        return;
    }
    const kind = node._ryhKindWidget?.value || "all";
    const current = node._ryhFileWidget?.value === "none" ? "" : node._ryhFileWidget?.value || "";
    let picked = null;
    try {
        picked = await openMediaPicker({ root, kind, current, title: LABEL.title });
    } catch (e) {
        console.warn("[BrowseMediaAtFolder] 选择器打开失败", e);
        showError(String(e?.message || e));
        return;
    }
    if (!picked) return;
    setFile(node, picked.relPath);
}

function setupNode(node, { isNew = false } = {}) {
    if (node._ryhBrowseReady) {
        updatePreview(node);
        updateNav(node);
        return;
    }
    const folderWidget = node.widgets?.find((w) => w.name === "folder");
    const kindWidget = node.widgets?.find((w) => w.name === "kind");
    const fileWidget = node.widgets?.find((w) => w.name === "file");
    if (!folderWidget || !kindWidget || !fileWidget) {
        setTimeout(() => setupNode(node, { isNew }), 100);
        return;
    }
    node._ryhBrowseReady = true;
    node._ryhFolderWidget = folderWidget;
    node._ryhKindWidget = kindWidget;
    node._ryhFileWidget = fileWidget;
    node._ryhLastRoot = folderWidget.value;

    // 下拉里只放当前选中值：文件路径由选择器写入，不需要列整棵目录
    syncFileOptions(node);

    const bar = document.createElement("div");
    bar.dataset.ryhMediaPreview = "1";
    bar.style.cssText = "display:flex;gap:4px;align-items:center;width:100%;padding:2px 0;";
    const makeBtn = (label, cb) => {
        const btn = document.createElement("button");
        btn.textContent = label;
        btn.title = label;
        btn.style.cssText = "flex:1;padding:3px 0;font-size:12px;cursor:pointer;line-height:1;";
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            cb();
        });
        bar.appendChild(btn);
        return btn;
    };
    // ◀ ▶ 在当前文件所在目录内步进（列表按需拉取，不塞进下拉）
    const prevBtn = makeBtn("◀", () => { void stepFile(node, -1); });
    prevBtn.title = isZH ? "PREV · 同目录上一个" : "PREV · previous in folder";
    prevBtn.style.flex = "0 0 26px";
    const nextBtn = makeBtn("▶", () => { void stepFile(node, 1); });
    nextBtn.title = isZH ? "NEXT · 同目录下一个" : "NEXT · next in folder";
    nextBtn.style.flex = "0 0 26px";
    const counter = document.createElement("span");
    counter.style.cssText =
        "flex:0 0 auto;min-width:30px;text-align:center;font-size:10px;line-height:1;" +
        "color:#9ab;font-variant-numeric:tabular-nums;";
    counter.textContent = "–";
    bar.appendChild(counter);
    node._ryhPrevBtn = prevBtn;
    node._ryhNextBtn = nextBtn;
    node._ryhCounter = counter;

    const browseBtn = makeBtn(LABEL.browse, () => { void pickMedia(node); });
    browseBtn.style.flex = "2 1 0";
    makeBtn(LABEL.root, () => { void browseRoot(node); });
    const clearBtn = makeBtn(LABEL.clear, () => setFile(node, "none"));
    clearBtn.style.color = "#e06c6c";

    const barWidget = node.addDOMWidget("ryh_browse_bar", "controls", bar, {
        serialize: false,
        hideOnZoom: false,
    });
    barWidget.computeSize = function (width) {
        return [width, 28];
    };

    const view = previewRoot();
    node.addDOMWidget("ryh_browse_preview", "preview", view.root, {
        serialize: false,
        hideOnZoom: false,
    });
    node._ryhBrowseView = view;

    chainCallback(folderWidget, "callback", function () {
        if (node._ryhLastRoot === folderWidget.value) return;
        node._ryhLastRoot = folderWidget.value;
        // 选的路径是相对旧根目录的，换根之后已无意义
        setFile(node, "none");
    });
    chainCallback(fileWidget, "callback", function () {
        updatePreview(node);
        updateNav(node);
    });
    chainCallback(kindWidget, "callback", function () {
        // 类型变了，同目录列表和计数都要重来
        node._ryhSiblings = null;
        node._ryhSiblingsKey = "";
        updateNav(node);
    });

    if (isNew && !node.properties?.ryh_browse_sized) {
        node.setProperty?.("ryh_browse_sized", true);
        node.setSize([Math.max(node.size[0], 340), Math.max(node.size[1], 340)]);
    }

    updatePreview(node);
    updateNav(node);
}

app.registerExtension({
    name: "ComfyUI-RYH.BrowseMediaAtFolder",

    nodeCreated(node) {
        if (node.comfyClass === "BrowseMediaAtFolder") setupNode(node, { isNew: true });
        return node;
    },

    loadedGraphNode(node) {
        if (node.comfyClass !== "BrowseMediaAtFolder") return node;
        setupNode(node);
        // configure() 会用节点定义里的 ["none"] 覆盖下拉选项，恢复后再补一次
        syncFileOptions(node);
        updatePreview(node);
        updateNav(node);
        return node;
    },

    afterConfigureGraph() {
        for (const node of app.graph?._nodes ?? []) {
            if (node.comfyClass !== "BrowseMediaAtFolder") continue;
            setupNode(node);
            syncFileOptions(node);
            updatePreview(node);
            updateNav(node);
        }
    },
});
