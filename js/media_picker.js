import { api } from "../../scripts/api.js";

// ============================================================
// ComfyUI-RYH · 全屏媒体选择器（可复用）
// 目录树 + 搜索 + 虚拟滚动列表/缩略图 + 预览，服务端出缩略图。
// 用法：openMediaPicker({ root, kind, current, title }) -> Promise<{relPath,name,kind}|null>
// ============================================================

const isZH = navigator.language.startsWith("zh");
const VIEW_KEY = "ComfyUI-RYH.mediaPicker.view";
const ROW_H = 30;
const CARD_GAP = 8;
const CARD_MIN = 132;
const THUMB_SIZE = 160;

const L = {
    viewList: isZH ? "列表" : "List",
    viewThumbs: isZH ? "缩略图" : "Thumbnails",
    refresh: isZH ? "刷新" : "Refresh",
    cancel: isZH ? "取消" : "Cancel",
    use: isZH ? "使用所选文件" : "Use selected file",
    search: isZH ? "搜索文件名或路径…" : "Filter by name or path…",
    searchAll: isZH ? "全库" : "All folders",
    root: isZH ? "全部（根目录）" : "All (root)",
    loading: isZH ? "正在读取目录…" : "Loading…",
    empty: isZH ? "这里没有可选的媒体文件" : "No media files here",
    emptySearch: isZH ? "没有匹配的文件" : "No matching files",
    count: isZH ? "共 {n} 个文件" : "{n} files",
    subdirs: isZH ? "{n} 个子文件夹" : "{n} subfolders",
    searchCount: isZH ? "找到 {n} 个结果" : "{n} matches",
    truncated: isZH ? "结果过多，仅显示前 {n} 条" : "Too many, showing first {n}",
    previewEmpty: isZH ? "选择左侧文件后在这里预览" : "Select a file to preview it here",
    file: isZH ? "文件" : "File",
    path: isZH ? "路径" : "Path",
    info: isZH ? "信息" : "Info",
    time: isZH ? "时间" : "Time",
    image: isZH ? "图片" : "image",
    video: isZH ? "视频" : "video",
    audio: isZH ? "音频" : "audio",
    all: isZH ? "全部" : "All",
};

