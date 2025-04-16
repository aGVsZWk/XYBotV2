# -*- coding: utf-8 -*-
# @Time        : 2025/4/13
# @Author      : helei
# @File        : sink.py
# @Description :
import av
import pilk
import os
from typing import Optional, Union, Dict, List, Tuple

# def to_pcm(in_path: str) -> tuple[str, int]:
#     """任意媒体文件转 pcm"""
#     out_path = os.path.splitext(in_path)[0] + '.pcm'
#     with av.open(in_path) as in_container:
#         in_stream = in_container.streams.audio[0]
#         sample_rate = in_stream.codec_context.sample_rate
#         if sample_rate not in [8000, 12000, 16000, 24000, 32000, 44100, 48000]:
#             sample_rate = 24000
#         with av.open(out_path, 'w', 's16le') as out_container:
#             out_stream = out_container.add_stream(
#                 'pcm_s16le',
#                 rate=sample_rate,
#                 layout='mono'
#             )
#             try:
#                for frame in in_container.decode(in_stream):
#                   frame.pts = None
#                   for packet in out_stream.encode(frame):
#                      out_container.mux(packet)
#             except:
#                pass
#     return out_path, sample_rate


# noinspection PyUnresolvedReferences
def to_pcm(in_path: str) -> Tuple[str, int]:
    """任意媒体文件转 PCM"""
    out_path = os.path.splitext(in_path)[0] + ".pcm"
    with av.open(in_path) as in_container:
        in_stream = in_container.streams.audio[0]
        sample_rate = in_stream.codec_context.sample_rate
        if sample_rate not in [8000, 12000, 16000, 24000, 32000, 44100, 48000]:
            sample_rate = 24000
        with av.open(out_path, "w", "s16le") as out_container:
            out_stream = out_container.add_stream(
                "pcm_s16le", rate=sample_rate, layout="mono"
            )
            try:
                for frame in in_container.decode(in_stream):
                    frame.pts = None
                    for packet in out_stream.encode(frame):
                        out_container.mux(packet)
            except Exception as e:
                print("Warning", e.args)
    return out_path, sample_rate


def convert_to_silk(media_path: str) -> str:
    """任意媒体文件转 silk, 返回silk路径"""
    pcm_path, sample_rate = to_pcm(media_path)
    silk_path = os.path.splitext(pcm_path)[0] + '.silk'
    duration = pilk.encode(pcm_path, silk_path, pcm_rate=sample_rate, tencent=True)
    # duration = pilk.encode(pcm_path, silk_path, pcm_rate=sample_rate, tencent=True)
    print(duration)
    os.remove(pcm_path)
    return silk_path
