"""任意目录媒体的列举、探测、解码与缩略图生成，供三个 LoadXXXAtFolder 节点共用。

ComfyUI 官方 loader 只认 input 目录，而这些节点读的是用户随便指定的目录，
所以列举、探测、缩略图、播放都由这里自己提供。
"""

import os
import time
from collections import OrderedDict
from io import BytesIO

import av
import numpy as np
import torch
from PIL import Image, ImageDraw, ImageOps, ImageSequence

import folder_paths
import node_helpers

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".wmv", ".mpg", ".mpeg")
AUDIO_EXTENSIONS = (".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".aif", ".aiff", ".wma")

MEDIA_EXTENSIONS_BY_KIND = {
    "image": IMAGE_EXTENSIONS,
    "video": VIDEO_EXTENSIONS,
    "audio": AUDIO_EXTENSIONS,
}

AV_TIME_BASE = 1_000_000  # container/steam duration 的单位是微秒
WAVEFORM_BINS = 64  # 再多的柱子在几十像素宽的格子里也分辨不出来
WAVEFORM_MAX_SECONDS = 120.0  # 音频缩略图最多解码这么久，长素材只画开头一段
_THUMBNAIL_CACHE_LIMIT = 512
_PROBE_CACHE_LIMIT = 4096
_DIR_TREE_TTL = 5.0
_SEARCH_LIMIT = 2000
_META_BATCH_LIMIT = 400

_thumbnail_cache = OrderedDict()
_probe_cache = OrderedDict()
_dir_tree_cache = {}


def media_extension(path):
    return os.path.splitext(path)[1].lower()


def is_allowed_media(path):
    """只允许列举/播放这三类扩展名：路由的 folder 参数是用户任意指定的目录，
    不给白名单就等于可以把 .py/.json/HTML 之类的文件读出去。"""
    return media_extension(path) in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + AUDIO_EXTENSIONS


def resolve_media_path(folder, name):
    """把媒体输入值解析为绝对路径。

    兼容三种取值：
    1. 绝对路径 -> 原样使用；
    2. 裸文件名 -> 与 folder 拼接（节点自身下拉列表的行为）；
    3. 含目录分隔符的路径 -> 先尝试相对 folder，不存在再按相对
       ComfyUI input 根目录解析（批量工具下拉注入的是这种路径）。
    """
    if not name:
        return ""
    if os.path.isabs(name):
        return os.path.normpath(name)
    if "/" in name or os.path.sep in name:
        folder_candidate = os.path.normpath(os.path.join(folder, name))
        if os.path.isfile(folder_candidate):
            return folder_candidate
        input_candidate = os.path.normpath(
            os.path.join(folder_paths.get_input_directory(), name)
        )
        if os.path.isfile(input_candidate):
            return input_candidate
        return folder_candidate
    return os.path.normpath(os.path.join(folder, name))


def list_media(folder, kind):
    """目录下该类型的文件列表：[{name, mtime, ...探测信息}]，按文件名排序。"""
    extensions = MEDIA_EXTENSIONS_BY_KIND.get(kind, IMAGE_EXTENSIONS)
    if not folder or not os.path.isdir(folder):
        return []
    result = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    for name in names:
        full_path = os.path.join(folder, name)
        if not os.path.isfile(full_path) or media_extension(full_path) not in extensions:
            continue
        try:
            mtime = os.stat(full_path).st_mtime_ns
        except OSError:
            mtime = 0
        item = {"name": name, "mtime": mtime}
        item.update(probe_media(full_path))
        result.append(item)
    return result


def extensions_for_kind(kind):
    if kind == "all":
        return IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + AUDIO_EXTENSIONS
    return MEDIA_EXTENSIONS_BY_KIND.get(kind, IMAGE_EXTENSIONS)


def media_kind(path):
    ext = media_extension(path)
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return ""


def _within(root, path):
    root_n = os.path.normcase(os.path.realpath(root))
    path_n = os.path.normcase(os.path.realpath(path))
    return path_n == root_n or path_n.startswith(root_n + os.sep)


def resolve_browse_dir(root, rel_dir):
    """把相对目录解析成 root 内的绝对路径；越界或目录不存在返回 None。"""
    if not root or not os.path.isdir(root):
        return None
    rel = str(rel_dir or "").replace("\\", "/").strip("/")
    target = os.path.realpath(os.path.join(root, rel)) if rel else os.path.realpath(root)
    if not _within(root, target) or not os.path.isdir(target):
        return None
    return target


def _file_entry(root, entry, stat):
    rel_path = os.path.relpath(entry.path, root).replace("\\", "/")
    subfolder = os.path.dirname(rel_path).replace("\\", "/")
    return {
        "name": entry.name,
        "relPath": rel_path,
        "subfolder": "" if subfolder == "." else subfolder,
        "mtime": stat.st_mtime_ns,
        "modified": stat.st_mtime,
    }


def list_dir_media(root, abs_dir, extensions):
    """单层文件列表（不带探测信息）+ 子目录名；尺寸/时长由 /ryh/media_meta 按需补。"""
    files = []
    subdirs = []
    with os.scandir(abs_dir) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.name)
                    continue
                if media_extension(entry.name) not in extensions:
                    continue
                stat = entry.stat()
            except OSError:
                continue
            files.append(_file_entry(root, entry, stat))
    files.sort(key=lambda item: (-item["modified"], item["relPath"]))
    return files, subdirs


