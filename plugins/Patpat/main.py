from loguru import logger
import tomllib

from WechatAPI import WechatAPIClient
from utils.decorators import *
from utils.plugin_base import PluginBase
import aiohttp
import traceback
import random


class PatPlugin(PluginBase):
    description = "拍一拍插件"
    author = "Luke"
    version = "1.0.0"

    # 同步初始化
    def __init__(self):
        super().__init__()

        with open("plugins/Patpat/config.toml", "rb") as f:
            plugin_config = tomllib.load(f)

        with open("main_config.toml", "rb") as f:
            main_config = tomllib.load(f)

        config = plugin_config["Patpat"]
        main_config = main_config["XYBot"]

        self.enable = config["enable"]
        self.version = main_config["version"]
    # 异步初始化
    async def async_init(self):
        return

    async def get_hitokoto_info(self, c=None):
        """
        从『一言』获取信息。(官网：https://hitokoto.cn/)
        :return: str,一言。
        """
        try:
            if c:
                url = "https://v1.hitokoto.cn?c=%s" % c
            else:
                url = "https://v1.hitokoto.cn"

            async with aiohttp.ClientSession() as session:
                # response = await session.post(f'http://{self.ip}:{self.port}/GetContractDetail', json=json_param)
                # json_resp = await response.json()
                response = await session.get(url, params={'encode': 'text'})
                return await response.text()

        except Exception as e:
            logger.error(f"程序发生错误: {e}")
            logger.error(traceback.format_exc())
            logger.info("等待文件改变后自动重启...")
            return "一言获取失败"

    @on_pat_message
    async def handle_pat(self, bot: WechatAPIClient, message: dict):
        logger.info("收到了拍一拍消息")
        if not self.enable:
            return

        reply_msg = await self.get_hitokoto_info(c=random.choice(["a", "b", "c", "d", "e", "f", "g", "h", "j", "k"]))
        # reply_msg = "来自拍一拍: " + reply_msg
        if message["IsGroup"] is True:
            await bot.send_at_message(message["FromWxid"], reply_msg, [message["Patted"]])
        else:
            await bot.send_text_message(message["FromWxid"], reply_msg)