const CSS = `
.ryh-pick-overlay{position:fixed;inset:0;z-index:10000;padding:16px;background:rgba(0,0,0,.72);display:flex;align-items:center;justify-content:center;box-sizing:border-box}
.ryh-pick{width:min(1180px,94vw);height:min(840px,92vh);background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:12px;box-sizing:border-box;display:flex;flex-direction:column;gap:10px;color:#ddd;font-size:11px;box-shadow:0 10px 28px rgba(0,0,0,.5)}
.ryh-pick-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex-wrap:wrap}
.ryh-pick-title{flex:0 1 auto;min-width:0;padding-top:4px;color:#e6e6e6;font-size:12px;font-weight:600;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ryh-pick-search{display:flex;align-items:center;gap:8px;flex:1 1 300px;min-width:0;justify-content:flex-end}
.ryh-pick-search input.ryh-pick-q{flex:1 1 240px;max-width:340px;min-width:120px;background:#181818;border:1px solid #3a3a3a;border-radius:6px;color:#eee;padding:5px 9px;font-size:11px;font-family:inherit;outline:none}
.ryh-pick-search input.ryh-pick-q:focus{border-color:#6aa9ff;box-shadow:0 0 0 2px rgba(106,169,255,.18)}
.ryh-pick-scope{display:inline-flex;align-items:center;gap:4px;color:#9a9a9a;font-size:10px;cursor:pointer;white-space:nowrap;user-select:none}
.ryh-pick-scope input{margin:0;width:13px;height:13px;cursor:pointer;accent-color:#6aa9ff}
.ryh-pick-actions{display:flex;gap:8px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end}
.ryh-pick-btn{appearance:none;background:#2a2a2a;border:1px solid #3a3a3a;border-radius:6px;color:#ddd;padding:4px 10px;font-size:11px;font-family:inherit;line-height:1.5;cursor:pointer;white-space:nowrap}
.ryh-pick-btn:hover{background:#333;border-color:#4a4a4a;color:#fff}
.ryh-pick-btn.active{border-color:#6aa9ff;color:#6aa9ff}
.ryh-pick-btn.primary{background:#2f6fd0;border-color:#3b7fdf;color:#fff}
.ryh-pick-btn.primary:hover{background:#3a7ce0;border-color:#4b90ef}
.ryh-pick-status{color:#999;font-size:11px;line-height:1.4;min-height:15px}
.ryh-pick-body{display:grid;grid-template-columns:190px minmax(0,1fr) minmax(260px,380px);gap:10px;flex:1 1 auto;min-height:0}
.ryh-pick-tree{display:flex;flex-direction:column;gap:1px;min-height:0;overflow:auto;background:#141414;border:1px solid #333;border-radius:6px;padding:6px;scrollbar-width:thin;scrollbar-color:#3a3a3a transparent}
.ryh-pick-node{appearance:none;display:flex;align-items:center;gap:4px;width:100%;background:transparent;border:1px solid transparent;border-radius:4px;color:#c8c8c8;font:inherit;font-size:11px;line-height:1.35;padding:4px 6px;text-align:left;cursor:pointer;min-width:0}
.ryh-pick-node:hover{background:#202020;color:#eee}
.ryh-pick-node.active{border-color:#6aa9ff;background:#1b2333;color:#9cc4ff}
.ryh-pick-caret{width:12px;flex-shrink:0;color:#777;text-align:center;font-size:9px;line-height:1}
.ryh-pick-label{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ryh-pick-count{flex-shrink:0;color:#6f6f6f;font-size:10px;font-variant-numeric:tabular-nums}
.ryh-pick-left,.ryh-pick-right{display:flex;flex-direction:column;gap:6px;min-width:0;min-height:0}
.ryh-pick-crumbs{display:flex;align-items:center;gap:2px;flex-wrap:wrap;font-size:10px;color:#888;min-height:16px;flex-shrink:0}
.ryh-pick-crumb{appearance:none;background:transparent;border:none;color:#9a9a9a;font:inherit;font-size:10px;padding:1px 3px;border-radius:3px;cursor:pointer;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ryh-pick-crumb:hover{background:#1b2333;color:#6aa9ff}
.ryh-pick-sep{color:#555}
.ryh-pick-scope-note{color:#6f6f6f;padding-left:3px}
.ryh-pick-table{width:100%;flex:1 1 auto;min-height:0;background:#141414;border:1px solid #333;border-radius:6px;color:#eee;box-sizing:border-box;display:flex;flex-direction:column;overflow:hidden;outline:none}
.ryh-pick-thead{display:grid;grid-template-columns:minmax(0,1fr) 104px 128px;flex-shrink:0;border-bottom:1px solid #333;background:#1a1a1a}
.ryh-pick-th{appearance:none;background:transparent;border:none;color:#8e8e8e;font-size:11px;text-align:left;padding:8px 10px;cursor:pointer;display:flex;align-items:center;gap:5px;min-width:0;font-family:inherit;line-height:1.35}
.ryh-pick-th:hover{color:#ddd}
.ryh-pick-th.static{cursor:default}
.ryh-pick-th.is-active{color:#d8d8d8}
.ryh-pick-sort{display:inline-block;width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;opacity:0;flex-shrink:0}
.ryh-pick-th.is-active .ryh-pick-sort{opacity:1;border-top:5px solid #6aa9ff;border-bottom:0}
.ryh-pick-th.is-active.is-asc .ryh-pick-sort{border-top:0;border-bottom:5px solid #6aa9ff}
.ryh-pick-scroller{flex:1 1 auto;overflow:auto;min-height:0;position:relative;scrollbar-width:thin;scrollbar-color:#3a3a3a transparent}
.ryh-pick-scroller.is-empty{display:flex;align-items:center;justify-content:center}
.ryh-pick-scroller.is-empty .ryh-pick-vspace{display:none}
.ryh-pick-gallery{display:none;position:relative;flex:1 1 auto;min-height:0;overflow:auto;padding:8px;box-sizing:border-box;background:#141414;border:1px solid #333;border-radius:6px;scrollbar-width:thin;scrollbar-color:#3a3a3a transparent}
.ryh-pick-left.is-thumbs .ryh-pick-table{display:none}
.ryh-pick-left.is-thumbs .ryh-pick-gallery{display:block}
.ryh-pick-left.is-thumbs .ryh-pick-gallery.is-empty{display:flex;align-items:center;justify-content:center}
.ryh-pick-vspace{position:relative;width:100%}
.ryh-pick-row{position:absolute;left:0;right:0;height:30px;box-sizing:border-box;display:grid;grid-template-columns:minmax(0,1fr) 104px 128px;align-items:center;cursor:pointer;border-bottom:1px solid #262626;color:#ddd;font-size:11px;line-height:1.35}
.ryh-pick-row:hover{background:#222}
.ryh-pick-row.selected{background:#2c3550}
.ryh-pick-cell{padding:0 10px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ryh-pick-cell.name{color:#eee}
.ryh-pick-cell.sub{color:#6f8a9a}
.ryh-pick-cell.info,.ryh-pick-cell.time{color:#9a9a9a;font-variant-numeric:tabular-nums}
.ryh-pick-empty{padding:18px;color:#666;font-size:11px;text-align:center}
.ryh-pick-preview{flex:1 1 auto;min-height:0;background:#111;border:1px solid #333;border-radius:6px;display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative}
.ryh-pick-preview img,.ryh-pick-preview video{display:block;width:100%;height:100%;object-fit:contain;background:#000}
.ryh-pick-preview audio{display:block;width:calc(100% - 20px)}
.ryh-pick-preview .hint{padding:18px;color:#666;font-size:11px;line-height:1.45;text-align:center}
.ryh-pick-meta{display:flex;flex-direction:column;gap:4px;color:#9a9a9a;font-size:10px;line-height:1.45;word-break:break-all;flex-shrink:0}
.ryh-pick-card{position:absolute;box-sizing:border-box;appearance:none;min-width:0;padding:4px;background:#161616;border:1px solid #333;border-radius:6px;color:#ddd;cursor:pointer;text-align:left;font:inherit;overflow:hidden}
.ryh-pick-card:hover,.ryh-pick-card.selected{border-color:#6aa9ff;background:#1b2333}
.ryh-pick-card img{display:block;width:100%;object-fit:cover;background:#090909;border-radius:4px}
.ryh-pick-card .badge{position:absolute;right:8px;width:18px;height:18px;border-radius:9px;background:rgba(0,0,0,.55);color:#fff;font-size:9px;line-height:18px;text-align:center;pointer-events:none}
.ryh-pick-card-name{display:block;padding:4px 2px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;line-height:1.35}
@media(max-width:768px){
.ryh-pick-body{grid-template-columns:minmax(0,1fr)}
.ryh-pick-tree,.ryh-pick-right{display:none}
.ryh-pick-thead,.ryh-pick-row{grid-template-columns:minmax(0,1fr) 72px 108px}
.ryh-pick-cell,.ryh-pick-th{padding-left:8px;padding-right:8px}
}
`;