def dir_tree(root, extensions, refresh=False):
    """root 下全部子目录的扁平先序列表，count 为该目录（含子目录）内的媒体数量。"""
    key = (os.path.normcase(os.path.realpath(root)), extensions)
    now = time.monotonic()
    cached = _dir_tree_cache.get(key)
    if not refresh and cached and now - cached[0] < _DIR_TREE_TTL:
        return cached[1]

    nodes = []

    def walk(abs_dir, rel, depth):
        try:
            entries = []
            with os.scandir(abs_dir) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        entries.append((entry.name, entry.path, entry.is_dir(follow_symlinks=False)))
                    except OSError:
                        continue
        except OSError:
            return 0
        count = 0
        children = []
        for name, path, is_dir in entries:
            if is_dir:
                children.append((name, path))
            elif media_extension(name) in extensions:
                count += 1
        for name, path in sorted(children, key=lambda item: item[0].lower()):
            child_rel = f"{rel}/{name}" if rel else name
            node = {"name": name, "relPath": child_rel, "depth": depth, "count": 0}
            nodes.append(node)
            node["count"] = walk(path, child_rel, depth + 1)
            count += node["count"]
        return count

    walk(os.path.realpath(root), "", 0)
    _dir_tree_cache[key] = (now, nodes)
    return nodes


