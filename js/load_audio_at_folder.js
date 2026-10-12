import { registerFolderMediaBrowser } from "./folder_media_browser.js";

// 与图片节点同一套浏览逻辑，差别只在 kind：
// 画廊格子是波形图 + 时长/采样率标注，预览区用原生 <audio controls> 播放。
registerFolderMediaBrowser({
    nodeClass: "LoadAudioAtFolder",
    fileWidget: "audio",
    kind: "audio",
});
