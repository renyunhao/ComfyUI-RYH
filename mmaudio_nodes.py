# -*- coding: utf-8 -*-
r"""RYH · MMAudio Segmented —— 长视频分段 Video-to-Audio 节点。

设计要点
--------
* 输入直接用上游（MiniMaxH3Director）的 IMAGE 帧序列，不落盘、不调用 ffmpeg。
  帧的绝对时间就是 i / fps，因此 Gradio/CLI 版里的 ffmpeg 切段、B 帧时间戳偏移、
  24/25 帧率重采样误差、临时 mp4 全部不存在。
* 按绝对时间切成 6~8 秒的段（贴近 8s 训练长度），逐段 generate()，
  再用 overlap-add + sin/cos 交叉淡化拼回一条与视频等长的音轨。
* 段内按目标采样率 8fps(CLIP) / 25fps(Synchformer) 做 ceil 重采样，复刻
  mmaudio/data/av_utils.py::read_frames 的行为，但作用在张量索引上。
* 输出 ComfyUI 原生 AUDIO：waveform (1, 2, N) 双声道（两路相同，与 Gradio
  原版 make_video 走 PyAV 上混出的 dual-mono 一致），sample_rate 44100。
  可直接接 VHS VideoCombine 的 optional audio 输入。

依赖安装记录（ComfyUI portable, Python 3.13.9）
---------------------------------------------
2026-11-15  python_embeded\python.exe -m pip install --no-deps \
                colorlog==6.12.0 torchdiffeq==0.2.5
  原因：mmaudio/eval_utils.py 顶层 import colorlog；
        mmaudio/model/flow_matching.py 顶层 from torchdiffeq import odeint。
  用 --no-deps 是为了不让 pip 改动已有的 torch 2.13.0+cu132。
  本环境其余依赖自带，版本清单见同目录 requirements-mmaudio.txt。

模型选择（遵守 ComfyUI 规范，权重一律放 ComfyUI/models/mmaudio）
--------------------------------------------------------------
* checkpoint / vae_model / synchformer_model 下拉只从 ComfyUI/models/mmaudio
  列文件（走 folder_paths）。D:/AI/NSFW_MMaudio 现在只用来 import mmaudio 源码，
  不再从那里读权重。
* 2026-11-15 已把三份权重复制进规范目录：
  nsfw_gold_8.5k_final.pth (3.9G) / v1-44.pth (1.2G) /
  synchformer_state_dict.pth (907M)。原文件保留供 Gradio/CLI 版继续使用。
* checkpoint_path_override：可填绝对路径覆盖下拉，换权重无需重启。
* 下拉在节点注册时生成，往目录里新增权重需重启 ComfyUI 刷新列表。
* 换权重会自动释放旧的常驻模型；IS_CHANGED 纳入当前选中文件的 mtime。
"""

import copy
import gc
import math
import os
import sys
from pathlib import Path

import torch