def search_media(root, abs_dir, extensions, query, limit=_SEARCH_LIMIT):
    """在 abs_dir 下递归按相对路径模糊匹配，返回 (files, truncated)。"""
    needle = query.casefold()
    hits = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(abs_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or media_extension(name) not in extensions:
                continue
            full_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(full_path, root).replace("\\", "/")
            if needle not in rel_path.casefold():
                continue
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            subfolder = os.path.dirname(rel_path).replace("\\", "/")
            hits.append({
                "name": name,
                "relPath": rel_path,
                "subfolder": "" if subfolder == "." else subfolder,
                "mtime": stat.st_mtime_ns,
                "modified": stat.st_mtime,
            })
            if len(hits) >= limit:
                truncated = True
                break
        if truncated:
            break
    hits.sort(key=lambda item: (-item["modified"], item["relPath"]))
    return hits, truncated


def browse_media(root, rel_dir, kind, query="", refresh=False):
    """目录树 + 当前层文件 / 递归搜索结果，供前端媒体选择器使用。"""
    abs_dir = resolve_browse_dir(root, rel_dir)
    if abs_dir is None:
        raise ValueError(f"目录不可用：{root or '(空)'}")
    extensions = extensions_for_kind(kind)
    root_real = os.path.realpath(root)
    rel_dir = "" if os.path.normcase(abs_dir) == os.path.normcase(root_real) else (
        os.path.relpath(abs_dir, root_real).replace("\\", "/")
    )
    tree = dir_tree(root_real, extensions, refresh)
    if query:
        files, truncated = search_media(root_real, abs_dir, extensions, query)
        return {"root": root_real, "dir": rel_dir, "files": files, "dirs": [],
                "total": len(files), "truncated": truncated, "tree": tree}

    files, subdirs = list_dir_media(root_real, abs_dir, extensions)
    counts = {node["relPath"]: node["count"] for node in tree}
    dirs = []
    for name in sorted(subdirs, key=str.lower):
        child_rel = f"{rel_dir}/{name}" if rel_dir else name
        dirs.append({"name": name, "relPath": child_rel, "count": counts.get(child_rel, 0)})
    return {"root": root_real, "dir": rel_dir, "files": files, "dirs": dirs,
            "total": len(files), "truncated": False, "tree": tree}


def probe_many(paths):
    """批量读取文件头信息，key 用调用方传进来的原始路径，方便前端直接对照。"""
    result = {}
    for path in paths[:_META_BATCH_LIMIT]:
        if not isinstance(path, str) or not path:
            continue
        full_path = os.path.normpath(path)
        if not os.path.isfile(full_path) or not is_allowed_media(full_path):
            continue
        info = probe_media(full_path)
        if info:
            result[path] = info
    return result


def probe_media(path):
    """读文件头信息（尺寸/时长/帧数/采样率）用于画廊标注，失败返回空 dict。

    坏文件、扩展名与实际编码不符都会走到空 dict：标注只是锦上添花，
    不能因为它让整个列表接口挂掉。
    """
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return {}

    key = (os.path.normpath(path), mtime)
    hit = _probe_cache.get(key)
    if hit is not None:
        _probe_cache.move_to_end(key)
        return hit

    try:
        if media_extension(path) in IMAGE_EXTENSIONS:
            info = _probe_image(path)
        else:
            info = _probe_container(path)
    except Exception:
        info = {}

    _probe_cache[key] = info
    while len(_probe_cache) > _PROBE_CACHE_LIMIT:
        _probe_cache.popitem(last=False)
    return info


def _probe_image(path):
    # PIL 是懒加载：open 只读文件头，拿到宽高就够画廊用了
    with Image.open(path) as img:
        return {"width": img.width, "height": img.height}


def _probe_container(path):
    info = {}
    with av.open(path) as container:
        duration = (container.duration or 0) / AV_TIME_BASE
        if duration > 0:
            info["duration"] = round(duration, 3)

        video = container.streams.video[0] if container.streams.video else None
        if video is not None:
            rate = float(video.average_rate or video.guessed_rate or 0.0)
            info["width"] = video.codec_context.width
            info["height"] = video.codec_context.height
            info["frame_rate"] = round(rate, 3)
            frames = video.frames or (int(round(duration * rate)) if rate and duration else 0)
            info["frame_count"] = int(frames or 0)

        audio = container.streams.audio[0] if container.streams.audio else None
        if audio is not None and audio.codec_context.sample_rate:
            info["sample_rate"] = int(audio.codec_context.sample_rate)
    return info


def build_thumbnail(path, size):
    """返回 (webp 字节, etag)，失败 (None, None)。

    图片等比裁切、视频取中间帧、音频画波形，全部输出正方形：画廊格子是
    正方形 + object-fit: cover，等比缩放会让宽图在格子里只剩中间一条。
    """
    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except OSError:
        return None, None

    key = (os.path.normpath(path), mtime_ns, size)
    hit = _thumbnail_cache.get(key)
    if hit is not None:
        _thumbnail_cache.move_to_end(key)
        return hit

    extension = media_extension(path)
    try:
        if extension in IMAGE_EXTENSIONS:
            source = _first_image_frame(path)
        elif extension in VIDEO_EXTENSIONS:
            source = _video_frame_at(path, 0.5)
        elif extension in AUDIO_EXTENSIONS:
            source = _waveform_image(path, size)
        else:
            return None, None
        if source is None:
            return None, None
        data = _fit_to_webp(source, size)
    except Exception as e:
        print(f"[ComfyUI-RYH] 缩略图生成失败 {path}: {e}")
        return None, None

    entry = (data, f'W/"{mtime_ns:x}-{size}"')
    _thumbnail_cache[key] = entry
    while len(_thumbnail_cache) > _THUMBNAIL_CACHE_LIMIT:
        _thumbnail_cache.popitem(last=False)
    return entry


def _first_image_frame(path):
    with Image.open(path) as img:
        frame = next(iter(ImageSequence.Iterator(img)))
        frame = ImageOps.exif_transpose(frame)
        # 调色板/灰度等模式存不进 webp，统一成 RGB
        return frame if frame.mode in ("RGB", "RGBA") else frame.convert("RGB")


def _video_frame_at(path, position):
    """取视频 position（0~1）处的画面：先 seek 到目标之前的关键帧，再解码到目标时间。"""
    with av.open(path) as container:
        if not container.streams.video:
            return None
        stream = container.streams.video[0]
        duration = (container.duration or 0) / AV_TIME_BASE
        time_base = float(stream.time_base or 0.0)
        target = duration * position
        if duration > 0 and time_base > 0:
            container.seek(int(target / time_base), stream=stream)

        picked = None
        for frame in container.decode(stream):
            picked = frame
            if frame.pts is None or time_base <= 0:
                break
            if frame.pts * time_base + 0.02 >= target:
                break
        if picked is None:
            return None
        return Image.fromarray(picked.to_ndarray(format="rgb24"))


def _waveform_image(path, size):
    waveform, sample_rate = decode_audio_window(path, 0.0, WAVEFORM_MAX_SECONDS)
    if waveform is None or sample_rate <= 0:
        return None
    samples = waveform.mean(dim=0).double().numpy()
    if samples.size == 0:
        return None

    bins = min(WAVEFORM_BINS, max(8, size // 2))
    usable = (samples.size // bins) * bins
    if usable <= 0:
        return None
    peaks = np.abs(samples[:usable].reshape(bins, -1)).max(axis=1)
    peak_max = float(peaks.max()) or 1.0
    peaks = peaks / peak_max

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    bar_width = max(1, size // bins - 1)
    middle = size // 2
    for index, peak in enumerate(peaks):
        x = int(index * size / bins)
        half = max(1, int(peak * middle * 0.9))
        draw.rectangle([x, middle - half, x + bar_width, middle + half], fill=(140, 190, 255, 255))
    return image


def _fit_to_webp(image, size):
    image = ImageOps.fit(image, (size, size), Image.LANCZOS)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="WEBP", quality=80)
    buffer.seek(0)
    return buffer.getvalue()


def _to_float32(buffer):
    """av 交出来的采样位深随编码而变（mp3 常是 int16、flac 可能是 int32），统一成 [-1,1]。"""
    if buffer.dtype.is_floating_point:
        return buffer.float()
    if buffer.dtype == torch.int16:
        return buffer.float() / 32768.0
    if buffer.dtype == torch.int32:
        return buffer.float() / 2147483648.0
    if buffer.dtype == torch.uint8:
        return (buffer.float() - 128.0) / 128.0
    return buffer.float()


def decode_audio_window(path, start_time=0.0, duration=0.0):
    """解码音频窗口，返回 (waveform[C, L] float32, sample_rate)；没有音轨返回 (None, 0)。"""
    with av.open(path) as container:
        if not container.streams.audio:
            return None, 0
        stream = container.streams.audio[0]
        sample_rate = int(stream.codec_context.sample_rate or 0)
        channels = int(stream.channels or 1)
        if sample_rate <= 0:
            return None, 0

        wanted_start = max(0, int(round(start_time * sample_rate)))
        wanted_length = int(round(duration * sample_rate)) if duration and duration > 0 else 0

        chunks = []
        collected = 0
        decoded = 0
        for frame in container.decode(stream):
            buffer = torch.from_numpy(frame.to_ndarray())
            if buffer.ndim == 1:
                buffer = buffer.view(1, -1)
            if buffer.shape[0] != channels:
                # 交错存放的样本要转成官方 AUDIO 用的 [通道, 样本] 布局
                buffer = buffer.reshape(-1, channels).t()
            buffer = _to_float32(buffer)

            frame_length = buffer.shape[1]
            if decoded + frame_length <= wanted_start:
                decoded += frame_length
                continue
            keep = buffer[:, max(0, wanted_start - decoded):]
            chunks.append(keep)
            collected += keep.shape[1]
            decoded += frame_length
            if wanted_length and collected >= wanted_length:
                break

        if not chunks:
            return None, sample_rate
        waveform = torch.cat(chunks, dim=1)
        if wanted_length:
            waveform = waveform[:, :wanted_length]
        return waveform.contiguous(), sample_rate


def load_audio(path, start_time=0.0, duration=0.0):
    """按 ComfyUI 官方 AUDIO 结构返回 {"waveform": [1, C, L], "sample_rate": int}。"""
    waveform, sample_rate = decode_audio_window(path, start_time, duration)
    if waveform is None:
        return None
    return {"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate}


def load_video(path, fps_limit=0, max_frames=0, max_side=0):
    """抽帧解码视频，返回 (images[T, H, W, C] float32, 输出帧率, 对齐长度的 AUDIO)。

    只把选中的帧转成张量：先把整段解码成 float32 再筛帧，1080p 几百帧就是几个 GB。
    音轨截到与输出画面等长，接 H3 连续音频时画面和声音才对得上。
    """
    with av.open(path) as container:
        if not container.streams.video:
            return None, 0.0, None
        stream = container.streams.video[0]
        has_audio = bool(container.streams.audio)
        source_rate = float(stream.average_rate or stream.guessed_rate or 0.0) or 24.0

        keep_every = 1.0
        if fps_limit and 0 < fps_limit < source_rate:
            keep_every = source_rate / fps_limit
        output_rate = source_rate / keep_every

        frames = []
        next_index = 0.0
        index = 0
        for frame in container.decode(stream):
            if index + 1e-6 >= next_index:
                image = Image.fromarray(frame.to_ndarray(format="rgb24"))
                if max_side and max_side > 0:
                    image = ImageOps.contain(image, (max_side, max_side), Image.LANCZOS)
                frames.append(torch.from_numpy(np.array(image).astype(np.float32) / 255.0))
                next_index += keep_every
                if max_frames and max_frames > 0 and len(frames) >= max_frames:
                    break
            index += 1

    if not frames:
        return None, 0.0, None

    images = torch.stack(frames)
    audio = None
    if has_audio:
        window = images.shape[0] / output_rate if output_rate > 0 else 0.0
        audio = load_audio(path, 0.0, window)
    return images, round(output_rate, 6), audio


def load_image(path):
    """解码图片首帧，返回 (IMAGE[H,W,C] float32, MASK)；多帧 GIF/webp 只取第一帧。"""
    img = node_helpers.pillow(Image.open, path)
    frame = next(iter(ImageSequence.Iterator(img)))
    frame = node_helpers.pillow(ImageOps.exif_transpose, frame)

    image = frame.convert("RGB")
    tensor = torch.from_numpy(np.array(image).astype(np.float32) / 255.0)[None,]

    if "A" in frame.getbands():
        alpha = np.array(frame.getchannel("A")).astype(np.float32) / 255.0
        mask = 1.0 - torch.from_numpy(alpha)
    else:
        mask = torch.zeros((64, 64), dtype=torch.float32)

    img.close()
    return tensor, mask

