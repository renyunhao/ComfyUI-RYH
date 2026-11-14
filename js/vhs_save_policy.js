import { app } from "../../scripts/app.js";

// ============================================================
// ComfyUI-RYH · Video Combine 保存选项的前端兼容层
//
// 旧补丁在节点上放的是布尔开关 only_save_with_audio（True = 只留带音频那份），
// 现在改成下拉 save_audio_mode：都保存 / 无音频 / 有音频。
// VHS 自己的 onConfigure 按 widget 名恢复 widgets_values：旧键会被忽略，
// 新键回落到默认「都保存」，于是老工作流里开过旧开关的节点会静默变成两份都存。
// 这里在整条配置链跑完后把旧布尔值迁移过来，顺手规范化落到下拉上的非法值，
// 免得老工作流再次保存时把 true/false 写进 combo。
// ============================================================

const EXTENSION_NAME = "ComfyUI-RYH.VHSSaveAudioMode";
const NODE_CLASS = "VHS_VideoCombine";
const WIDGET_NAME = "save_audio_mode";
const LEGACY_WIDGET_NAME = "only_save_with_audio";
const OPTIONS = ["都保存", "无音频", "有音频"];
const DEFAULT = OPTIONS[0];
const WITH_AUDIO = OPTIONS[2];

function chainCallback(object, property, callback) {
    if (object[property] == undefined) {
        object[property] = callback;
        return;
    }
    const original = object[property];
    object[property] = function () {
        const first = original.apply(this, arguments);
        const second = callback.apply(this, arguments);
        return first === undefined ? second : first;
    };
}

function normalize(value) {
    if (value === true || value === 1 || value === "true" || value === "1") return WITH_AUDIO;
    if (OPTIONS.includes(value)) return value;
    // false / "false" / 未知值 -> 原版行为（两份都保存）
    return DEFAULT;
}

app.registerExtension({
    name: EXTENSION_NAME,
    nodeCreated(node) {
        if (!node || (node.type !== NODE_CLASS && node.comfyClass !== NODE_CLASS)) return;
        const widget = node.widgets?.find((w) => w.name === WIDGET_NAME);
        if (!widget) return; // 后端补丁没装（如 RYH_DISABLE_PATCHES=1）时不介入

        chainCallback(node, "onConfigure", function (info) {
            const saved = info?.widgets_values;
            const legacy = saved && !Array.isArray(saved) ? saved[LEGACY_WIDGET_NAME] : undefined;
            // VHS 的 onConfigure 也是挂在这条链上的，先后顺序取决于扩展加载顺序。
            // 放到微任务里，等同步配置全部结束后再定值，避免被它的默认值覆盖。
            queueMicrotask(() => {
                let value = normalize(widget.value);
                if (value === DEFAULT && (legacy === true || legacy === "true")) {
                    value = WITH_AUDIO; // 旧开关打开 -> 等价的「有音频」
                }
                if (widget.value !== value) {
                    widget.value = value;
                    widget.callback?.(value);
                }
            });
        });
    },
});