# 与 app.py 一致：有代理时 hf-mirror 会 308 回 huggingface.co，
# huggingface_hub>=1.x 会拒绝；必须在 open_clip / huggingface_hub 导入前处理。
if (os.environ.get('HF_ENDPOINT', '').rstrip('/') == 'https://hf-mirror.com'
        and (os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy'))):
    os.environ.pop('HF_ENDPOINT', None)

# 源码目录：只用来把 mmaudio 包加进 sys.path，不从这里读权重。
MMAUDIO_ROOT = Path(r'D:\AI\NSFW_MMaudio')
# 权重规范位置：ComfyUI/models/mmaudio
# (__file__ 在 custom_nodes/ComfyUI-RYH 下，往上三级即 ComfyUI 根目录)
MODELS_DIR = Path(__file__).resolve().parents[2] / 'models' / 'mmaudio'
CKPT_PATH = MODELS_DIR / 'nsfw_gold_8.5k_final.pth'      # 默认主权重
VAE_PATH = MODELS_DIR / 'v1-44.pth'
SYNCH_PATH = MODELS_DIR / 'synchformer_state_dict.pth'

_MODEL_EXTS = ('.pth', '.safetensors', '.ckpt', '.pt')
_AUTO_VAE = 'auto (内置 v1-44.pth)'
_AUTO_SYNCH = 'auto (内置 synchformer)'
_NONE_FOUND = '<未找到权重，请填 checkpoint_path_override>'

_CLIP_FPS = 8.0
_SYNC_FPS = 25.0
_CLIP_SIZE = 384
_SYNC_SIZE = 224
_SAMPLE_RATE = 44100

# 重模型延迟导入：ComfyUI 启动时会 import 所有 custom_nodes 的顶层，
# 这里只留轻量的 torch，真正的 mmaudio 在节点首次执行时才加载。
_EM = None
_MODEL_CACHE = {}

# 下拉标签 → 绝对路径。由 INPUT_TYPES 构建，运行时查表；
# 用标签而不是路径，是为了让 UI 里显示短名字。
_MODEL_REGISTRY = {}


def _log(msg):
    print(f'[RYH MMAudio] {msg}', flush=True)


def _scan_models_dir():
    """兜底：直接扫规范目录顶层（folder_paths 不可用时）。"""
    out = []
    if MODELS_DIR.is_dir():
        for p in sorted(MODELS_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in _MODEL_EXTS:
                out.append((p.name, p))
    return out


def _comfy_models():
    """只从 ComfyUI/models/mmaudio 列权重，走 folder_paths 规范接口。"""
    try:
        import folder_paths
        if 'mmaudio' not in folder_paths.folder_names_and_paths:
            folder_paths.add_model_folder_path('mmaudio', str(MODELS_DIR))
        out = []
        for rel in folder_paths.get_filename_list('mmaudio'):
            if os.path.splitext(rel)[1].lower() not in _MODEL_EXTS:
                continue
            full = folder_paths.get_full_path('mmaudio', rel)
            if full:
                out.append((rel, Path(full)))
        return out
    except Exception as e:
        _log(f'folder_paths 扫描失败，改用直接扫描: {e}')
        return _scan_models_dir()


def _build_model_choices():
    """构建 checkpoint / vae / synchformer 三个下拉，并刷新 _MODEL_REGISTRY。"""
    global _MODEL_REGISTRY
    _MODEL_REGISTRY = {}
    seen, candidates = set(), []
    for label, path in _comfy_models():
        if str(path) not in seen:
            seen.add(str(path))
            candidates.append((label, path))

    ckpts, vaes, synchs = [], [], []
    for label, path in candidates:
        n = path.name.lower()
        _MODEL_REGISTRY[label] = path
        if 'synchformer' in n:
            synchs.append(label)
        elif 'vae' in n or 'v1-44' in n or 'v1_44' in n:
            vaes.append(label)
        elif not any(k in n for k in ('bigvgan', 'clip', 'dfn5b')):
            ckpts.append(label)

    if not ckpts:
        ckpts = [_NONE_FOUND]
    return (ckpts, [_AUTO_VAE] + vaes, [_AUTO_SYNCH] + synchs)


def _default_checkpoint(choices):
    for label in choices:
        if 'nsfw_gold_8.5k_final' in label:
            return label
    return choices[0]


def _resolve_model(label, override='', kind='权重'):
    """下拉标签 / 绝对路径覆盖 → 实际 Path。"""
    if override and str(override).strip():
        p = Path(str(override).strip().strip('"'))
        if not p.is_file():
            raise FileNotFoundError(f'{kind} 自定义路径不存在: {p}')
        return p
    if label in (None, '', _NONE_FOUND) and CKPT_PATH.is_file():
        # 兼容缺少 checkpoint 输入的旧调用 / 空列表
        return CKPT_PATH
    if not _MODEL_REGISTRY:
        _build_model_choices()
    if label in _MODEL_REGISTRY:
        p = _MODEL_REGISTRY[label]
        if p.is_file():
            return p
    if label and Path(label).is_file():     # 允许老工作流直接传路径
        return Path(label)
    raise FileNotFoundError(
        f'找不到{kind}: {label!r}。请检查权重文件，或用 checkpoint_path_override 填绝对路径。')


def _resolve_vae_or_synch(label, kind):
    if label == _AUTO_VAE:
        return VAE_PATH
    if label == _AUTO_SYNCH:
        return SYNCH_PATH
    return _resolve_model(label, kind=kind)


def _ensure_mmaudio():
    global _EM
    if _EM is not None:
        return _EM
    if not MMAUDIO_ROOT.is_dir():
        raise RuntimeError(f'MMAudio 项目目录不存在: {MMAUDIO_ROOT}')
    root = str(MMAUDIO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    from torchvision.transforms import v2
    from mmaudio.eval_utils import generate
    from mmaudio.model.flow_matching import FlowMatching
    from mmaudio.model.networks import get_my_mmaudio
    from mmaudio.model.sequence_config import CONFIG_44K
    from mmaudio.model.utils.features_utils import FeaturesUtils

    _EM = dict(generate=generate, FlowMatching=FlowMatching,
               get_my_mmaudio=get_my_mmaudio, CONFIG_44K=CONFIG_44K,
               FeaturesUtils=FeaturesUtils, v2=v2)
    return _EM


def _devices():
    try:
        import comfy.model_management as mm
        return mm.get_torch_device(), mm.unet_offload_device(), mm
    except ImportError:
        return (torch.device('cuda' if torch.cuda.is_available() else 'cpu'),
                torch.device('cpu'), None)


def _load_model(ckpt_path, vae_path, synch_path, precision):
    """加载并缓存模型。同一组权重再次执行时只搬回 device，不重新读盘。"""
    em = _ensure_mmaudio()
    device, offload, _ = _devices()
    dtype = torch.float16 if precision == 'fp16' else torch.float32
    key = (str(ckpt_path), str(vae_path), str(synch_path), precision)

    if key in _MODEL_CACHE:
        bundle = _MODEL_CACHE[key]
        bundle['net'].to(device)
        bundle['feature_utils'].to(device)
        return bundle

    for p in (ckpt_path, vae_path, synch_path):
        if not p.is_file():
            raise FileNotFoundError(f'MMAudio 权重缺失: {p}')

    # 换了权重就先释放旧 bundle，避免两份 ~10GB 模型同时常驻
    if _MODEL_CACHE:
        for old in _MODEL_CACHE.values():
            old['net'].to('cpu')
            old['feature_utils'].to('cpu')
        _MODEL_CACHE.clear()
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    _log(f'加载模型 {ckpt_path.name} ({precision})，约 10GB，请稍候 ...')
    net = em['get_my_mmaudio']('large_44k')
    state = torch.load(str(ckpt_path), map_location='cpu', weights_only=True)
    net.load_weights(state)
    del state
    net = net.to(device, dtype).eval()

    feature_utils = em['FeaturesUtils'](
        tod_vae_ckpt=str(vae_path),
        synchformer_ckpt=str(synch_path),
        enable_conditions=True,
        mode='44k',
        bigvgan_vocoder_ckpt=None,
        need_vae_encoder=False,
    )
    feature_utils = feature_utils.to(device, dtype).eval()

    seq_cfg = copy.copy(em['CONFIG_44K'])
    bundle = dict(net=net, feature_utils=feature_utils, seq_cfg=seq_cfg,
                  device=device, offload=offload, dtype=dtype)
    _MODEL_CACHE[key] = bundle
    _log('模型加载完成')
    return bundle


def plan_segments(total_dur, seg, overlap):
    """返回 [(start_sec, end_sec), ...]，与 segment_video_to_audio.py 保持一致。"""
    if seg <= 0:
        raise ValueError('seg_len 必须大于 0')
    if seg < 1.0:
        raise ValueError('seg_len 不能小于 1 秒：Synchformer 同步窗口不足')
    if seg < 4.0:
        _log(f'警告: seg_len={seg}s 偏短，同步窗口不足 11 个，推荐 6~8 秒')
    if overlap < 0 or overlap >= seg:
        raise ValueError('overlap 必须 >= 0 且小于 seg_len')
    step = max(seg - overlap, 1e-3)
    segments, start = [], 0.0
    while start < total_dur - 1e-3:
        end = min(start + seg, total_dur)
        segments.append((round(start, 3), round(end, 3)))
        if end >= total_dur - 1e-3:
            break
        start += step
    return segments


def fit_audio_length(audio, target_samples):
    n = audio.numel()
    if n < target_samples:
        return torch.nn.functional.pad(audio, (0, target_samples - n))
    if n > target_samples:
        return audio[:target_samples]
    return audio


def crossfade_join(audio_list, sr, overlap, starts, total_samples):
    """按每段在原视频中的起始时间做 overlap-add + sin/cos 交叉淡化 + 权重归一化。"""
    audio_list = [a.reshape(-1).float() for a in audio_list]
    ov = int(round(overlap * sr)) if overlap > 0 else 0
    if ov <= 0 or len(audio_list) == 1:
        return fit_audio_length(torch.cat(audio_list, dim=0), total_samples)

    acc = torch.zeros(total_samples)
    wsum = torch.zeros(total_samples)
    last = len(audio_list) - 1
    for i, (a, start) in enumerate(zip(audio_list, starts)):
        pos = int(round(start * sr))
        if pos >= total_samples:
            continue
        n = min(a.numel(), total_samples - pos)
        if n <= 0:
            continue
        w = torch.ones(n)
        k = min(ov, n)
        ramp = torch.linspace(0.0, math.pi / 2, k)
        if i > 0:
            w[:k] = torch.sin(ramp)
        if i < last:
            w[-k:] = torch.minimum(w[-k:], torch.cos(ramp))
        acc[pos:pos + n] += a[:n] * w
        wsum[pos:pos + n] += w
    return acc / wsum.clamp(min=1e-6)


def _sample_indices(start_sec, count, target_fps, src_fps, num_frames):
    """ceil 重采样：采样时刻 k/target_fps 取第一个 pts >= 它的源帧。

    复刻 read_frames 的 `while frame_time >= next_frame_time`，但用整数索引表达。
    源帧 i 的时间为 i/src_fps；段起点对应的源帧为 ceil(start*src_fps)。
    24fps→25fps 时约 4% 采样会落到同一源帧，属正常的上采样复制。
    """
    s_idx = math.ceil(start_sec * src_fps - 1e-9)
    ratio = src_fps / target_fps
    out = []
    for k in range(count):
        i = s_idx + math.ceil(k * ratio - 1e-9)
        out.append(min(max(i, 0), num_frames - 1))
    return out


def _build_frames(images, start_sec, end_sec, src_fps, em):
    """从整个 IMAGE 序列中按时间戳取出一段的 clip/sync 帧，返回带 batch 维的张量。

    先索引再 permute，避免把整段 60s / 上千帧的 IMAGE 全量复制一遍。
    """
    v2 = em['v2']
    n_total = images.shape[0]

    # 关键：把段时长量化到 25fps 帧网格，使 seq_cfg.sync_seq_len 与
    # Synchformer 实际滑出的窗口数严格一致，否则会触发形状断言失败。
    n_sync = max(int(round((end_sec - start_sec) * _SYNC_FPS)), 16)
    duration = n_sync / _SYNC_FPS
    n_clip = max(int(duration * _CLIP_FPS), 1)

    sync_idx = torch.tensor(
        _sample_indices(start_sec, n_sync, _SYNC_FPS, src_fps, n_total), dtype=torch.long)
    clip_idx = torch.tensor(
        _sample_indices(start_sec, n_clip, _CLIP_FPS, src_fps, n_total), dtype=torch.long)

    # ComfyUI IMAGE 是 (N, H, W, C) float32 [0,1]
    sync_x = images[sync_idx].permute(0, 3, 1, 2).float()
    clip_x = images[clip_idx].permute(0, 3, 1, 2).float()

    # 与 mmaudio/eval_utils.py::load_video 的 transform 一致，
    # 只去掉 ToImage()（输入已经是 tensor）；ToDtype(scale=True) 对 float 不缩放。
    clip_transform = v2.Compose([
        v2.Resize((_CLIP_SIZE, _CLIP_SIZE), interpolation=v2.InterpolationMode.BICUBIC),
        v2.ToDtype(torch.float32, scale=True),
    ])
    sync_transform = v2.Compose([
        v2.Resize(_SYNC_SIZE, interpolation=v2.InterpolationMode.BICUBIC),
        v2.CenterCrop(_SYNC_SIZE),
        v2.ToDtype(torch.float32, scale=True),
        v2.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
    ])

    clip_frames = clip_transform(clip_x).unsqueeze(0)
    sync_frames = sync_transform(sync_x).unsqueeze(0)
    return clip_frames, sync_frames, duration


class RYH_MMAudioSegmented:
    """把长 IMAGE 序列分段生成音轨，输出可直接接 VHS VideoCombine 的 AUDIO。"""

    @classmethod
    def INPUT_TYPES(cls):
        ckpts, vaes, synchs = _build_model_choices()
        return {
            "required": {
                "images": ("IMAGE",),
                "checkpoint": (ckpts, {
                    "default": _default_checkpoint(ckpts),
                    "tooltip": "主微调权重。候选来自 ComfyUI/models/mmaudio；往目录新增文件后需重启 ComfyUI 刷新列表",
                }),
                "fps": ("FLOAT", {
                    "default": 24.0, "min": 1.0, "max": 120.0, "step": 0.001,
                    "tooltip": "接 MiniMaxH3Director 的 fps 输出；必须与 VideoCombine 的 frame_rate 一致",
                }),
                "prompt": ("STRING", {"default": "", "multiline": True}),
                "negative_prompt": ("STRING", {"default": "music", "multiline": True}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 2 ** 31 - 1,
                                 "tooltip": "-1 为随机；>=0 时每段按 seed+段序号确定性派生"}),
                "steps": ("INT", {"default": 25, "min": 1, "max": 200}),
                "cfg": ("FLOAT", {"default": 4.5, "min": 1.0, "max": 20.0, "step": 0.1}),
                "seg_len": ("FLOAT", {"default": 8.0, "min": 1.0, "max": 60.0, "step": 0.5,
                                      "tooltip": "推荐 6~8 秒（模型训练长度 8 秒）"}),
                "overlap": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 10.0, "step": 0.1}),
            },
            "optional": {
                "vae_model": (vaes, {"default": _AUTO_VAE,
                                     "tooltip": "VAE 权重，一般保持 auto（models/mmaudio/v1-44.pth）"}),
                "synchformer_model": (synchs, {"default": _AUTO_SYNCH,
                                               "tooltip": "Synchformer 权重，一般保持 auto（models/mmaudio）"}),
                "checkpoint_path_override": ("STRING", {
                    "default": "",
                    "tooltip": "可选：直接填主权重绝对路径，优先级高于 checkpoint 下拉；换权重无需重启 ComfyUI",
                }),
                "force_offload": ("BOOLEAN", {"default": True,
                                              "tooltip": "生成后把模型搬回 offload device 并清显存"}),
                "precision": (["fp32", "fp16"], {"default": "fp32"}),
            },
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)
    FUNCTION = "generate_audio"
    CATEGORY = "RYH/MMAudio"
    DESCRIPTION = (
        "Segmented MMAudio video-to-audio. Takes the upstream IMAGE sequence directly "
        "(no temp video files), splits it into 6-8s segments, generates audio per segment "
        "and crossfades them into one track matching the video duration. "
        "Pick the fine-tuned checkpoint from the dropdown, or override with an absolute path."
    )

    @classmethod
    def IS_CHANGED(cls, checkpoint=None, checkpoint_path_override="", **kwargs):
        # 权重文件被替换（同名但内容变了）时强制重跑。
        # ComfyUI 对输入值变化本来就会重跑，这里只补 mtime 维度。
        try:
            if checkpoint_path_override and str(checkpoint_path_override).strip():
                p = Path(str(checkpoint_path_override).strip().strip('"'))
            elif checkpoint and checkpoint in _MODEL_REGISTRY:
                p = _MODEL_REGISTRY[checkpoint]
            else:
                p = CKPT_PATH
            return str(p.stat().st_mtime_ns)
        except OSError:
            return 'missing'

    # 必须整体处于 inference_mode：FeaturesUtils 内部用 @torch.inference_mode()
    # 产出 inference tensor，若外层的 preprocess_conditions/clip_input_proj 不在
    # 同一模式下，nn.Linear 会抛 "Inference tensors cannot be saved for backward"。
    # 与 app.py::video_to_audio / segment_video_to_audio.py::gen_segment_audio 一致。
    @torch.inference_mode()
    def generate_audio(self, images, fps, prompt, negative_prompt, seed, steps, cfg,
                       seg_len, overlap, checkpoint=None,
                       vae_model=_AUTO_VAE, synchformer_model=_AUTO_SYNCH,
                       checkpoint_path_override="", force_offload=True, precision="fp32"):
        ckpt = _resolve_model(checkpoint, checkpoint_path_override, kind='主权重')
        vae = _resolve_vae_or_synch(vae_model, 'VAE')
        synch = _resolve_vae_or_synch(synchformer_model, 'Synchformer')
        _log(f'权重: {ckpt.name} / {vae.name} / {synch.name}')
        fps = float(fps)
        seed = int(seed)
        steps = int(steps)
        seg_len = float(seg_len)
        overlap = float(overlap)
        if fps <= 0:
            raise ValueError('fps 必须大于 0')
        if steps < 1:
            raise ValueError('steps 必须 >= 1')
        if images.ndim != 4 or images.shape[-1] != 3:
            raise ValueError(f'images 应为 (N,H,W,3)，实际 {tuple(images.shape)}')

        num_frames = images.shape[0]
        total_dur = num_frames / fps
        segments = plan_segments(total_dur, seg_len, overlap)
        total_samples = int(round(total_dur * _SAMPLE_RATE))
        _log(f'{num_frames} 帧 @ {fps}fps = {total_dur:.3f}s → {len(segments)} 段 '
             f'(seg={seg_len}s, overlap={overlap}s)')

        em = _ensure_mmaudio()
        bundle = _load_model(ckpt, vae, synch, precision)
        net = bundle['net']
        feature_utils = bundle['feature_utils']
        seq_cfg = bundle['seq_cfg']
        device, dtype = bundle['device'], bundle['dtype']

        try:
            from comfy.utils import ProgressBar
            pbar = ProgressBar(len(segments))
        except Exception:
            pbar = None

        rng_base = torch.Generator(device='cpu')
        if seed >= 0:
            rng_base.manual_seed(seed)
        else:
            rng_base.seed()

        audio_list = []
        try:
            for i, (s, e) in enumerate(segments):
                _log(f'[{i + 1}/{len(segments)}] {s:.2f}-{e:.2f}s')
                clip_frames, sync_frames, duration = _build_frames(images, s, e, fps, em)

                seq_cfg.duration = duration
                net.update_seq_lengths(seq_cfg.latent_seq_len,
                                       seq_cfg.clip_seq_len,
                                       seq_cfg.sync_seq_len)
                fm = em['FlowMatching'](min_sigma=0, inference_mode='euler', num_steps=steps)
                rng = torch.Generator(device=device)
                if seed >= 0:
                    rng.manual_seed(seed + i)
                else:
                    rng.manual_seed(int(torch.randint(
                        0, 2 ** 31 - 1, (1,), generator=rng_base).item()))

                audios = em['generate'](
                    clip_frames.to(device, dtype),
                    sync_frames.to(device, dtype),
                    [prompt],
                    negative_text=[negative_prompt],
                    feature_utils=feature_utils,
                    net=net,
                    fm=fm,
                    rng=rng,
                    cfg_strength=float(cfg),
                )
                audio_list.append(audios.float().cpu()[0].reshape(-1))
                del clip_frames, sync_frames, audios, fm
                gc.collect()
                if pbar is not None:
                    pbar.update(1)
        finally:
            if force_offload:
                net.to(bundle['offload'])
                feature_utils.to(bundle['offload'])
                try:
                    _, _, mm = _devices()
                    if mm is not None:
                        mm.soft_empty_cache()
                except Exception:
                    torch.cuda.empty_cache()

        joined = crossfade_join(audio_list, _SAMPLE_RATE, overlap,
                                [s for s, _ in segments], total_samples)
        mono = joined.reshape(1, 1, -1)
        # 双声道：两路内容相同，与原版 make_video 走 PyAV 上混出的 dual-mono 一致。
        # VHS VideoCombine 用 waveform.size(1) 决定 -ac，故无需再手动上混。
        # 临时退出 inference_mode 再构造输出：torch.from_numpy 在
        # inference_mode 内仍会产出 inference tensor，下游音频节点若要
        # clone / requires_grad_ 会报错。这里交出去的是普通可写张量。
        with torch.inference_mode(False):
            waveform = torch.cat([mono, mono], dim=1).contiguous().clone()
        _log(f'完成，音频 {joined.numel() / _SAMPLE_RATE:.3f}s '
             f'({joined.numel()} samples, stereo)')
        return ({"waveform": waveform, "sample_rate": _SAMPLE_RATE},)


NODE_CLASS_MAPPINGS = {
    "RYH_MMAudioSegmented": RYH_MMAudioSegmented,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "RYH_MMAudioSegmented": "MMAudio Segmented (RYH)",
}
