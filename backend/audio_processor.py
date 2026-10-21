import os
import tempfile
import warnings
from typing import Optional

import numpy as np
import soundfile as sf
from scipy import signal
from scipy.ndimage import maximum_filter1d

try:
    import noisereduce as nr
    HAS_NOISEREDUCE = True
except ImportError:
    HAS_NOISEREDUCE = False

# STFT 分块大小（帧）。只影响峰值内存，不影响结果：
# 一块约占用 chunk_frames * n_fft * 8 字节（索引矩阵）。
# 1024 帧时约 16MB，适合内存受限的手机环境。
_STFT_CHUNK_FRAMES = 1024

def _ensure_parent_dir(file_path):
    parent = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(parent, exist_ok=True)

def _check_output_path(output_path, *input_paths):
    """禁止输出文件覆盖任意输入文件，包括符号链接指向同一文件的情况。"""
    output_real = os.path.normcase(os.path.realpath(output_path))

    for input_path in input_paths:
        input_real = os.path.normcase(os.path.realpath(input_path))
        if output_real == input_real:
            raise ValueError("输出路径不能与输入文件相同")

        if os.path.exists(output_path):
            try:
                if os.path.samefile(output_path, input_path):
                    raise ValueError("输出路径不能与输入文件相同")
            except OSError:
                pass

def _write_wav_atomic(output_path, y, sr):
    """成功写入临时文件后再替换目标文件，避免失败时留下半个 WAV。"""
    _ensure_parent_dir(output_path)
    parent = os.path.dirname(os.path.abspath(output_path))

    fd, temp_path = tempfile.mkstemp(
        prefix=".audio_",
        suffix=".wav",
        dir=parent,
    )
    os.close(fd)

    try:
        sf.write(
            temp_path,
            np.asarray(y, dtype=np.float32),
            sr,
            format="WAV",
            subtype="PCM_16",
        )
        os.replace(temp_path, output_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

def _resample(y, orig_sr, target_sr):
    """
    重采样，仅依赖 soundfile/numpy/scipy（外加可选的 soxr），
    不引入 librosa / numba。

    优先用 soxr（C 扩展，体积小、质量好）；没有则退回 scipy.resample_poly。
    """
    y = np.asarray(y, dtype=np.float32)
    if orig_sr == target_sr:
        return y

    try:
        import soxr

        return soxr.resample(
            y,
            in_rate=int(orig_sr),
            out_rate=int(target_sr),
        ).astype(np.float32)
    except Exception:
        from math import gcd

        g = gcd(int(orig_sr), int(target_sr))
        up = int(target_sr) // g
        down = int(orig_sr) // g
        return signal.resample_poly(y, up, down).astype(np.float32)

def _load_mono(audio_path, sr):
    # 用 soundfile(libsndfile) 直接解码；本项目对齐链路只喂 WAV，
    # 因此不需要 librosa/audioread 的多格式解码，也避免牵连 numba。
    y, native_sr = sf.read(audio_path, dtype="float32", always_2d=True)
    if y.shape[1] > 1:
        y = y.mean(axis=1)
    else:
        y = y[:, 0]
    y = np.asarray(y, dtype=np.float32)

    if native_sr != sr:
        y = _resample(y, native_sr, sr)

    if y.size == 0:
        raise ValueError(f"音频为空: {audio_path}")

    if not np.all(np.isfinite(y)):
        raise ValueError(f"音频包含 NaN 或无穷值: {audio_path}")

    return y

def _analysis_normalize(y):
    """
    仅用于分析，不改变最终写出的原音乐音量。

    不设置固定的“太小就不归一化”阈值，
    以免把音量很小但仍有内容的录音当成无效信号。
    """
    x = np.asarray(y, dtype=np.float64)
    x = x - np.mean(x)

    peak = float(np.max(np.abs(x)))
    if peak == 0.0:
        raise ValueError("音频没有可用于对齐的变化信号，可能是静音")

    return (x / peak).astype(np.float32)

def _standardize_rows(x):
    """对每个特征维度标准化，并抑制异常尖峰。"""
    x = np.asarray(x, dtype=np.float64)
    x = x - np.mean(x, axis=1, keepdims=True)
    std = np.std(x, axis=1, keepdims=True)

    x = np.divide(
        x,
        std,
        out=np.zeros_like(x),
        where=std > 1e-8,
    )
    return np.clip(x, -5.0, 5.0).astype(np.float32)

def _stft_power(y, n_fft, hop_length, chunk_frames=_STFT_CHUNK_FRAMES):
    """
    中心零填充的分帧 STFT，返回功率谱 (n_freq, n_frames)。
    纯 numpy/scipy 实现，替代 librosa.stft。

    分块计算：一次只把 chunk_frames 帧展开成矩阵，
    使峰值内存与音频长度无关。整段展开时 5 分钟素材会
    临时占用 1GB 以上（手机上必然 OOM）。
    """
    y = np.asarray(y, dtype=np.float32)
    pad = n_fft // 2
    y_pad = np.pad(y, (pad, pad), mode="constant")

    n_frames = 1 + (y_pad.shape[0] - n_fft) // hop_length
    if n_frames < 1:
        # 太短：补到至少一个完整窗。
        y_pad = np.pad(y_pad, (0, max(0, n_fft - y_pad.shape[0])))
        n_frames = 1

    # 周期 Hann 窗（与 librosa 默认的 periodic 窗一致）。
    window = (
        0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n_fft) / n_fft)
    ).astype(np.float32)

    n_freq = n_fft // 2 + 1
    power = np.empty((n_freq, n_frames), dtype=np.float32)

    chunk = max(1, int(chunk_frames))
    offset = 0

    while offset < n_frames:
        count = min(chunk, n_frames - offset)
        start = offset * hop_length

        idx = (
            start
            + np.arange(count)[:, None] * hop_length
            + np.arange(n_fft)[None, :]
        )
        frames = y_pad[idx] * window
        del idx

        spec = np.fft.rfft(frames, n=n_fft, axis=1)
        del frames

        block = spec.real ** 2 + spec.imag ** 2
        power[:, offset:offset + count] = block.T
        del spec, block

        offset += count

    return power  # (n_freq, n_frames)

