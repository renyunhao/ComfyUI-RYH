import { registerFolderMediaBrowser } from "./folder_media_browser.js";

// 目录浏览的全部行为在 folder_media_browser.js：
// [📁 目录] 选目录、下拉/◀ ▶ 选图、▦ 画廊缩略图网格 + 尺寸滑块、节点内等比预览、🗑 删除。
registerFolderMediaBrowser({
    nodeClass: "LoadImageAtFolder",
    fileWidget: "image",
    kind: "image",
});
