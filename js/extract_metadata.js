import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ============================================================
// ComfyUI-RYH · ExtractMetadata 前端扩展
// 功能：在节点上添加 [📂 文件] 按钮，弹窗选择视频/图片文件并回填路径
// ============================================================

const EXTENSION_NAME = "ComfyUI-RYH.ExtractMetadata";
const NODE_CLASS = "ExtractMetadata";

const isZH = navigator.language.startsWith("zh");

function setupNode(node) {
    if (node._ryhMetaSetupDone) return;

    const fileWidget = node.widgets?.find((w) => w.name === "file");
    if (!fileWidget) {
        // widget 创建时序问题，稍后重试
        setTimeout(() => setupNode(node), 100);
        return;
    }
    node._ryhMetaSetupDone = true;

    const bar = document.createElement("div");
    bar.style.cssText = "display:flex;gap:4px;align-items:center;width:100%;padding:2px 0;";
    const btn = document.createElement("button");
    btn.textContent = isZH ? "📂 选择文件" : "📂 Choose File";
    btn.style.cssText = "flex:1;padding:3px 0;font-size:12px;cursor:pointer;line-height:1;";
    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        browseFile(node, fileWidget);
    });
    bar.appendChild(btn);

    const controlsWidget = node.addDOMWidget("ryh_meta_controls", "controls", bar, {
        serialize: false,
        hideOnZoom: false,
    });
    controlsWidget.computeSize = function (width) {
        return [width, 28];
    };
}

function showError(msg) {
    console.warn("[ExtractMetadata]", msg);
    try {
        const toast = window.app?.extensionManager?.toast;
        if (toast && typeof toast.add === "function") {
            toast.add({ severity: "error", summary: "ExtractMetadata", detail: msg, life: 6000 });
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

async function browseFile(node, fileWidget) {
    try {
        const res = await api.fetchApi("/ryh/choose_file", { method: "POST" });
        const data = await res.json();
        if (data?.path) {
            fileWidget.value = data.path;
            fileWidget.callback?.(data.path);
            node.setDirtyCanvas?.(true, true);
        } else if (data?.error) {
            showError(data.error);
        }
    } catch (e) {
        showError(`文件选择失败: ${e}`);
    }
}

app.registerExtension({
    name: EXTENSION_NAME,

    nodeCreated(node) {
        if (node.comfyClass === NODE_CLASS) setupNode(node);
        return node;
    },

    loadedGraphNode(node) {
        if (node.comfyClass !== NODE_CLASS) return node;
        setupNode(node);
        return node;
    },

    afterConfigureGraph() {
        const g = app.graph;
        for (const node of g?._nodes ?? []) {
            if (node.comfyClass !== NODE_CLASS) continue;
            setupNode(node);
        }
    },
});