def _hztomel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f, dtype=np.float64) / 700.0)

def _melt_hz(m):
    return 700.0 * (10.0 ** (np.asarray(m, dtype=np.float64) / 2595.0) - 1.0)

def _mel_filterbank(sr, n_fft, n_mels, fmin, fmax):
    """三角梅尔滤波器组，形状 (n_mels, n_freq)。"""
    n_freq = n_fft // 2 + 1
    mel_pts = _melt_hz(
        np.linspace(_hztomel(fmin), _hztomel(fmax), n_mels + 2)
    )
    bins = np.floor((n_fft + 1) * mel_pts / sr).astype(int)
    bins = np.clip(bins, 0, n_freq - 1)

    basis = np.zeros((n_mels, n_freq), dtype=np.float64)
    for i in range(n_mels):
        left, center, right = bins[i], bins[i + 1], bins[i + 2]

        if center > left:
            ramp = np.arange(left, center)
            basis[i, ramp] = (ramp - left) / (center - left)
        if right > center:
            ramp = np.arange(center, right)
            basis[i, ramp] = (right - ramp) / (right - center)

    return basis

def _power_to_db(S, ref=None, top_db=None):
    """功率转分贝；ref='max' 时峰值对齐到 0 dB，top_db 做下限截断。"""
    S = np.asarray(S, dtype=np.float64)
    db = 10.0 * np.log10(S + np.finfo(np.float64).tiny)

    if ref == "max":
        peak = float(np.max(db))
        if np.isfinite(peak):
            db = db - peak
    elif ref is not None:
        db = db - 10.0 * np.log10(float(ref))

    if top_db is not None:
        db = np.maximum(db, np.max(db) - top_db)

    return db