let styleReady = false;

function ensureStyle() {
    if (styleReady) return;
    styleReady = true;
    const el = document.createElement("style");
    el.textContent = CSS;
    document.head.appendChild(el);
}

function apiUrl(path) {
    return api.apiURL ? api.apiURL(path) : path;
}

function readView() {
    try {
        return localStorage.getItem(VIEW_KEY) === "thumbs" ? "thumbs" : "list";
    } catch {
        return "list";
    }
}

function writeView(view) {
    try {
        localStorage.setItem(VIEW_KEY, view === "thumbs" ? "thumbs" : "list");
    } catch {
        /* private mode / quota */
    }
}

const EXT_KIND = {
    image: [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"],
    video: [".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".wmv", ".mpg", ".mpeg"],
    audio: [".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".aif", ".aiff", ".wma"],
};

export function mediaKindOf(name) {
    const ext = String(name || "").toLowerCase().match(/\.[^.\/\\]+$/)?.[0] || "";
    for (const [kind, exts] of Object.entries(EXT_KIND)) {
        if (exts.includes(ext)) return kind;
    }
    return "";
}

function splitRel(relPath) {
    const rel = String(relPath || "").replace(/\\/g, "/");
    const slash = rel.lastIndexOf("/");
    return slash < 0
        ? { subfolder: "", name: rel }
        : { subfolder: rel.slice(0, slash), name: rel.slice(slash + 1) };
}

function absFolder(root, subfolder) {
    let base = String(root || "").replace(/[\\/]+$/, "");
    if (/^[A-Za-z]:$/.test(base)) base += "/";
    const sub = String(subfolder || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    return sub ? `${base}/${sub}` : base;
}

function thumbnailUrl(folder, name) {
    const params = new URLSearchParams({ folder, file: name, size: String(THUMB_SIZE) });
    return apiUrl(`/ryh/thumbnail?${params}`);
}

function mediaUrl(folder, name) {
    const params = new URLSearchParams({ folder, file: name });
    return apiUrl(`/ryh/media?${params}`);
}

function formatTime(seconds) {
    const n = Number(seconds);
    if (!Number.isFinite(n) || n <= 0) return "—";
    const d = new Date(n * 1000);
    const pad = (v) => String(v).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatDuration(seconds) {
    const total = Math.round(Number(seconds) || 0);
    if (total <= 0) return "";
    return total < 60 ? `${total}s` : `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function infoText(item, meta) {
    const kind = mediaKindOf(item.name);
    if (kind === "image") {
        return meta?.width && meta?.height ? `${meta.width}×${meta.height}` : "—";
    }
    const parts = [];
    const duration = formatDuration(meta?.duration);
    if (duration) parts.push(duration);
    if (kind === "video" && meta?.frame_count) parts.push(`${meta.frame_count}f`);
    if (kind === "audio" && meta?.sample_rate) {
        parts.push(`${Math.round(meta.sample_rate / 100) / 10}k`);
    }
    return parts.length ? parts.join(" · ") : "—";
}

async function browseMedia(root, { dir = "", kind = "all", query = "", refresh = false } = {}) {
    const params = new URLSearchParams({ root, kind });
    if (dir) params.set("dir", dir);
    if (query) params.set("q", query);
    if (refresh) params.set("refresh", "1");
    const resp = await api.fetchApi(`/ryh/browse_media?${params.toString()}`);
    if (!resp.ok) {
        const message = (await resp.text()).trim();
        throw new Error(message || `HTTP ${resp.status}`);
    }
    const data = await resp.json();
    return {
        root: typeof data?.root === "string" ? data.root : root,
        dir: typeof data?.dir === "string" ? data.dir : "",
        files: Array.isArray(data?.files) ? data.files : [],
        dirs: Array.isArray(data?.dirs) ? data.dirs : [],
        tree: Array.isArray(data?.tree) ? data.tree : [],
        total: Number(data?.total) || 0,
        truncated: !!data?.truncated,
    };
}

async function fetchMeta(paths) {
    if (!paths.length) return {};
    const resp = await api.fetchApi("/ryh/media_meta", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths }),
    });
    if (!resp.ok) return {};
    const data = await resp.json();
    return data?.meta && typeof data.meta === "object" ? data.meta : {};
}

const KIND_LABEL = { image: L.image, video: L.video, audio: L.audio, all: L.all };

export function openMediaPicker({ root, kind = "all", current = "", title = "" } = {}) {
    ensureStyle();
    return new Promise((resolve) => {
        const overlay = document.createElement("div");
        overlay.className = "ryh-pick-overlay";
        const panel = document.createElement("div");
        panel.className = "ryh-pick";
        panel.innerHTML = `
            <div class="ryh-pick-head">
                <div class="ryh-pick-title"></div>
                <div class="ryh-pick-search">
                    <input type="search" class="ryh-pick-q" spellcheck="false" />
                    <label class="ryh-pick-scope"><input type="checkbox" class="ryh-pick-scope-all" /><span></span></label>
                </div>
                <div class="ryh-pick-actions"></div>
            </div>
            <div class="ryh-pick-status"></div>
            <div class="ryh-pick-body">
                <div class="ryh-pick-tree"></div>
                <div class="ryh-pick-left">
                    <div class="ryh-pick-crumbs"></div>
                    <div class="ryh-pick-table" tabindex="0">
                        <div class="ryh-pick-thead">
                            <button type="button" class="ryh-pick-th" data-sort="name"><span></span><i class="ryh-pick-sort"></i></button>
                            <div class="ryh-pick-th static"><span></span></div>
                            <button type="button" class="ryh-pick-th" data-sort="time"><span></span><i class="ryh-pick-sort"></i></button>
                        </div>
                        <div class="ryh-pick-scroller"></div>
                    </div>
                    <div class="ryh-pick-gallery" tabindex="0"></div>
                </div>
                <div class="ryh-pick-right">
                    <div class="ryh-pick-preview"></div>
                    <div class="ryh-pick-meta"></div>
                </div>
            </div>`;

        const titleEl = panel.querySelector(".ryh-pick-title");
        const statusEl = panel.querySelector(".ryh-pick-status");
        const actionsEl = panel.querySelector(".ryh-pick-actions");
        const treeEl = panel.querySelector(".ryh-pick-tree");
        const crumbsEl = panel.querySelector(".ryh-pick-crumbs");
        const tableEl = panel.querySelector(".ryh-pick-table");
        const scrollerEl = panel.querySelector(".ryh-pick-scroller");
        const leftEl = panel.querySelector(".ryh-pick-left");
        const galleryEl = panel.querySelector(".ryh-pick-gallery");
        const previewEl = panel.querySelector(".ryh-pick-preview");
        const metaEl = panel.querySelector(".ryh-pick-meta");
        const searchInput = panel.querySelector(".ryh-pick-q");
        const scopeAllEl = panel.querySelector(".ryh-pick-scope-all");

        const rowsHost = document.createElement("div");
        rowsHost.className = "ryh-pick-vspace";
        scrollerEl.appendChild(rowsHost);
        const cardsHost = document.createElement("div");
        cardsHost.className = "ryh-pick-vspace";
        galleryEl.appendChild(cardsHost);

        titleEl.textContent = title || `${isZH ? "选择媒体" : "Choose media"} · ${KIND_LABEL[kind] || L.all}`;
        searchInput.placeholder = L.search;
        panel.querySelector(".ryh-pick-scope span").textContent = L.searchAll;
        const thEls = [...panel.querySelectorAll(".ryh-pick-th[data-sort]")];
        thEls.forEach((th) => {
            th.querySelector("span").textContent = th.dataset.sort === "time" ? L.time : L.file;
        });
        panel.querySelector(".ryh-pick-th.static span").textContent = L.info;

        const startRel = current ? splitRel(current) : null;
        let dir = startRel ? startRel.subfolder : "";
        let query = "";
        let searchAll = false;
        let view = readView();
        let sortKey = "time";
        let sortDir = "desc";
        let items = [];
        let itemsByPath = new Map();
        let treeNodes = [];
        let rowsView = [];
        let rowsStamp = 0;
        let selectedValue = current || "";
        let loadSeq = 0;
        let rowEls = new Map();
        let cardEls = new Map();
        let rowsKey = "";
        let cardsKey = "";
        let grid = { cols: 4, cardW: 150, cardH: 130 };
        let rafId = 0;
        let searchTimer = 0;
        let metaTimer = 0;
        let resizeObserver = null;
        const expanded = new Set();
        const metaCache = new Map();
        const metaPending = new Set();

        const rootLabel = String(root || "").split(/[\\/]/).filter(Boolean).pop() || "root";

        const finish = (value) => {
            clearTimeout(searchTimer);
            clearTimeout(metaTimer);
            if (rafId) cancelAnimationFrame(rafId);
            resizeObserver?.disconnect();
            window.removeEventListener("keydown", onKeyDown, true);
            overlay.remove();
            resolve(value);
        };

        const selectedItem = () => itemsByPath.get(selectedValue || "") || null;

        const commit = () => {
            const item = selectedItem();
            if (!item) return;
            finish({ relPath: item.relPath, name: item.name, kind: mediaKindOf(item.name) });
        };

        const parentOf = (relPath) => {
            const slash = relPath.lastIndexOf("/");
            return slash < 0 ? "" : relPath.slice(0, slash);
        };

        const expandAncestors = (relPath) => {
            let cur = relPath;
            while (cur) {
                expanded.add(cur);
                cur = parentOf(cur);
            }
        };

        const applySort = () => {
            const labelOf = (item) => (query ? item.relPath : item.name);
            const copy = items.slice();
            copy.sort((a, b) => {
                if (sortKey === "name") {
                    const c = labelOf(a).toLowerCase().localeCompare(labelOf(b).toLowerCase(), undefined,
                        { numeric: true, sensitivity: "base" });
                    if (c) return sortDir === "asc" ? c : -c;
                } else {
                    const av = Number(a.modified) || 0;
                    const bv = Number(b.modified) || 0;
                    if (av !== bv) return sortDir === "asc" ? av - bv : bv - av;
                }
                return (a.relPath || "").localeCompare(b.relPath || "");
            });
            rowsView = copy;
            rowsStamp += 1;
        };

        const syncHeader = () => {
            thEls.forEach((th) => {
                const active = th.dataset.sort === sortKey;
                th.classList.toggle("is-active", active);
                th.classList.toggle("is-asc", active && sortDir === "asc");
            });
        };

        const metaKey = (item) => `${item.absPath}|${item.mtime}`;
        const metaOf = (item) => metaCache.get(metaKey(item)) || null;

        const renderPreview = (item) => {
            previewEl.textContent = "";
            metaEl.textContent = "";
            if (!item) {
                const hint = document.createElement("div");
                hint.className = "hint";
                hint.textContent = L.previewEmpty;
                previewEl.appendChild(hint);
                return;
            }
            const kindOfFile = mediaKindOf(item.name);
            const src = mediaUrl(item.absFolder, item.name);
            if (kindOfFile === "image") {
                const img = document.createElement("img");
                img.src = src;
                img.alt = item.name;
                previewEl.appendChild(img);
            } else if (kindOfFile === "audio") {
                const audio = document.createElement("audio");
                audio.src = src;
                audio.controls = true;
                audio.preload = "metadata";
                previewEl.appendChild(audio);
            } else if (kindOfFile === "video") {
                const video = document.createElement("video");
                video.src = src;
                video.controls = true;
                video.preload = "metadata";
                video.muted = true;
                video.playsInline = true;
                previewEl.appendChild(video);
            }
            const meta = metaOf(item);
            const fileEl = document.createElement("div");
            fileEl.textContent = `${L.file}: ${item.name} · ${KIND_LABEL[kindOfFile] || ""}`;
            const pathEl = document.createElement("div");
            pathEl.textContent = `${L.path}: ${item.relPath}`;
            metaEl.append(fileEl, pathEl);
            if (meta) {
                const info = document.createElement("div");
                info.textContent = `${L.info}: ${infoText(item, meta)}`;
                metaEl.appendChild(info);
            }
        };

        const applySelection = () => {
            for (const [idx, el] of rowEls) {
                el.classList.toggle("selected", rowsView[idx]?.relPath === selectedValue);
            }
            for (const [idx, el] of cardEls) {
                el.classList.toggle("selected", rowsView[idx]?.relPath === selectedValue);
            }
        };

        const visibleRange = () => {
            const total = rowsView.length;
            if (!total) return [0, 0];
            const viewH = scrollerEl.clientHeight || 360;
            const start = Math.max(0, Math.floor(scrollerEl.scrollTop / ROW_H) - 4);
            const end = Math.min(total, Math.ceil((scrollerEl.scrollTop + viewH) / ROW_H) + 4);
            return [start, end];
        };

        const measureGrid = () => {
            const innerW = Math.max(1, galleryEl.clientWidth - 16);
            const cols = Math.max(1, Math.floor((innerW + CARD_GAP) / (CARD_MIN + CARD_GAP)));
            const cardW = Math.max(96, Math.floor((innerW - CARD_GAP * (cols - 1)) / cols));
            return { cols, cardW, cardH: Math.round(cardW * 0.62) + 28 };
        };

        const requestMeta = () => {
            clearTimeout(metaTimer);
            metaTimer = setTimeout(() => {
                const [start, end] = visibleRange();
                const pending = [];
                for (let i = start; i < end; i++) {
                    const item = rowsView[i];
                    if (!item) continue;
                    const key = metaKey(item);
                    if (metaPending.has(key) || metaCache.has(key)) continue;
                    metaPending.add(key);
                    pending.push({ key, absPath: item.absPath });
                }
                if (!pending.length) return;
                void fetchMeta(pending.slice(0, 120).map((entry) => entry.absPath)).then((meta) => {
                    for (const entry of pending) {
                        if (meta[entry.absPath]) metaCache.set(entry.key, meta[entry.absPath]);
                    }
                    for (let i = start; i < end; i++) {
                        const item = rowsView[i];
                        const cell = rowEls.get(i)?.querySelector(".ryh-pick-cell.info");
                        if (item && cell) cell.textContent = infoText(item, metaOf(item));
                    }
                    const current = selectedItem();
                    if (current) renderPreview(current);
                }).catch(() => {});
            }, 120);
        };

        const paintRows = (force = false) => {
            if (!rowsView.length) return;
            scrollerEl.classList.remove("is-empty");
            scrollerEl.querySelectorAll(".ryh-pick-empty").forEach((el) => el.remove());
            rowsHost.style.height = `${rowsView.length * ROW_H}px`;
            const [start, end] = visibleRange();
            const key = `${start}:${end}:${rowsView.length}:${rowsStamp}`;
            if (!force && key === rowsKey) {
                applySelection();
                return;
            }
            rowsKey = key;
            rowsHost.textContent = "";
            rowEls = new Map();
            for (let i = start; i < end; i++) {
                const item = rowsView[i];
                const row = document.createElement("div");
                row.className = "ryh-pick-row";
                row.style.top = `${i * ROW_H}px`;
                row.dataset.path = item.relPath;
                const nameCell = document.createElement("div");
                nameCell.className = "ryh-pick-cell name";
                nameCell.title = item.relPath;
                if (query && item.subfolder) {
                    const sub = document.createElement("span");
                    sub.className = "ryh-pick-cell sub";
                    sub.textContent = `${item.subfolder}/`;
                    nameCell.appendChild(sub);
                }
                nameCell.appendChild(document.createTextNode(item.name));
                const infoCell = document.createElement("div");
                infoCell.className = "ryh-pick-cell info";
                infoCell.textContent = infoText(item, metaOf(item));
                const timeCell = document.createElement("div");
                timeCell.className = "ryh-pick-cell time";
                timeCell.textContent = formatTime(item.modified);
                row.append(nameCell, infoCell, timeCell);
                row.addEventListener("click", () => select(item, { scroll: false }));
                row.addEventListener("dblclick", commit);
                rowsHost.appendChild(row);
                rowEls.set(i, row);
            }
            applySelection();
            requestMeta();
        };

        const paintGallery = (force = false) => {
            if (!rowsView.length) return;
            galleryEl.classList.remove("is-empty");
            galleryEl.querySelectorAll(".ryh-pick-empty").forEach((el) => el.remove());
            grid = measureGrid();
            const total = rowsView.length;
            const rowH = grid.cardH + CARD_GAP;
            cardsHost.style.height = `${Math.ceil(total / grid.cols) * rowH}px`;
            const viewH = galleryEl.clientHeight || 360;
            const firstRow = Math.max(0, Math.floor(galleryEl.scrollTop / rowH) - 1);
            const lastRow = Math.min(Math.ceil(total / grid.cols),
                Math.ceil((galleryEl.scrollTop + viewH) / rowH) + 1);
            const start = firstRow * grid.cols;
            const end = Math.min(total, lastRow * grid.cols);
            const key = `${start}:${end}:${total}:${grid.cols}:${grid.cardW}:${rowsStamp}`;
            if (!force && key === cardsKey) {
                applySelection();
                return;
            }
            cardsKey = key;
            cardsHost.textContent = "";
            cardEls = new Map();
            const thumbH = grid.cardH - 28;
            for (let i = start; i < end; i++) {
                const item = rowsView[i];
                const kindOfFile = mediaKindOf(item.name);
                const card = document.createElement("button");
                card.type = "button";
                card.className = "ryh-pick-card";
                card.dataset.path = item.relPath;
                card.style.left = `${(i % grid.cols) * (grid.cardW + CARD_GAP)}px`;
                card.style.top = `${Math.floor(i / grid.cols) * rowH}px`;
                card.style.width = `${grid.cardW}px`;
                card.style.height = `${grid.cardH}px`;
                const img = document.createElement("img");
                img.style.height = `${thumbH}px`;
                img.alt = "";
                img.loading = "lazy";
                img.src = thumbnailUrl(item.absFolder, item.name);
                img.addEventListener("error", () => {
                    img.removeAttribute("src");
                });
                card.appendChild(img);
                if (kindOfFile !== "image") {
                    const badge = document.createElement("span");
                    badge.className = "badge";
                    badge.style.bottom = `${grid.cardH - thumbH + 12}px`;
                    badge.textContent = kindOfFile === "video" ? "▶" : "♪";
                    card.appendChild(badge);
                }
                const name = document.createElement("span");
                name.className = "ryh-pick-card-name";
                name.textContent = query ? item.relPath : item.name;
                name.title = item.relPath;
                card.appendChild(name);
                card.addEventListener("click", () => select(item));
                card.addEventListener("dblclick", commit);
                cardsHost.appendChild(card);
                cardEls.set(i, card);
            }
            applySelection();
        };

        const renderBrowser = () => {
            applyViewClass();
            if (view === "thumbs") paintGallery(true);
            else paintRows(true);
        };

        const setEmpty = (text) => {
            rowEls = new Map();
            cardEls = new Map();
            rowsKey = "";
            cardsKey = "";
            rowsHost.textContent = "";
            cardsHost.textContent = "";
            scrollerEl.querySelectorAll(".ryh-pick-empty").forEach((el) => el.remove());
            galleryEl.querySelectorAll(".ryh-pick-empty").forEach((el) => el.remove());
            for (const host of [scrollerEl, galleryEl]) {
                const row = document.createElement("div");
                row.className = "ryh-pick-empty";
                row.textContent = text;
                host.appendChild(row);
            }
            scrollerEl.classList.add("is-empty");
            galleryEl.classList.add("is-empty");
        };

        const scrollIntoView = () => {
            const idx = rowsView.findIndex((item) => item.relPath === selectedValue);
            if (idx < 0) return;
            if (view === "thumbs") {
                const rowH = grid.cardH + CARD_GAP;
                const top = Math.floor(idx / grid.cols) * rowH;
                const viewH = galleryEl.clientHeight || 360;
                if (top < galleryEl.scrollTop) galleryEl.scrollTop = top;
                else if (top + grid.cardH > galleryEl.scrollTop + viewH) {
                    galleryEl.scrollTop = top + grid.cardH - viewH;
                }
                paintGallery();
                return;
            }
            const top = idx * ROW_H;
            const viewH = scrollerEl.clientHeight || 360;
            if (top < scrollerEl.scrollTop) scrollerEl.scrollTop = top;
            else if (top + ROW_H > scrollerEl.scrollTop + viewH) {
                scrollerEl.scrollTop = top + ROW_H - viewH;
            }
            paintRows();
        };

        function select(item, { scroll = false } = {}) {
            if (!item) return;
            if (item.relPath !== selectedValue) {
                selectedValue = item.relPath;
                applySelection();
                renderPreview(item);
            }
            if (scroll) scrollIntoView();
        }

        const moveSelection = (delta) => {
            if (!rowsView.length) return;
            const idx = rowsView.findIndex((item) => item.relPath === selectedValue);
            const base = idx < 0 ? 0 : idx + delta;
            const next = rowsView[Math.max(0, Math.min(rowsView.length - 1, base))];
            if (next) select(next, { scroll: true });
        };

        const navigate = (nextDir) => {
            if (searchInput.value.trim()) searchInput.value = "";
            query = "";
            dir = nextDir || "";
            expandAncestors(dir);
            rowsKey = "";
            cardsKey = "";
            void load();
        };

        const renderTree = () => {
            treeEl.textContent = "";
            const rootBtn = document.createElement("button");
            rootBtn.type = "button";
            rootBtn.className = `ryh-pick-node${dir === "" ? " active" : ""}`;
            const rootName = document.createElement("span");
            rootName.className = "ryh-pick-label";
            rootName.textContent = L.root;
            rootBtn.appendChild(rootName);
            rootBtn.addEventListener("click", () => navigate(""));
            treeEl.appendChild(rootBtn);

            const parents = new Set(treeNodes.map((node) => parentOf(node.relPath)));
            let hiddenBelow = -1;
            for (const node of treeNodes) {
                if (hiddenBelow >= 0 && node.depth > hiddenBelow) continue;
                hiddenBelow = -1;
                const row = document.createElement("button");
                row.type = "button";
                row.className = `ryh-pick-node${dir === node.relPath ? " active" : ""}`;
                row.style.paddingLeft = `${4 + node.depth * 11}px`;
                const caret = document.createElement("span");
                caret.className = "ryh-pick-caret";
                if (parents.has(node.relPath)) {
                    caret.textContent = expanded.has(node.relPath) ? "▾" : "▸";
                    caret.addEventListener("click", (e) => {
                        e.stopPropagation();
                        if (expanded.has(node.relPath)) expanded.delete(node.relPath);
                        else expanded.add(node.relPath);
                        renderTree();
                    });
                }
                const label = document.createElement("span");
                label.className = "ryh-pick-label";
                label.textContent = node.name;
                label.title = node.relPath;
                const count = document.createElement("span");
                count.className = "ryh-pick-count";
                count.textContent = String(node.count);
                row.append(caret, label, count);
                row.addEventListener("click", () => navigate(node.relPath));
                treeEl.appendChild(row);
                if (!expanded.has(node.relPath)) hiddenBelow = node.depth;
            }
        };

        const renderCrumbs = () => {
            crumbsEl.textContent = "";
            const addCrumb = (label, target) => {
                if (crumbsEl.childNodes.length) {
                    const sep = document.createElement("span");
                    sep.className = "ryh-pick-sep";
                    sep.textContent = "/";
                    crumbsEl.appendChild(sep);
                }
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "ryh-pick-crumb";
                btn.textContent = label;
                btn.title = target || rootLabel;
                btn.addEventListener("click", () => navigate(target));
                crumbsEl.appendChild(btn);
            };
            addCrumb(rootLabel, "");
            let acc = "";
            for (const seg of dir ? dir.split("/") : []) {
                acc = acc ? `${acc}/${seg}` : seg;
                addCrumb(seg, acc);
            }
            if (query && searchAll) {
                const note = document.createElement("span");
                note.className = "ryh-pick-scope-note";
                note.textContent = `· ${L.searchAll}`;
                crumbsEl.appendChild(note);
            }
        };

        const updateStatus = (data) => {
            if (query) {
                const base = L.searchCount.replace("{n}", String(data.total));
                statusEl.textContent = data.truncated
                    ? `${base} · ${L.truncated.replace("{n}", String(data.total))}`
                    : base;
                return;
            }
            const parts = [L.count.replace("{n}", String(data.total))];
            if (data.dirs.length) parts.push(L.subdirs.replace("{n}", String(data.dirs.length)));
            statusEl.textContent = parts.join(" · ");
        };

        const load = async ({ refresh = false } = {}) => {
            const seq = ++loadSeq;
            statusEl.textContent = L.loading;
            const browseDir = query && searchAll ? "" : dir;
            try {
                const data = await browseMedia(root, { dir: browseDir, kind, query, refresh });
                if (seq !== loadSeq) return;
                if (!(query && searchAll)) dir = data.dir || "";
                items = data.files.map((file) => {
                    const { subfolder, name } = splitRel(file.relPath);
                    return { ...file, absFolder: absFolder(root, subfolder), absPath: `${absFolder(root, subfolder)}/${name}` };
                });
                itemsByPath = new Map(items.map((item) => [item.relPath, item]));
                treeNodes = data.tree;
                if (!query) expandAncestors(dir);
                applySort();
                if (!itemsByPath.has(selectedValue)) {
                    selectedValue = rowsView[0]?.relPath || "";
                }
                syncHeader();
                renderTree();
                renderCrumbs();
                if (!rowsView.length) {
                    setEmpty(query ? L.emptySearch : L.empty);
                    renderPreview(null);
                } else {
                    renderBrowser();
                    renderPreview(selectedItem());
                }
                updateStatus(data);
            } catch (err) {
                if (seq !== loadSeq) return;
                items = [];
                itemsByPath = new Map();
                rowsView = [];
                rowsStamp += 1;
                setEmpty(err?.message || String(err));
                statusEl.textContent = err?.message || String(err);
            }
        };

        const applyViewClass = () => {
            leftEl.classList.toggle("is-thumbs", view === "thumbs");
            listBtn.classList.toggle("active", view === "list");
            thumbsBtn.classList.toggle("active", view === "thumbs");
        };

        thEls.forEach((th) => {
            th.addEventListener("click", () => {
                const key = th.dataset.sort || "time";
                if (sortKey === key) sortDir = sortDir === "asc" ? "desc" : "asc";
                else {
                    sortKey = key;
                    sortDir = key === "name" ? "asc" : "desc";
                }
                applySort();
                scrollIntoView();
                renderBrowser();
                syncHeader();
            });
        });

        searchInput.addEventListener("input", () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                query = searchInput.value.trim();
                rowsKey = "";
                cardsKey = "";
                void load();
            }, 220);
        });
        scopeAllEl.addEventListener("change", () => {
            searchAll = scopeAllEl.checked;
            if (!searchInput.value.trim()) return;
            query = searchInput.value.trim();
            void load();
        });

        const onScroll = (paint) => () => {
            if (rafId) return;
            rafId = requestAnimationFrame(() => {
                rafId = 0;
                paint();
            });
        };
        scrollerEl.addEventListener("scroll", onScroll(() => paintRows()));
        galleryEl.addEventListener("scroll", onScroll(() => paintGallery()));

        const viewToggle = document.createElement("div");
        viewToggle.style.cssText = "display:inline-flex;gap:4px";
        const listBtn = document.createElement("button");
        listBtn.type = "button";
        listBtn.className = "ryh-pick-btn";
        listBtn.textContent = L.viewList;
        const thumbsBtn = document.createElement("button");
        thumbsBtn.type = "button";
        thumbsBtn.className = "ryh-pick-btn";
        thumbsBtn.textContent = L.viewThumbs;
        const setView = (next) => {
            view = next === "thumbs" ? "thumbs" : "list";
            writeView(view);
            rowsKey = "";
            cardsKey = "";
            renderBrowser();
            if (view === "thumbs") galleryEl.focus();
            else tableEl.focus();
        };
        listBtn.addEventListener("click", () => setView("list"));
        thumbsBtn.addEventListener("click", () => setView("thumbs"));
        viewToggle.append(listBtn, thumbsBtn);
        actionsEl.appendChild(viewToggle);

        const refreshBtn = document.createElement("button");
        refreshBtn.type = "button";
        refreshBtn.className = "ryh-pick-btn";
        refreshBtn.textContent = L.refresh;
        refreshBtn.addEventListener("click", () => { void load({ refresh: true }); });
        actionsEl.appendChild(refreshBtn);

        const actionsBottom = document.createElement("div");
        actionsBottom.className = "ryh-pick-actions";
        actionsBottom.style.cssText = "display:flex;gap:8px;justify-content:flex-end;flex:0 0 auto";
        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "ryh-pick-btn";
        cancelBtn.textContent = L.cancel;
        cancelBtn.addEventListener("click", () => finish(null));
        const okBtn = document.createElement("button");
        okBtn.type = "button";
        okBtn.className = "ryh-pick-btn primary";
        okBtn.textContent = L.use;
        okBtn.addEventListener("click", commit);
        actionsBottom.append(cancelBtn, okBtn);
        panel.appendChild(actionsBottom);

        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) finish(null);
        });
        panel.addEventListener("click", (e) => e.stopPropagation());

        resizeObserver = new ResizeObserver(() => {
            rowsKey = "";
            cardsKey = "";
            renderBrowser();
        });
        resizeObserver.observe(leftEl);

        const onKeyDown = (e) => {
            if (e.target === searchInput) {
                e.stopPropagation();
                if (e.key === "Escape") {
                    e.preventDefault();
                    finish(null);
                } else if (e.key === "Enter") {
                    e.preventDefault();
                    commit();
                }
                return;
            }
            if (e.key === "Escape") {
                e.preventDefault();
                e.stopPropagation();
                finish(null);
                return;
            }
            if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "ArrowLeft" || e.key === "ArrowRight") {
                if (!panel.contains(e.target) && e.target !== document.body) return;
                if (view === "list" && (e.key === "ArrowLeft" || e.key === "ArrowRight")) return;
                e.preventDefault();
                const cols = view === "thumbs" ? grid.cols : 1;
                const delta = e.key === "ArrowDown" ? cols
                    : e.key === "ArrowUp" ? -cols
                    : e.key === "ArrowRight" ? 1
                    : -1;
                moveSelection(delta);
                return;
            }
            if (e.key !== "Enter") return;
            if (e.target?.closest?.(".ryh-pick-th, .ryh-pick-actions, .ryh-pick-tree, .ryh-pick-crumbs")) return;
            e.preventDefault();
            commit();
        };
        window.addEventListener("keydown", onKeyDown, true);

        overlay.appendChild(panel);
        document.body.appendChild(overlay);
        applyViewClass();
        renderTree();
        renderCrumbs();
        renderPreview(null);
        void load();
        if (view === "thumbs") galleryEl.focus();
        else tableEl.focus();
    });
}
