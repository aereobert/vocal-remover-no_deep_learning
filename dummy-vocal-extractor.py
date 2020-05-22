import os

import librosa
import numpy as np
import soundfile as sf
import torch
from scipy import interpolate
import math


def crop_center(h1, h2, concat=True):
    # s_freq = (h2.shape[2] - h1.shape[2]) // 2
    # e_freq = s_freq + h1.shape[2]
    h1_shape = h1.size()
    h2_shape = h2.size()
    if h2_shape[3] < h1_shape[3]:
        raise ValueError('h2_shape[3] must be greater than h1_shape[3]')
    s_time = (h2_shape[3] - h1_shape[3]) // 2
    e_time = s_time + h1_shape[3]
    h2 = h2[:, :, :, s_time:e_time]
    if concat:
        return torch.cat([h1, h2], dim=1)
    else:
        return h2


def calc_spec(X, hop_length):
    n_fft = (hop_length - 1) * 2
    audio_left = np.asfortranarray(X[0])
    audio_right = np.asfortranarray(X[1])
    spec_left = librosa.stft(audio_left, n_fft, hop_length=hop_length)
    spec_right = librosa.stft(audio_right, n_fft, hop_length=hop_length)
    spec = np.asfortranarray([spec_left, spec_right])

    return spec


def mask_uninformative(mask, ref, thres=0.3, min_range=64, fade_area=32):
    if min_range < fade_area * 2:
        raise ValueError('min_range must be >= fade_area * 2')
    idx = np.where(ref.mean(axis=(0, 1)) < thres)[0]
    starts = np.insert(idx[np.where(np.diff(idx) != 1)[0] + 1], 0, idx[0])
    ends = np.append(idx[np.where(np.diff(idx) != 1)[0]], idx[-1])
    uninformative = np.where(ends - starts > min_range)[0]
    if len(uninformative) > 0:
        starts = starts[uninformative]
        ends = ends[uninformative]
        old_e = None
        for s, e in zip(starts, ends):
            if old_e is not None and s - old_e < fade_area:
                s = old_e - fade_area * 2
            elif s != 0:
                start_mask = mask[:, :, s:s + fade_area]
                np.clip(
                    start_mask + np.linspace(0, 1, fade_area), 0, 1,
                    out=start_mask)
            if e != mask.shape[2]:
                end_mask = mask[:, :, e - fade_area:e]
                np.clip(
                    end_mask + np.linspace(1, 0, fade_area), 0, 1,
                    out=end_mask)
            mask[:, :, s + fade_area:e - fade_area] = 1
            old_e = e

    return mask

def _lcm(x, y):
    return x * y // math.gcd(x, y)

def _resample(audio,desired_sample_count):
    # upsample by interpolate
    desired_sample_count = int(desired_sample_count)
    x_original = np.linspace(0,audio.shape[1]-1,audio.shape[1])
    left = interpolate.interp1d(x_original, audio[0], kind='cubic')
    right = interpolate.interp1d(x_original, audio[1], kind='cubic')

    x_desired = np.linspace(0,audio.shape[1]-1,desired_sample_count)
    resampled_left = left(x_desired)
    resampled_right = right(x_desired)

    return np.vstack([resampled_left,resampled_right])