def _chroma_projection(sr, n_fft, n_chroma=12):
    """
    把每个 FFT 频率 bin 线性映射到其音级(pitch class)，
    并按小数部分分摊到相邻音级，得到 (n_chroma, n_freq) 投影矩阵。
    """
    n_freq = n_fft // 2 + 1
    freqs = np.linspace(0, sr / 2, n_freq)
    mapping = np.zeros((n_chroma, n_freq), dtype=np.float64)

    for k in range(n_freq):
        f = freqs[k]
        if f <= 0.0:
            continue
        midi = 69.0 + 12.0 * np.log2(f / 440.0)
        pc = midi - np.floor(midi / 12.0) * 12.0  # 0..12
        lo = int(np.floor(pc)) % n_chroma
        frac = pc - np.floor(pc)
        mapping[lo, k] += 1.0 - frac
        mapping[(lo + 1) % n_chroma, k] += frac

    return mapping

def _build_feature(y, sr, hop_length=512):
    """
    三组互补特征（纯 numpy/scipy，不依赖 librosa/numba）：
    1. 对数梅尔频谱：描述随时间变化的频谱结构；
    2. 色度：描述音高结构；
    3. 起音：描述节奏变化。

    同一分析窗口用于所有特征，避免帧时间轴不一致。
    下游只做行标准化 + 互相关，所以 mel/dB 约定与 librosa
    略有差异不影响对齐。
    """
    y = _analysis_normalize(y)

    n_fft = 2048
    n_mels = 40
    n_chroma = 12
    upper_hz = min(8000.0, sr * 0.48)

    power = _stft_power(y, n_fft, hop_length)  # (n_freq, n_frames)

    mel_basis = _mel_filterbank(
        sr,
        n_fft,
        n_mels,
        fmin=60.0,
        fmax=upper_hz,
    )
    mel = mel_basis @ power  # (n_mels, n_frames)

    log_mel = _power_to_db(mel, ref="max", top_db=80.0)

    # 去除每一帧共有的响度分量，突出频谱形状。
    spectral_shape = log_mel - np.mean(
        log_mel, axis=0, keepdims=True
    )

    chroma_map = _chroma_projection(sr, n_fft, n_chroma)
    chroma = chroma_map @ power  # (n_chroma, n_frames)

    # 每帧按 L2 归一，避免整体响度主导色度。
    chroma = np.divide(
        chroma,
        np.linalg.norm(chroma, axis=0, keepdims=True),
        out=np.zeros_like(chroma),
        where=np.linalg.norm(chroma, axis=0, keepdims=True) > 1e-12,
    )

    # 从对数梅尔频谱计算正向谱通量。
    flux = np.maximum(np.diff(log_mel, axis=1), 0.0)
    onset = np.concatenate([
        np.zeros(1, dtype=np.float32),
        np.mean(flux, axis=0),
    ])[None, :]

    frames = min(
        spectral_shape.shape[1],
        chroma.shape[1],
        onset.shape[1],
    )

    if frames < 3:
        raise ValueError("音频过短，无法建立可靠的对齐特征")

    # 每组先标准化，再加权。
    # 除以 sqrt(维数)，避免 40 维梅尔天然压过其他特征。
    mel_feature = (
        _standardize_rows(spectral_shape[:, :frames])
        * np.sqrt(0.55 / spectral_shape.shape[0])
    )
    chroma_feature = (
        _standardize_rows(chroma[:, :frames])
        * np.sqrt(0.30 / chroma.shape[0])
    )
    onset_feature = (
        _standardize_rows(onset[:, :frames])
        * np.sqrt(0.15)
    )

    return np.vstack([
        mel_feature,
        chroma_feature,
        onset_feature,
    ])

