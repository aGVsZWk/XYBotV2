import random
import tomllib

from WechatAPI import WechatAPIClient
from utils.decorators import *
from utils.plugin_base import PluginBase


class RandomMember(PluginBase):
    description = "随机群成员"
    author = "HenryXiaoYang"
    version = "1.0.0"

    def __init__(self):
        super().__init__()

        with open("plugins/RandomMember/config.toml", "rb") as f:
            plugin_config = tomllib.load(f)

        config = plugin_config["RandomMember"]

        self.enable = config["enable"]
        self.command = config["command"]
        self.count = config["count"]

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

    @on_text_message
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        content = str(message["Content"]).strip()
        command = content.split(" ")

        if command[0] not in self.command:
            return

        if not message["IsGroup"]:
            await bot.send_text_message(message["FromWxid"], "-----Y3I3-----\nð 只能在群里使用！")
            return

        memlist = await bot.get_chatroom_member_list(message["FromWxid"])
        random_members = random.sample(memlist, self.count)

        reply_msg = await self.get_hitokoto_info(c=random.choice(["a", "b", "c", "d", "e", "f", "g", "h", "j", "k"]))
        senders = [message["SenderWxid"]]

        output = "\n-----XYBot-----\nð嘿嘿，我随机选到了这几位："
        for member in random_members:
            output += f"\n✨{member['NickName']}"
            senders.append(member["SenderWxid"])
        output += f"\n" + reply_msg
        
        await bot.send_at_message(message["FromWxid"], output, senders)
        
