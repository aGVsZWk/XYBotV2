# -*- coding: utf-8 -*-
# @Time        : 2025/2/22
# @Author      : helei
# @File        : main.py
# @Description :
from loguru import logger
import linecache
import tomllib
import os
import json
from WechatAPI import WechatAPIClient
from utils.decorators import *
from utils.plugin_base import PluginBase
import aiohttp
import traceback
import random


class JokePlugin(PluginBase):
    description = "笑话插件"
    author = "Luke"
    version = "1.0.0"

    # 同步初始化
    def __init__(self):
        super().__init__()

        with open("plugins/Joke/config.toml", "rb") as f:
            plugin_config = tomllib.load(f)

        with open("main_config.toml", "rb") as f:
            main_config = tomllib.load(f)

        config = plugin_config["Joke"]
        main_config = main_config["XYBot"]

        self.enable = config["enable"]
        self.command = config["command"]
        self.version = main_config["version"]

    # 异步初始化
    async def async_init(self):
        return

    def get_joke(self):
        no = random.randint(0, 66028)
        curdir = os.path.dirname(__file__)
        f = "xiaohua.data"
        data_path = os.path.join(curdir, f)
        data = linecache.getline(data_path, no)
        data = json.loads(data)
        output = data["title"] + ": " + data["content"]
        return output

    @on_text_message(priority=81)
    async def handle_pat(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        content = str(message["Content"]).strip()
        command = content.split(" ")
        if command[0] in self.command:
            reply_msg = self.get_joke()
            if message["IsGroup"] is True:
                await bot.send_at_message(message["FromWxid"], reply_msg, [message["SenderWxid"]])
            else:
                await bot.send_text_message(message["FromWxid"], reply_msg)