def _feature_correlation(query_feat, ref_feat, min_overlap_frames):
    """
    lag 的定义与旧代码一致：
        query[t] 对应 reference[t - lag]

    对每个 lag，根据实际重叠区间计算归一化相关性。
    """
    q = np.asarray(query_feat, dtype=np.float64)
    r = np.asarray(ref_feat, dtype=np.float64)

    nq = q.shape[1]
    nr = r.shape[1]
    lags = signal.correlation_lags(nq, nr, mode="full")

    q_start = np.maximum(lags, 0)
    r_start = np.maximum(-lags, 0)

    # 完全无重叠的 lag 会让 overlap 变成 0 甚至负数，
    # 继续拿它做下标会产生回绕/除零。这里先夹到 0。
    overlap = np.maximum(
        np.minimum(nq - q_start, nr - r_start),
        0,
    )

    q_end = q_start + overlap
    r_end = r_start + overlap

    # 除法的分母：这些 lag 最终会被 valid 掩码滤掉，
    # 这里只保证中间计算不产生 nan/inf。
    safe_overlap = np.where(overlap > 0, overlap, 1)

    numerator = np.zeros(len(lags), dtype=np.float64)
    q_energy = np.zeros(len(lags), dtype=np.float64)
    r_energy = np.zeros(len(lags), dtype=np.float64)

    for qi, ri in zip(q, r):
        numerator += signal.correlate(
            qi, ri, mode="full", method="fft"
        )

        q_sum = np.concatenate([[0.0], np.cumsum(qi)])
        r_sum = np.concatenate([[0.0], np.cumsum(ri)])
        q_sq = np.concatenate([[0.0], np.cumsum(qi * qi)])
        r_sq = np.concatenate([[0.0], np.cumsum(ri * ri)])

        qs = q_sum[q_end] - q_sum[q_start]
        rs = r_sum[r_end] - r_sum[r_start]

        # 把相关值转换为重叠区间内的去均值相关值。
        numerator -= qs * rs / safe_overlap

        q_energy += np.maximum(
            q_sq[q_end] - q_sq[q_start] - qs * qs / safe_overlap,
            0.0,
        )
        r_energy += np.maximum(
            r_sq[r_end] - r_sq[r_start] - rs * rs / safe_overlap,
            0.0,
        )

    denominator = np.sqrt(q_energy * r_energy)
    valid = (
        (overlap >= min_overlap_frames)
        & (denominator > 1e-12)
    )

    scores = np.full(len(lags), -np.inf, dtype=np.float64)
    scores[valid] = np.clip(
        numerator[valid] / denominator[valid],
        -1.0,
        1.0,
    )

    return lags, scores, overlap

def _find_candidates(
    query_feat,
    ref_feat,
    analysis_sr,
    hop_length,
    max_shift_seconds,
    min_overlap_seconds,
    candidate_count,
    fine_radius_seconds,
):
    shorter = min(query_feat.shape[1], ref_feat.shape[1])

    # 短素材也至少要求覆盖较短音频的大部分；
    # 长素材默认至少 3 秒或较短音频的 25%，取较大者。
    requested = max(
        int(np.ceil(min_overlap_seconds * analysis_sr / hop_length)),
        int(np.ceil(shorter * 0.25)),
    )
    minimum = min(requested, max(3, int(shorter * 0.8)))

    lags, scores, overlap = _feature_correlation(
        query_feat, ref_feat, minimum
    )

    if max_shift_seconds is not None:
        allowed = (
            np.abs(lags * hop_length / analysis_sr)
            <= max_shift_seconds
        )
        scores[~allowed] = -np.inf

    if not np.any(np.isfinite(scores)):
        raise ValueError("没有有效对齐候选，请检查音频长度或偏移限制")

    # 相邻候选的精修范围尽量不重复。
    separation = max(
        1,
        int(np.ceil(
            2.0 * fine_radius_seconds
            * analysis_sr / hop_length
        )),
    )

    local_max = maximum_filter1d(
        scores,
        size=2 * separation + 1,
        mode="constant",
        cval=-np.inf,
    )

    peak_indices = np.flatnonzero(
        np.isfinite(scores) & (scores == local_max)
    )
    order = peak_indices[np.argsort(scores[peak_indices])[::-1]]

    chosen = []
    for index in order:
        if all(
            abs(int(lags[index]) - item["lag_frames"]) >= separation
            for item in chosen
        ):
            chosen.append({
                "lag_frames": int(lags[index]),
                "feature_score": float(scores[index]),
                "overlap_frames": int(overlap[index]),
            })

        if len(chosen) >= candidate_count:
            break

    return chosen

