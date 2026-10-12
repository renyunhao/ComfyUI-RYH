import { registerFolderMediaBrowser } from "./folder_media_browser.js";

// 与图片节点同一套浏览逻辑，差别只在 kind：
// 画廊格子是中间帧 + 时长/帧数标注，预览区用原生 <video controls> 播放。
registerFolderMediaBrowser({
    nodeClass: "LoadVideoAtFolder",
    fileWidget: "video",
    kind: "video",
});
