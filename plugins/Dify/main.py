import json
import mimetypes
import os
import tomllib
import traceback
import urllib

import aiohttp
import filetype
from loguru import logger
import random
from WechatAPI import WechatAPIClient
from database.database import BotDatabase
from utils.decorators import *
from utils.plugin_base import PluginBase
import re


def handle_sentences(text):
    # 使用正则表达式匹配句子边界（中文句号和问号），保留分割符号
    sentences = re.findall(r'.*?[。！]', text)
    # 处理最后一个句子可能没有结束符号的情况
    last_part = text[len(''.join(sentences)):]
    if last_part:
        sentences.append(last_part)
    ret = [s.strip() for s in sentences if s.strip()]
    return ret


def clean_response(content):
    content = re.sub(r'\[.*?\]', '', content, flags=re.DOTALL)
    # 去除Markdown加粗和倾斜标记
    content = re.sub(r'\*\*\*', '', content)
    content = re.sub(r'\*\*', '', content)
    content = re.sub(r'\*', '', content)
    # 去除行首的#和-
    content = re.sub(r'^\t*[#-]+', '', content, flags=re.MULTILINE)

    content = re.sub(r'\n+', '\n', content)
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    return content.strip()


class Dify(PluginBase):
    description = "Dify插件"
    author = "HenryXiaoYang"
    version = "1.1.0"

    # Change Log
    # 1.1.0 2025-02-20 插件优先级，插件阻塞
    # 1.2.0 2025-02-22 有插件阻塞了，other-plugin-cmd可删了

    def __init__(self):
        super().__init__()

        with open("main_config.toml", "rb") as f:
            config = tomllib.load(f)

        self.admins = config["XYBot"]["admins"]

        with open("plugins/Dify/config.toml", "rb") as f:
            config = tomllib.load(f)

        plugin_config = config["Dify"]

        self.enable = plugin_config["enable"]
        self.api_key_keai = plugin_config["keai-api-key"]
        self.api_key_zorg = plugin_config["zorg-api-key"]
        self.api_key_y3i3 = plugin_config["y3i3-api-key"]
        self.api_key = self.api_key_keai
        # self.api_key = plugin_config["api-key"]
        self.base_url = plugin_config["base-url"]
        self.base_resource_path = plugin_config["base-resource-path"]

        self.commands = plugin_config["commands"]
        self.command_tip = plugin_config["command-tip"]
        self.other_plugin_cmd = plugin_config["other-plugin-cmd"]


        self.price = plugin_config["price"]
        self.admin_ignore = plugin_config["admin_ignore"]
        self.whitelist_ignore = plugin_config["whitelist_ignore"]

        self.http_proxy = plugin_config["http-proxy"]

        self.db = BotDatabase()

    @on_text_message(priority=20)
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        command = str(message["Content"]).strip().split(" ")

        if (not command or command[0] not in self.commands) and message["IsGroup"]:  # 不是指令，且是群聊
            return
        elif len(command) == 1 and command[0] in self.commands:  # 只是指令，但没请求内容
            await bot.send_at_message(message["FromWxid"], "\n" + self.command_tip, [message["SenderWxid"]])
            return

        elif command and command[0] in self.other_plugin_cmd:  # 指令来自其他插件
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            if "小可爱" in message["Content"]:
                key = self.api_key_keai
                # query = message["Content"].replace("小可爱", "", 1).lstrip()
            elif "ZORG" in message["Content"] or "zorg" in message["Content"]:
                # query = message["Content"].replace("ZORG", "", 1).replace("zorg", "", 1).lstrip()
                key = self.api_key_zorg
            elif "Y3I3" in message["Content"] or "y3i3" in message["Content"]:
                # query = message["Content"].replace("Y3I3", "", 1).replace("y3i3", "", 1).lstrip()
                key = self.api_key_y3i3
            else:
                key = self.api_key_keai
                # query = message["Content"]
            self.api_key = key
            await self.dify(bot, message, message["Content"])
        return False

    @on_at_message(priority=20)
    async def handle_at(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            await self.dify(bot, message, message["Content"])

        return False

    @on_voice_message(priority=20)
    async def handle_voice(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            upload_file_id = await self.upload_file(message["FromWxid"], message["Content"])
            files = [
                {
                    "type": "audio",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_file_id
                }
            ]
            logger.info("upload voice file finish")
            await self.dify(bot, message, " \n", files)
        return False

    @on_image_message(priority=20)
    async def handle_image(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        # if message["IsGroup"]:
        #     return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            upload_file_id = await self.upload_file(message["FromWxid"], bot.base64_to_byte(message["Content"]))

            files = [
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_file_id
                }
            ]

            await self.dify(bot, message, " \n", files)

        return False

    @on_video_message(priority=20)
    async def handle_video(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            upload_file_id = await self.upload_file(message["FromWxid"], bot.base64_to_byte(message["Video"]))

            files = [
                {
                    "type": "video",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_file_id
                }
            ]

            await self.dify(bot, message, " \n", files)

        return False

    @on_file_message(priority=20)
    async def handle_file(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            upload_file_id = await self.upload_file(message["FromWxid"], message["Content"])

            files = [
                {
                    "type": "document",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_file_id
                }
            ]

            await self.dify(bot, message, " \n", files)

        return False

    async def dify(self, bot: WechatAPIClient, message: dict, query: str, files=None):
        if files is None:
            files = []
        conversation_id = self.db.get_llm_thread_id(message["FromWxid"],
                                                    namespace="dify")
        logger.info(f"query: {query}")

        headers = {"Authorization": f"Bearer {self.api_key}",  # TODO
                   "Content-Type": "application/json"}
        payload = json.dumps({
            "inputs": {},
            "query": query,
            "response_mode": "streaming",
            "conversation_id": conversation_id,
            "user": message["FromWxid"],
            "files": files,
            "auto_generate_name": False,
        })
        url = f"{self.base_url}/chat-messages"

        ai_resp = ""
        async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
            async with session.post(url=url, headers=headers, data=payload) as resp:
                if resp.status == 200:
                    # 读取响应
                    async for line in resp.content:  # 流式传输
                        line = line.decode("utf-8").strip()
                        if not line or line == "event: ping":  # 空行或ping
                            continue
                        elif line.startswith("data: "):  # 脑瘫吧，为什么前面要加 "data: " ？？？
                            line = line[6:]
                        try:
                            print(line)
                            resp_json = json.loads(line)
                        except json.decoder.JSONDecodeError:
                            logger.error(f"Dify返回的JSON解析错误，请检查格式: {line}")

                        event = resp_json.get("event", "")
                        if event == "message":  # LLM 返回文本块事件
                            ai_resp += resp_json.get("answer", "")
                        elif event == "message_replace":  # 消息内容替换事件
                            ai_resp = resp_json("answer", "")
                        elif event == "message_file":  # 文件事件 目前dify只输出图片
                            await self.dify_handle_image(bot, message, resp_json.get("url", ""))
                        elif event == "tts_message":  # TTS 音频流结束事件
                            await self.dify_handle_audio(bot, message, resp_json.get("audio", ""))
                        elif event == "error":  # 流式输出过程中出现的异常
                            await self.dify_handle_error(bot, message,
                                                         resp_json.get("task_id", ""),
                                                         resp_json.get("message_id", ""),
                                                         resp_json.get("status", ""),
                                                         resp_json.get("code", ""),
                                                         resp_json.get("message", ""))
                    new_con_id = resp_json.get("conversation_id", "")
                    if new_con_id and new_con_id != conversation_id:
                        self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")

                elif resp.status == 404:
                    self.db.save_llm_thread_id(message["FromWxid"], "", "dify")
                    return await self.dify(bot, message, query)

                elif resp.status == 400:
                    return await self.handle_400(bot, message, resp)

                elif resp.status == 500:
                    return await self.handle_500(bot, message)

                else:
                    return await self.handle_other_status(bot, message, resp)

        if ai_resp:
            await self.dify_handle_text(bot, message, ai_resp)

    async def upload_file(self, user: str, file: bytes):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # user multipart/form-data
        kind = filetype.guess(file)
        formdata = aiohttp.FormData()
        formdata.add_field("user", user)
        formdata.add_field("file", file, filename=kind.extension, content_type=kind.mime)
        url = f"{self.base_url}/files/upload"
        async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
            async with session.post(url, headers=headers, data=formdata) as resp:
                resp_json = await resp.json()
        return resp_json.get("id", "")

    async def download_file(self, url: str) -> tuple[bytes, str]:
        async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
            async with session.get(url) as resp:
                content_type = resp.headers.get('Content-Type', '')
                return await resp.read(), content_type

    async def dify_handle_text(self, bot: WechatAPIClient, message: dict, text: str):
        # 匹配Dify返回的图片引用格式
        image_pattern = r'\[(.*?)\]\((.*?)\)'
        matches = re.findall(image_pattern, text)
        # 移除所有图片引用文本
        text = re.sub(image_pattern, '', text)

        # if text:
        #     if "@" in text or not message["IsGroup"]:
        #         for _text in handle_sentences(text):
        #             _text = _text.replace(" ", "\u2005")
        #             await asyncio.sleep(random.random() * 5)
        #             await bot.send_text_message(message["FromWxid"], _text)
        #         return
        #     else:
        #         try:
        #             text = json.loads(text)
        #             test_data = {
        #                 'video': text["url"],
        #                 'title': text["desc"],
        #                 'name': text["nickname"],
        #                 'cover': 'https://is1-ssl.mzstatic.com/image/thumb/Purple221/v4/7c/49/e1/7c49e1af-ce92-d1c4-9a93-0a316e47ba94/AppIcon_TikTok-0-0-1x_U007epad-0-1-0-0-85-220.png/512x512bb.jpg'
        #             }
        #             logger.info("开始发送测试卡片")
        #             logger.debug(f"测试数据: {test_data}")
        #             # 发送测试卡片
        #             await bot.send_link_message(
        #                 wxid=message["FromWxid"],
        #                 url=test_data['video'],
        #                 title=f"{test_data['title'][:30]} - {test_data['name'][:10]}",
        #                 description=text["desc"],
        #                 thumb_url=test_data['cover']
        #             )
        #         except:
        #             text = text
        #             await asyncio.sleep(random.random() * 5)
        #             await bot.send_at_message(message["FromWxid"], text, [message["SenderWxid"]])
        # 先发送文字内容
        if text:
            paragraphs = text.split("//n")
            for paragraph in paragraphs:
                if paragraph.strip():
                    await bot.send_text_message(message["FromWxid"], paragraph.strip())
            # if message["MsgType"] == 34 or self.voice_reply_all:
            #     await self.text_to_voice_message(bot, message, text)
            # else:
            #     paragraphs = text.split("//n")
            #     for paragraph in paragraphs:
            #         if paragraph.strip():
            #             await bot.send_text_message(message["FromWxid"], paragraph.strip())
        # 如果有图片引用，只处理最后一个
        if matches:
            filename, url = matches[-1]  # 只取最后一个图片
            try:
                # 如果URL是相对路径,添加base_url
                if url.startswith('/files') and ".mp3" not in url:
                    # 移除base_url中可能的v1路径
                    base_url = self.base_url.replace('/v1', '')
                    url = f"{base_url}{url}"
                    logger.debug(f"处理图片链接: {url}")
                    headers = {"Authorization": f"Bearer {self.api_key}"}
                    async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status == 200:
                                image_data = await resp.read()
                                await bot.send_image_message(message["FromWxid"], image_data)
                            else:
                                logger.error(f"下载图片失败: HTTP {resp.status}")
                                await bot.send_text_message(message["FromWxid"], f"下载图片失败: HTTP {resp.status}")
            except Exception as e:
                logger.error(f"处理图片 {url} 失败: {e}")
                await bot.send_text_message(message["FromWxid"], f"处理图片失败: {str(e)}")

        # pattern = r"\]\((https?:\/\/[^\s\)]+)\)"
        pattern = r"\]\(([^\s\)]+)\)"
        links = re.findall(pattern, text)
        for _url in links:
            url = self.base_resource_path + _url
            logger.info(url)
            file, content_type = await self.download_file(url)
            extension = filetype.guess_extension(file)
            if extension in ('wav', 'mp3'):
                await bot.send_voice_message(message["FromWxid"], voice=file, format=extension)
            elif extension in ('jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg'):
                await bot.send_image_message(message["FromWxid"], file)
            elif extension in ('mp4', 'avi', 'mov', 'mkv', 'flv'):
                await bot.send_video_message(message["FromWxid"], video=file, image="None")

        # 识别普通文件链接
        file_pattern = r'\.(?:pdf|doc|docx|xls|xlsx|txt|zip|rar|7z|tar|gz)'
        file_links = re.findall(file_pattern, text)
        for _url in file_links:
            url = self.base_resource_path + _url
            await self.download_and_send_file(bot, message, url)


        # pattern = r'\[[^\]]+\]\(https?:\/\/[^\s\)]+\)'
        pattern = r'\[[^\]]+\]\([^\s\)]+\)'
        text = re.sub(pattern, '', text)

    async def download_and_send_file(self, bot: WechatAPIClient, message: dict, url: str):
        """下载并发送文件"""
        try:
            # 从URL中获取文件名
            parsed_url = urllib.parse.urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if not filename:
                filename = "downloaded_file"

            logger.debug(f"开始下载文件: {url}")
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        await bot.send_text_message(message["FromWxid"], f"下载文件失败: HTTP {resp.status}")
                        return
                    content = await resp.read()
                    # 检测文件类型
                    kind = filetype.guess(content)
                    if kind is None:
                        # 如果无法检测文件类型,尝试从Content-Type或URL获取
                        content_type = resp.headers.get('Content-Type', '')
                        ext = mimetypes.guess_extension(content_type) or os.path.splitext(filename)[1]
                        if not ext:
                            await bot.send_text_message(message["FromWxid"], f"无法识别文件类型: {filename}")
                            return
                    else:
                        ext = f".{kind.extension}"

                    # 确保文件名有扩展名
                    if not os.path.splitext(filename)[1]:
                        filename = f"{filename}{ext}"

                    # 根据文件类型发送不同类型的消息
                    if ext.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.bmp']:
                        await bot.send_image_message(message["FromWxid"], content)
                    elif ext.lower() in ['.mp3', '.wav', '.ogg', 'm4a']:
                        await bot.send_voice_message(message["FromWxid"], voice=content, format=ext[1:])
                    elif ext.lower() in ['.mp4', '.avi', '.mov', '.mkv']:
                        await bot.send_video_message(message["FromWxid"], video=content, image="None")
                    else:
                        # 其他类型文件，发送文件内容
                        await bot.send_text_message(message["FromWxid"],
                                                    f"文件名: {filename}\n内容长度: {len(content)} 字节")

                    logger.debug(f"文件 {filename} 发送成功")

        except Exception as e:
            logger.error(f"下载或发送文件失败: {e}")
            await bot.send_text_message(message["FromWxid"], f"处理文件失败: {str(e)}")

    async def dify_handle_image(self, bot: WechatAPIClient, message: dict, image: Union[str, bytes]):
        if isinstance(image, str) and image.startswith("http"):
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.get(image) as resp:
                    image = bot.byte_to_base64(await resp.read())
        elif isinstance(image, bytes):
            image = bot.byte_to_base64(image)

        await bot.send_image_message(message["FromWxid"], image)

    @staticmethod
    async def dify_handle_audio(bot: WechatAPIClient, message: dict, audio: str):

        await bot.send_voice_message(message["FromWxid"], audio)

    @staticmethod
    async def dify_handle_error(bot: WechatAPIClient, message: dict, task_id: str, message_id: str, status: str,
                                code: int, err_message: str):
        output = ("-----XYBot-----\n"
                  "ð对不起，Dify出现错误！\n"
                  f"任务 ID：{task_id}\n"
                  f"消息唯一 ID：{message_id}\n"
                  f"HTTP 状态码：{status}\n"
                  f"错误码：{code}\n"
                  f"错误信息：{err_message}")
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_400(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        output = ("-----XYBot-----\n"
                  "ð对不起，出现错误！\n"
                  f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_500(bot: WechatAPIClient, message: dict):
        output = "-----XYBot-----\nð对不起，Dify服务内部异常，请稍后再试。"
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_other_status(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        ai_resp = ("-----XYBot-----\n"
                   f"ð对不起，出现错误！\n"
                   f"状态码：{resp.status}\n"
                   f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_at_message(message["FromWxid"], "\n" + ai_resp, [message["SenderWxid"]])

    @staticmethod
    async def hendle_exceptions(bot: WechatAPIClient, message: dict):
        output = ("-----XYBot-----\n"
                  "ð对不起，出现错误！\n"
                  f"错误信息：\n"
                  f"{traceback.format_exc()}")
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    async def _check_point(self, bot: WechatAPIClient, message: dict) -> bool:
        wxid = message["SenderWxid"]

        if wxid in self.admins and self.admin_ignore:
            return True
        elif self.db.get_whitelist(wxid) and self.whitelist_ignore:
            return True
        else:
            if self.db.get_points(wxid) < self.price:
                await bot.send_at_message(message["FromWxid"],
                                          f"\n-----XYBot-----\n"
                                          f"ð­你的积分不够啦！需要 {self.price} 积分",
                                          [wxid])
                return False

            self.db.add_points(wxid, -self.price)
            return True