def _prepare_waveform(y, sr):
    """精修用波形：保留较稳定的中频区域，降低低频风噪等影响。"""
    y = _analysis_normalize(y)
    upper = min(5000.0, sr * 0.45)

    sos = signal.butter(
        4,
        [100.0, upper],
        btype="bandpass",
        fs=sr,
        output="sos",
    )

    if len(y) > 100:
        y = signal.sosfiltfilt(sos, y)

    return _analysis_normalize(y)

def _sliding_pearson(long_y, short_y):
    """
    返回 short_y 在 long_y 每个合法位置上的相关系数。
    使用 FFT，一次计算整个搜索范围。
    """
    short_y = np.asarray(short_y, dtype=np.float64)
    long_y = np.asarray(long_y, dtype=np.float64)

    short_y = short_y - np.mean(short_y)
    n = len(short_y)
    short_energy = float(np.dot(short_y, short_y))

    sums = np.concatenate([[0.0], np.cumsum(long_y)])
    squares = np.concatenate([[0.0], np.cumsum(long_y * long_y)])

    window_sum = sums[n:] - sums[:-n]
    window_energy = (
        squares[n:] - squares[:-n] - window_sum * window_sum / n
    )
    window_energy = np.maximum(window_energy, 0.0)

    numerator = signal.correlate(
        long_y, short_y, mode="valid", method="fft"
    )
    denominator = np.sqrt(short_energy * window_energy)

    result = np.divide(
        numerator,
        denominator,
        out=np.zeros_like(numerator),
        where=denominator > 1e-12,
    )

    return np.clip(result, -1.0, 1.0)

