import asyncio

import requests
import os
import av
import pilk
import tempfile
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


url = "https://api.siliconflow.cn/v1/audio/speech"

payload = {
    "model": "FunAudioLLM/CosyVoice2-0.5B",
    "input": "临时文件在python项目中时常会被使用到，其作用在于随机化的创建不重名的文件，路径一般都是放在Linux系统下的/tmp目录。如果项目中并不需要持久化的存储一个文件，就可以采用临时文件的形式进行存储和读取，在使用之后可以自行决定是删除还是保留。",
    "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
    "response_format": "wav",
    "sample_rate": 16000,
    "stream": False,
    "speed": 1,
    "gain": 0
}
headers = {
    "Authorization": "Bearer sk-nuquonnskijxjttcuubbnqfoohieknygtthpsstwrzzuqdqn",
    "Content-Type": "application/json"
}

# response = requests.request("POST", url, json=payload, headers=headers)
#
# # print(response.text)
tf = tempfile.NamedTemporaryFile(suffix=".wav")
# tf.write(response.content)
# p = str(tf.name)
# print(p)
# silk_voice = convert_to_silk(p)


import aiohttp
async def run():
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 200:
                audio = await resp.read()
                tf.write(audio)
                silk_voice = convert_to_silk(str(tf.name))
                print(silk_voice)
                # await bot.send_voice_message(message["FromWxid"], voice=silk_voice, format="wav")
            else:
                # logger.error(f"text-to-audio 接口调用失败: {resp.status} - {await resp.text()}")
                # await bot.send_text_message(message["FromWxid"], TEXT_TO_VOICE_FAILED)
                pass
asyncio.run(run())



tf.close()