def align_wave_head_and_tail(a, b, sr,clip_duration=8,sample_multiple=8,offset = 0):
    a_backup = a
    b_backup = b
    print("upsample...")
    a=_resample(a,a.shape[1]*sample_multiple)
    b = _resample(b, b.shape[1] * sample_multiple)

    print("finding alignment...")

    delta = 1000
    while delta>999:
        a_mono_front = a[:, sr*offset:sr * (clip_duration + offset)].sum(axis=0)
        b_mono_front = b[:, sr*offset:sr * (clip_duration + offset)].sum(axis=0)
        a_mono_front -= a_mono_front.mean()
        b_mono_front -= b_mono_front.mean()
        offset_front = len(a_mono_front) - 1

        back_end = min(a.shape[1], b.shape[1]) - sr * offset
        a_mono_back = a[:, back_end - (sr * clip_duration):back_end].sum(axis=0)[::-1]
        b_mono_back = b[:, back_end - (sr * clip_duration):back_end].sum(axis=0)[::-1]
        a_mono_back -= a_mono_back.mean()
        b_mono_back -= b_mono_back.mean()
        offset_back = len(a_mono_back) - 1
        delay_front = np.argmax(np.correlate(a_mono_front, b_mono_front, 'full')) - offset_front
        delay_back = offset_back - np.argmax(np.correlate(a_mono_back, b_mono_back, 'full'))
        print("delay_front:%d,delay_back:%d"%(delay_front,delay_back))
        delta = delay_back - delay_front
        if delta >1000:
            clip_duration += 1
            offset += 1

    if delay_front > 0:
        a = a[:, delay_front:]
    else:
        b = b[:, np.abs(delay_front):]

    print("aligning...")
    # adjust speed to match
    if abs(delta) == 1:
        pass
    else:
        if delta > 0:
            # make a shorter
            a = _resample(a , a.shape[1] - delta)
            # b = _resample(b,b.shape[1])

            pass
        else:
            # make b shorter
            b = _resample(b, b.shape[1] + delta)
            # a = _resample(a, a.shape[1] )
            pass

        if a.shape[1] < b.shape[1]:
            b = b[:, :a.shape[1]]
        else:
            a = a[:, :b.shape[1]]

    print("downsample...")
    a=_resample(a,a.shape[1]/sample_multiple)
    b = _resample(b, b.shape[1] / sample_multiple)
    return a, b


def cache_or_load(mix_path, inst_path, sr, hop_length):
    _, mix_ext = os.path.splitext(mix_path)
    _, inst_ext = os.path.splitext(inst_path)
    spec_mix_path = mix_path.replace(mix_ext, '.npy')
    spec_inst_path = inst_path.replace(inst_ext, '.npy')

    if os.path.exists(spec_mix_path) and os.path.exists(spec_inst_path):
        X = np.load(spec_mix_path)
        y = np.load(spec_inst_path)
    else:
        X, _ = librosa.load(
            mix_path, sr, False, dtype=np.float32, res_type='kaiser_fast')
        y, _ = librosa.load(
            inst_path, sr, False, dtype=np.float32, res_type='kaiser_fast')
        X, _ = librosa.effects.trim(X)
        y, _ = librosa.effects.trim(y)
        X, y = align_wave_head_and_tail(X, y, sr)

        X = np.abs(calc_spec(X, hop_length))
        y = np.abs(calc_spec(y, hop_length))

        _, ext = os.path.splitext(mix_path)
        np.save(spec_mix_path, X)
        np.save(spec_inst_path, y)

    return X, y

def spec_to_wav(mag, phase, hop_length):
    spec = mag * phase
    spec_left = np.asfortranarray(spec[0])
    spec_right = np.asfortranarray(spec[1])
    wav_left = librosa.istft(spec_left, hop_length=hop_length)
    wav_right = librosa.istft(spec_right, hop_length=hop_length)
    wav = np.asfortranarray([wav_left, wav_right])

    return wav


def batch_generate_dataset(sr=44100,sour='preprocess_input',dest='preprocess_output'):
    flist = []
    for i in os.listdir(os.path.join(sour,'mix')):
        if i.lower().split('.')[-1] in ('wav','flac','mp3'):
            flist.append(i)

    for i in flist:
        print("processing %s" % i)
        mix, _ = librosa.load(
            os.path.join(sour,"mix",i), sr, False, dtype=np.float64, res_type='kaiser_best')
        inst, _ = librosa.load(
            os.path.join(sour,"instrument",i), sr, False, dtype=np.float64, res_type='kaiser_best')
        mix, _ = librosa.effects.trim(mix)
        inst, _ = librosa.effects.trim(inst)
        inst_done, mix_done = align_wave_head_and_tail(inst, mix, sr)

        sf.write(os.path.join(dest,'instrument','%s.wav'%i.split(".")[0]), inst_done.T, sr)
        sf.write(os.path.join(dest,'mix','%s.wav'%i.split(".")[0]), mix_done.T, sr)
        sf.write(os.path.join(dest,'vocal','%s.wav'%i.split(".")[0]), (inst_done - mix_done).T, sr)
    pass

if __name__ == "__main__":
    batch_generate_dataset(sys.argv[1],sys.argv[2],sys.argv[3]) # sample rate, source folder path, destination folder path 
    # both of the folders should contain three folders: instrument, mix, vocal