def _refine_candidate(
    query_y,
    ref_y,
    sr,
    coarse_lag,
    radius_seconds,
    max_shift_seconds,
):
    """
    在多个分散窗口中，对粗定位附近逐采样点搜索。

    每个窗口比较同一长度的音频，避免重叠长度影响分数。
    """
    radius = max(1, int(round(radius_seconds * sr)))
    low = coarse_lag - radius
    high = coarse_lag + radius

    if max_shift_seconds is not None:
        limit = int(np.floor(max_shift_seconds * sr))
        low = max(low, -limit)
        high = min(high, limit)

    if low > high:
        return coarse_lag, 0.0, 0

    # 找出对全部待搜索 lag 都有效的 query 区间。
    start = max(0, high)
    end = min(len(query_y), len(ref_y) + low)
    available = end - start

    if available < int(0.5 * sr):
        return coarse_lag, 0.0, 0

    window = min(int(6.0 * sr), available)

    # 最多检查 5 个分散窗口；短音频减少窗口数。
    count = min(5, max(1, available // window))
    starts = np.unique(
        np.linspace(start, end - window, count).astype(np.int64)
    )

    curves = []

    for q_start in starts:
        q_start = int(q_start)
        q_segment = query_y[q_start:q_start + window]

        r_start = q_start - high
        r_end = q_start - low + window
        r_segment = ref_y[r_start:r_end]

        if np.std(q_segment) < 1e-7 or np.std(r_segment) < 1e-7:
            continue

        # 第 j 个位置对应 lag = high - j。
        curve = _sliding_pearson(r_segment, q_segment)
        curves.append(curve[::-1])

    if not curves:
        return coarse_lag, 0.0, 0

    # 各窗口一起投票。全局极性反转也能匹配，
    # 但不同窗口中正负不一致的偶然相关会相互抵消。
    combined = np.mean(curves, axis=0)
    best_index = int(np.argmax(np.abs(combined)))

    return (
        int(low + best_index),
        float(abs(combined[best_index])),
        len(curves),
    )

def _apply_lag(ref_y, target_length, lag_samples):
    """移动 reference，并严格输出 query 的采样点数量。"""
    aligned = np.zeros(target_length, dtype=np.float32)

    source_start = max(0, -lag_samples)
    target_start = max(0, lag_samples)

    count = min(
        len(ref_y) - source_start,
        target_length - target_start,
    )

    if count > 0:
        aligned[target_start:target_start + count] = (
            ref_y[source_start:source_start + count]
        )

    return aligned

def denoise_audio(
    audio_path: str,
    output_audio_path: Optional[str] = None,
    sr: int = 44100,
) -> str:
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"音频不存在: {audio_path}")

    if output_audio_path is None:
        base, _ = os.path.splitext(audio_path)
        output_audio_path = base + ".denoised.wav"

    _check_output_path(output_audio_path, audio_path)
    y = _load_mono(audio_path, sr)

    if HAS_NOISEREDUCE:
        try:
            # 保守降噪，减少对音乐瞬态和泛音的破坏。
            y = nr.reduce_noise(
                y=y,
                sr=sr,
                stationary=False,
                prop_decrease=0.6,
            )
        except Exception as exc:
            warnings.warn(
                f"降噪失败，使用原音频继续处理: {exc}",
                RuntimeWarning,
            )

    y = np.asarray(y, dtype=np.float32)

    if not np.all(np.isfinite(y)):
        raise ValueError("降噪结果包含无效数值")

    peak = float(np.max(np.abs(y)))
    if peak > 1.0:
        y = y / peak * 0.98

    _write_wav_atomic(output_audio_path, y, sr)
    return output_audio_path


def apply_manual_lag(
    query_audio_path: str,
    reference_audio_path: str,
    output_audio_path: str,
    lag_seconds: float,
    sr: int = 44100,
) -> dict:
    """
    人工指定延迟对齐：不做任何搜索，直接把 reference 平移 lag_seconds。

    跟 align_audio() 的关系：
        align_audio 是「猜」——用频谱特征 + 波形相关性找最可能的偏移；
        本函数是「照办」——用户听出来差多少，就直接按这个数位移。

    符号约定与 align_audio 完全一致（底层都走 _apply_lag）：
        lag_seconds > 0  → reference 往后推（音乐延后），开头补静音
        lag_seconds < 0  → reference 往前挪（音乐提前），掐掉开头
        lag_seconds = 0  → 原样对齐到 0 秒

    返回值字段与 align_audio 保持同构，这样 pipeline 和历史面板
    不用区分两种来源（confidence 固定为 "manual"）。
    """
    if not os.path.isfile(query_audio_path):
        raise FileNotFoundError(f"query 音频不存在: {query_audio_path}")

    if not os.path.isfile(reference_audio_path):
        raise FileNotFoundError(f"reference 音频不存在: {reference_audio_path}")

    if sr <= 0:
        raise ValueError("采样率设置不合法")

    if lag_seconds is None or not np.isfinite(lag_seconds):
        raise ValueError("人工延迟必须是有限的秒数")

    # 与 align_audio 一样，避免输出路径和输入相同导致写坏源文件。
    _check_output_path(
        output_audio_path,
        query_audio_path,
        reference_audio_path,
    )

    # query 只用来取长度：最终音频必须和视频等长。
    query_y = _load_mono(query_audio_path, sr)
    ref_y = _load_mono(reference_audio_path, sr)

    lag_samples = int(round(float(lag_seconds) * sr))
    aligned_y = _apply_lag(ref_y, len(query_y), lag_samples)

    peak = float(np.max(np.abs(aligned_y))) if aligned_y.size else 0.0
    if peak > 1.0:
        aligned_y = aligned_y / peak * 0.98

    _write_wav_atomic(output_audio_path, aligned_y, sr)

    return {
        "output_audio_path": output_audio_path,
        "lag_samples": lag_samples,
        "lag_seconds": float(lag_seconds),
        "coarse_lag_seconds": float(lag_seconds),
        "refined_lag_seconds": float(lag_seconds),
        # 不是统计意义上的可信度，只是标明来源，避免 UI 误显示成"低可信"
        "confidence": "manual",
        "selection_method": "manual_lag",
        "manual": True,
        "warnings": [],
        "accepted_by_policy": True,
    }


def align_audio(
    query_audio_path: str,
    reference_audio_path: str,
    output_audio_path: str = None,
    sr: int = 44100,
    analysis_sr: int = 22050,
    hop_length: int = 512,
    max_shift_seconds: Optional[float] = None,
    fine_tune: bool = True,
    *,
    min_overlap_seconds: float = 3.0,
    candidate_count: int = 5,
    fine_radius_seconds: float = 0.20,
) -> dict:
    """
    将 reference 外部音乐移动到 query 视频音轨的时间轴上。

    原有参数和返回字段保留，新增参数为仅关键字参数。

    注意：
    - 只处理固定时间偏移，不做变速或时间拉伸。
    - 输出仍为单声道 WAV，保持旧版行为。
    - confidence 是启发式提示，不是统计概率。
    """
    if not os.path.isfile(query_audio_path):
        raise FileNotFoundError(f"query 音频不存在: {query_audio_path}")

    if not os.path.isfile(reference_audio_path):
        raise FileNotFoundError(
            f"reference 音频不存在: {reference_audio_path}"
        )

    if sr <= 0 or analysis_sr < 4000 or hop_length <= 0:
        raise ValueError("采样率或 hop_length 设置不合法")
    if candidate_count < 1:
        raise ValueError("candidate_count 必须 >= 1")
    if min_overlap_seconds <= 0 or fine_radius_seconds <= 0:
        raise ValueError("重叠时长和精修范围必须 > 0")
    if max_shift_seconds is not None:
        if not np.isfinite(max_shift_seconds) or max_shift_seconds < 0:
            raise ValueError("max_shift_seconds 必须是非负有限数")

    if output_audio_path is None:
        base, _ = os.path.splitext(reference_audio_path)
        output_audio_path = base + ".aligned.wav"

    _check_output_path(
        output_audio_path,
        query_audio_path,
        reference_audio_path,
    )

    query_y = _load_mono(query_audio_path, sr)
    ref_y = _load_mono(reference_audio_path, sr)

    # 从已读取的音频降采样，避免重复解码文件。
    if sr == analysis_sr:
        query_a = query_y.copy()
        ref_a = ref_y.copy()
    else:
        query_a = _resample(query_y, sr, analysis_sr)
        ref_a = _resample(ref_y, sr, analysis_sr)

    query_feat = _build_feature(query_a, analysis_sr, hop_length)
    ref_feat = _build_feature(ref_a, analysis_sr, hop_length)

    candidates = _find_candidates(
        query_feat=query_feat,
        ref_feat=ref_feat,
        analysis_sr=analysis_sr,
        hop_length=hop_length,
        max_shift_seconds=max_shift_seconds,
        min_overlap_seconds=min_overlap_seconds,
        candidate_count=candidate_count,
        fine_radius_seconds=fine_radius_seconds,
    )

    for item in candidates:
        item["coarse_lag_samples"] = (
            item["lag_frames"] * hop_length
        )
        item["fine_lag_samples"] = item["coarse_lag_samples"]
        item["waveform_score"] = 0.0
        item["window_count"] = 0

    if fine_tune:
        query_wave = _prepare_waveform(query_a, analysis_sr)
        ref_wave = _prepare_waveform(ref_a, analysis_sr)

        for item in candidates:
            lag, waveform_score, windows = _refine_candidate(
                query_y=query_wave,
                ref_y=ref_wave,
                sr=analysis_sr,
                coarse_lag=item["coarse_lag_samples"],
                radius_seconds=fine_radius_seconds,
                max_shift_seconds=max_shift_seconds,
            )
            item["fine_lag_samples"] = lag
            item["waveform_score"] = waveform_score
            item["window_count"] = windows

    # 默认相信频谱粗定位；波形证据足够明确时，
    # 允许其他候选纠正粗定位。
    selected = candidates[0]
    selection_method = "feature"

    if fine_tune:
        waveform_order = sorted(
            candidates,
            key=lambda item: item["waveform_score"],
            reverse=True,
        )
        best_wave = waveform_order[0]
        runner_score = (
            waveform_order[1]["waveform_score"]
            if len(waveform_order) > 1
            else 0.0
        )

        if (
            best_wave["waveform_score"] >= 0.08
            and best_wave["waveform_score"] - runner_score >= 0.02
        ):
            selected = best_wave
            selection_method = "feature_and_waveform"

    # 波形相关性太低时，不让精修引入随机微调。
    use_refinement = (
        fine_tune and selected["waveform_score"] >= 0.03
    )

    final_analysis_lag = (
        selected["fine_lag_samples"]
        if use_refinement
        else selected["coarse_lag_samples"]
    )

    lag_seconds = final_analysis_lag / analysis_sr
    lag_samples = int(round(lag_seconds * sr))

    aligned_y = _apply_lag(ref_y, len(query_y), lag_samples)

    peak = float(np.max(np.abs(aligned_y)))
    if peak > 1.0:
        aligned_y = aligned_y / peak * 0.98

    feature_score = selected["feature_score"]
    waveform_score = selected["waveform_score"]

    alternatives = [
        item["feature_score"]
        for item in candidates
        if item is not selected
    ]
    feature_margin = (
        feature_score - max(alternatives)
        if alternatives
        else 0.0
    )

    # 这些阈值用于提示，后续应按你的实际素材校准。
    if (
        waveform_score >= 0.20
        and selected["window_count"] >= 2
        and feature_score >= 0.10
    ):
        confidence = "high"
    elif (
        waveform_score >= 0.08
        or (feature_score >= 0.35 and feature_margin >= 0.05)
    ):
        confidence = "medium"
    else:
        confidence = "low"

    warning_messages = []
    if confidence == "low":
        warning_messages.append(
            "对齐可信度较低，建议试听；目标音乐可能过弱、"
            "存在重复段落，或两段音频有速度差异。"
        )

    if not use_refinement and fine_tune:
        warning_messages.append(
            "波形证据不足，保留频谱粗定位结果。"
        )

    _write_wav_atomic(output_audio_path, aligned_y, sr)

    return {
        # 原有字段
        "output_audio_path": output_audio_path,
        "lag_samples": lag_samples,
        "lag_seconds": lag_seconds,
        "coarse_lag_frames": selected["lag_frames"],
        "coarse_lag_seconds": (
            selected["lag_frames"] * hop_length / analysis_sr
        ),
        "query_duration": len(query_y) / sr,
        "reference_duration": len(ref_y) / sr,
        "output_duration": len(aligned_y) / sr,
        "score": feature_score,
        "match_ratio": waveform_score,

        # 新增字段
        "feature_score": feature_score,
        "waveform_score": waveform_score,
        "confidence": confidence,
        "selection_method": selection_method,
        "fine_tune_applied": use_refinement,
        "matched_windows": selected["window_count"],
        "warnings": warning_messages,
        "candidates": [
            {
                "coarse_lag_seconds": (
                    item["coarse_lag_samples"] / analysis_sr
                ),
                "refined_lag_seconds": (
                    item["fine_lag_samples"] / analysis_sr
                ),
                "feature_score": item["feature_score"],
                "waveform_score": item["waveform_score"],
                "matched_windows": item["window_count"],
            }
            for item in candidates
        ],
    }