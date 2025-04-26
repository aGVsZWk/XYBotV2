import io
import re
import subprocess
import tomllib
from typing import Optional, Union, Dict, List, Tuple
import urllib.parse
import mimetypes
import aiohttp
import filetype
from loguru import logger
from WechatAPI import WechatAPIClient
from database.database import BotDatabase as XYBotDB, ChatHistoryDatabase
from utils.decorators import *
from utils.plugin_base import PluginBase
import traceback
import shutil
from PIL import Image
import base64
import json
import time
import tempfile
import os
from collections import defaultdict
from .silk import convert_to_silk, to_pcm
from .xunfei import SliceIdGenerator, XunfeiRequestApi
from .model import *
from datetime import datetime
import random
# from se import is_quiet_time

# 常量定义
XYBOT_PREFIX = "-----老夏的金库-----\n"
DIFY_ERROR_MESSAGE = "🙅对不起，Dify出现错误！\n"
INSUFFICIENT_POINTS_MESSAGE = "😭你的积分不够啦！需要 {price} 积分"
VOICE_TRANSCRIPTION_FAILED = "\n语音转文字失败"
TEXT_TO_VOICE_FAILED = "\n文本转语音失败"
MESSAGE_BUFFER_TIMEOUT = 10  # 消息缓冲区超时时间（秒）
MAX_BUFFERED_MESSAGES = 10  # 最大缓冲消息数

# 聊天室消息模板
CHAT_JOIN_MESSAGE = """✨ 欢迎来到聊天室！让我们开始愉快的对话吧~

💡 基础指引：
   📝 直接发消息与我对话
   🚪 发送"退出聊天"离开
   ⏰ 5分钟不说话自动暂离
   🔄 30分钟无互动将退出

🎮 聊天指令：
   📊 发送"查看状态"
   📈 发送"聊天室排行"
   👤 发送"我的统计"
   💤 发送"暂时离开"

开始聊天吧！期待与你的精彩对话~ 🌟"""

CHAT_LEAVE_MESSAGE = "👋 已退出聊天室，需要再次@我才能继续对话"
CHAT_TIMEOUT_MESSAGE = "由于您已经1小时没有活动，已被移出聊天室。如需继续对话，请重新发送消息。"
CHAT_AWAY_MESSAGE = "💤 已设置为离开状态，其他人将看到你正在休息"
CHAT_BACK_MESSAGE = "🌟 欢迎回来！已恢复活跃状态"
CHAT_AUTO_AWAY_MESSAGE = "由于您已经30分钟没有活动，已被自动设置为离开状态。"

# 消息回复时间间隔
# 间隔时间 = 字数 * (平均时间 + 随机时间)
AVERAGE_TYPING_SPEED = 0.2
RANDOM_TYPING_SPEED_MIN = 0.05
RANDOM_TYPING_SPEED_MAX = 0.1
MEMORY_SEGMENT_PATTERN = r'## 记忆片段 \[(.*?)\]\n(?:\*{2}重要度\*{2}: (\d*)\n)?\*{2}摘要\*{2}:(.*?)(?=\n## 记忆片段 |\Z)'


class ChatRoomManager:
    def __init__(self):
        self.active_users = {}
        self.message_buffers = defaultdict(lambda: MessageBuffer([], 0.0, None))
        self.user_stats: Dict[tuple[str, str], UserStats] = defaultdict(UserStats)

    def add_user(self, group_id: str, user_wxid: str) -> None:
        key = (group_id, user_wxid)
        self.active_users[key] = ChatRoomUser(
            wxid=user_wxid,
            group_id=group_id,
            last_active=time.time()
        )
        stats = self.user_stats[key]
        stats.join_count += 1
        stats.last_active = time.time()
        stats.status = UserStatus.ACTIVE

    def remove_user(self, group_id: str, user_wxid: str) -> None:
        key = (group_id, user_wxid)
        if key in self.active_users:
            user = self.active_users[key]
            stats = self.user_stats[key]
            stats.total_active_time += time.time() - stats.last_active
            stats.status = UserStatus.INACTIVE
            del self.active_users[key]
        if key in self.message_buffers:
            buffer = self.message_buffers[key]
            if buffer.timer_task and not buffer.timer_task.done():
                buffer.timer_task.cancel()
            del self.message_buffers[key]

    def update_user_activity(self, group_id: str, user_wxid: str) -> None:
        key = (group_id, user_wxid)
        if key in self.active_users:
            self.active_users[key].last_active = time.time()
            stats = self.user_stats[key]
            stats.total_messages += 1
            stats.last_active = time.time()

    def set_user_status(self, group_id: str, user_wxid: str, status: UserStatus) -> None:
        key = (group_id, user_wxid)
        if key in self.active_users:
            self.active_users[key].status = status
            self.user_stats[key].status = status

    def get_user_status(self, group_id: str, user_wxid: str) -> UserStatus:
        key = (group_id, user_wxid)
        if key in self.active_users:
            return self.active_users[key].status
        return UserStatus.INACTIVE

    def get_user_stats(self, group_id: str, user_wxid: str) -> UserStats:
        return self.user_stats[(group_id, user_wxid)]

    def get_room_stats(self, group_id: str) -> List[tuple[str, UserStats]]:
        stats = []
        for (g_id, wxid), user_stats in self.user_stats.items():
            if g_id == group_id:
                stats.append((wxid, user_stats))
        return sorted(stats, key=lambda x: x[1].total_messages, reverse=True)

    def get_active_users_count(self, group_id: str) -> tuple[int, int, int]:
        active = 0
        away = 0
        total = 0
        for (g_id, _), user in self.active_users.items():
            if g_id == group_id:
                total += 1
                if user.status == UserStatus.ACTIVE:
                    active += 1
                elif user.status == UserStatus.AWAY:
                    away += 1
        return active, away, total

    async def add_message_to_buffer(self, group_id: str, user_wxid: str, message: str, files: list[str] = None) -> None:
        """添加消息到缓冲区"""
        if files is None:
            files = []

        key = (group_id, user_wxid)
        if key not in self.message_buffers:
            self.message_buffers[key] = MessageBuffer()

        buffer = self.message_buffers[key]
        buffer.messages.append(message)
        buffer.last_message_time = time.time()
        buffer.message_count += 1
        buffer.files.extend(files)  # 添加文件ID到缓冲区

        logger.debug(
            f"成功添加消息到缓冲区 - 用户: {user_wxid}, 消息: {message}, 当前消息数: {buffer.message_count}, 文件: {files}")

    def get_and_clear_buffer(self, group_id: str, user_wxid: str) -> Tuple[str, list[str]]:
        """获取并清空缓冲区"""
        key = (group_id, user_wxid)
        buffer = self.message_buffers.get(key)
        if buffer:
            messages = "\n".join(buffer.messages)
            files = buffer.files.copy()  # 复制文件ID列表
            logger.debug(f"合并并清空缓冲区 - 用户: {user_wxid}, 合并消息: {messages}, 文件: {files}")
            buffer.messages.clear()
            buffer.message_count = 0
            buffer.files.clear()  # 清空文件ID列表
            return messages, files
        return "", []

    def is_user_active(self, group_id: str, user_wxid: str) -> bool:
        key = (group_id, user_wxid)
        if key not in self.active_users:
            return False

        user = self.active_users[key]
        if time.time() - user.last_active > CHAT_TIMEOUT:
            self.remove_user(group_id, user_wxid)
            return False
        return True

    def check_and_remove_inactive_users(self):
        current_time = time.time()
        inactive_users = []

        for (group_id, user_wxid), user in list(self.active_users.items()):
            if user.status == UserStatus.ACTIVE and current_time - user.last_active > CHAT_AWAY_TIMEOUT:
                self.set_user_status(group_id, user_wxid, UserStatus.AWAY)
                inactive_users.append((group_id, user_wxid, "away"))
            elif current_time - user.last_active > CHAT_TIMEOUT:
                inactive_users.append((group_id, user_wxid, "timeout"))
                self.remove_user(group_id, user_wxid)
        return inactive_users

    def format_user_stats(self, group_id: str, user_wxid: str, nickname: str = "未知用户") -> str:
        stats = self.get_user_stats(group_id, user_wxid)
        status = self.get_user_status(group_id, user_wxid)
        active_time = int(stats.total_active_time / 60)
        return f"""📊 {nickname} 的聊天室数据：

🏷️ 当前状态：{status.value}
💬 发送消息：{stats.total_messages} 条
📝 总字数：{stats.total_chars} 字
🔄 加入次数：{stats.join_count} 次
⏱️ 活跃时间：{active_time} 分钟"""

    def format_room_status(self, group_id: str) -> str:
        active, away, total = self.get_active_users_count(group_id)
        return f"""🏠 聊天室状态：

👥 当前成员：{total} 人
✨ 活跃成员：{active} 人
💤 暂离成员：{away} 人"""

    async def format_room_ranking(self, group_id: str, bot: WechatAPIClient, limit: int = 5) -> str:
        stats = self.get_room_stats(group_id)
        result = ["🏆 聊天室排行榜：\n"]

        for i, (wxid, user_stats) in enumerate(stats[:limit], 1):
            try:
                nickname = await bot.get_nickname(wxid) or "未知用户"
            except:
                nickname = "未知用户"
            result.append(f"{self._get_rank_emoji(i)} {nickname}")
            result.append(f"   💬 {user_stats.total_messages}条消息")
            result.append(f"   📝 {user_stats.total_chars}字")
        return "\n".join(result)

    @staticmethod
    def _get_rank_emoji(rank: int) -> str:
        if rank == 1:
            return "🥇"
        elif rank == 2:
            return "🥈"
        elif rank == 3:
            return "🥉"
        return f"{rank}."


@dataclass
class ImageProcessor:
    """图片处理器，负责管理图片缓存、转换和优化"""
    cache: Dict[str, Dict[str, any]] = field(default_factory=dict)
    cache_timeout: int = 60
    files_dir: str = "files"
    http_proxy: str = ""
    
    def __init__(self, cache_timeout: int = 60, files_dir: str = "files", http_proxy: str = ""):
        """初始化图片处理器
        
        Args:
            cache_timeout: 缓存超时时间(秒)
            files_dir: 文件存储目录
            http_proxy: HTTP代理地址
        """
        self.cache = {}
        self.cache_timeout = cache_timeout
        self.files_dir = files_dir
        self.http_proxy = http_proxy
        
        # 确保文件存储目录存在
        os.makedirs(self.files_dir, exist_ok=True)
    
    def clear_expired_cache(self):
        """清理过期的图片缓存"""
        current_time = time.time()
        for user_id in list(self.cache.keys()):
            if current_time - self.cache[user_id].get("timestamp", 0) > self.cache_timeout:
                del self.cache[user_id]
    
    def cache_image(self, user_id: str, image_content: bytes) -> bool:
        """缓存图片数据
        
        Args:
            user_id: 用户ID
            image_content: 图片二进制数据
            
        Returns:
            bool: 是否成功缓存
        """
        try:
            # 验证图片数据
            if not self._validate_image(image_content):
                logger.warning(f"无效的图片数据，用户 {user_id}")
                return False
                
            # 清理过期缓存
            self.clear_expired_cache()
            
            # 缓存新图片
            self.cache[user_id] = {
                "content": image_content,
                "timestamp": time.time(),
                "type": self._detect_image_type(image_content)
            }
            logger.debug(f"已缓存用户 {user_id} 的图片，类型: {self.cache[user_id]['type']}")
            return True
        except Exception as e:
            logger.error(f"缓存图片失败: {e}")
            return False
    
    def get_cached_image(self, user_id: str) -> Optional[bytes]:
        """获取用户最近缓存的图片
        
        Args:
            user_id: 用户ID
            
        Returns:
            Optional[bytes]: 图片二进制数据，如果不存在或已过期则返回None
        """
        if user_id not in self.cache:
            return None
            
        cache_data = self.cache[user_id]
        current_time = time.time()
        
        # 检查是否过期
        if current_time - cache_data.get("timestamp", 0) > self.cache_timeout:
            del self.cache[user_id]
            return None
            
        # 获取图片内容并从缓存中移除
        image_content = cache_data.get("content")
        if not isinstance(image_content, bytes):
            logger.error("缓存的图片内容不是二进制格式")
            del self.cache[user_id]
            return None
            
        # 再次验证图片数据
        if not self._validate_image(image_content):
            logger.error("缓存的图片数据无效")
            del self.cache[user_id]
            return None
            
        # 从缓存中删除，确保每张图片只使用一次
        del self.cache[user_id]
        return image_content
    
    def _validate_image(self, image_data: bytes) -> bool:
        """验证图片数据是否有效
        
        Args:
            image_data: 图片二进制数据
            
        Returns:
            bool: 数据是否为有效图片
        """
        if not image_data:
            return False
            
        try:
            Image.open(io.BytesIO(image_data))
            return True
        except Exception as e:
            logger.debug(f"图片验证失败: {e}")
            return False
    
    def _detect_image_type(self, image_data: bytes) -> str:
        """检测图片类型
        
        Args:
            image_data: 图片二进制数据
            
        Returns:
            str: 图片MIME类型
        """
        try:
            kind = filetype.guess(image_data)
            if kind is None:
                return "image/jpeg"  # 默认类型
            return f"image/{kind.extension}"
        except:
            return "image/jpeg"  # 出错时返回默认类型
    
    def optimize_image(self, image_data: bytes, max_size: int = 1024*1024) -> bytes:
        """优化图片大小和质量
        
        Args:
            image_data: 图片二进制数据
            max_size: 最大文件大小(字节)
            
        Returns:
            bytes: 优化后的图片数据
        """
        try:
            # 如果图片已经小于最大大小，直接返回
            if len(image_data) <= max_size:
                return image_data
                
            # 打开图片
            image = Image.open(io.BytesIO(image_data))
            
            # 转换为RGB模式(去除alpha通道)
            if image.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', image.size, (255, 255, 255))
                background.paste(image, mask=image.split()[-1])
                image = background
            
            # 计算压缩率，从90%开始
            quality = 90
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=quality)
            optimized_data = output.getvalue()
            
            # 如果仍然超过大小限制，逐步降低质量
            while len(optimized_data) > max_size and quality > 30:
                quality -= 10
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=quality)
                optimized_data = output.getvalue()
            
            logger.debug(f"图片优化: 原始大小={len(image_data)}字节, 优化后={len(optimized_data)}字节, 质量={quality}")
            return optimized_data
        except Exception as e:
            logger.error(f"图片优化失败: {e}")
            return image_data  # 失败时返回原始数据
    
    async def extract_image_from_message(self, message: dict) -> Optional[bytes]:
        """从消息中提取图片数据
        
        Args:
            message: 消息字典
            
        Returns:
            Optional[bytes]: 图片二进制数据，如果提取失败则返回None
        """
        try:
            # 解析XML获取图片信息
            xml_content = message.get("Content")
            if not isinstance(xml_content, str):
                logger.error("图片消息内容不是字符串格式")
                return None
                
            # 尝试提取base64编码的图片
            try:
                # 从XML中提取base64图片数据
                image_base64 = xml_content.split(',')[-1].strip()  # 获取base64部分
                
                # 转换base64为二进制
                image_content = base64.b64decode(image_base64)
                
                # 验证并优化图片
                if self._validate_image(image_content):
                    return self.optimize_image(image_content)
                else:
                    logger.error("无法验证从base64提取的图片数据")
            except Exception as e:
                logger.error(f"处理base64图片数据失败: {e}")
                
            return None
        except Exception as e:
            logger.error(f"从消息中提取图片失败: {e}")
            logger.debug(traceback.format_exc())
            return None
     
    async def upload_to_dify(self, image_data: bytes, user_id: str, model_config: ModelConfig) -> Optional[str]:
        """上传图片到Dify API
        
        Args:
            image_data: 图片二进制数据
            user_id: 用户ID
            model_config: 模型配置
            
        Returns:
            Optional[str]: 上传成功返回文件ID，失败返回None
        """
        if not image_data:
            logger.error("没有图片数据可上传")
            return None
             
        try:
            # 优化图片
            optimized_image = self.optimize_image(image_data)
             
            # 检测图片类型
            mime_type = self._detect_image_type(optimized_image)
             
            # 准备上传
            headers = {"Authorization": f"Bearer {model_config.api_key}"}
            formdata = aiohttp.FormData()
            formdata.add_field(
                "file", 
                optimized_image, 
                filename=f"image.{mime_type.split('/')[-1]}", 
                content_type=mime_type
            )
            formdata.add_field("user", user_id)

            # 发送请求
            url = f"{model_config.base_url}/files/upload"
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(url, headers=headers, data=formdata) as resp:
                    if resp.status in (200, 201):
                        result = await resp.json()
                        logger.debug(f"图片上传成功: {result}")
                        return result.get("id")
                    else:
                        error_text = await resp.text()
                        logger.error(f"图片上传失败: HTTP {resp.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"上传图片时发生错误: {e}")
            logger.debug(traceback.format_exc())
            return None


def parse_time(time_str):
    try:
        TimeResult = datetime.strptime(time_str, "%H:%M").time()
        return TimeResult
    except Exception as e:
        logger.error("\033[31m错误：主动消息安静时间设置有误！请填00:00-23:59 不要填24:00,并请注意中间的符号为英文冒号！\033[0m")


def remove_timestamps(text):
    """
    移除文本中所有[YYYY-MM-DD (Weekday) HH:MM(:SS)]格式的时间戳
    支持四种格式：
    1. [YYYY-MM-DD Weekday HH:MM:SS] - 带星期和秒
    2. [YYYY-MM-DD Weekday HH:MM] - 带星期但没有秒
    3. [YYYY-MM-DD HH:MM:SS] - 带秒但没有星期
    4. [YYYY-MM-DD HH:MM] - 基本格式
    并自动清理因去除时间戳产生的多余空格
    """
    # 定义支持多种格式的时间戳正则模式
    timestamp_pattern = r'''
        \[                # 起始方括号
        \d{4}             # 年份：4位数字
        -(0[1-9]|1[0-2])  # 月份：01-12
        -(0[1-9]|[12]\d|3[01]) # 日期：01-31
        (?:\s[A-Za-z]+)?  # 可选的星期部分
        \s                # 日期与时间之间的空格
        (?:2[0-3]|[01]\d) # 小时：00-23
        :[0-5]\d          # 分钟：00-59
        (?::[0-5]\d)?     # 可选的秒数：00-59括号
    '''

    # 使用正则标志：
    # 1. re.VERBOSE 允许模式中的注释和空格
    # 2. re.MULTILINE 跨行匹配
    # 3. 替换时自动处理前后空格
    return re.sub(
        pattern=timestamp_pattern,
        repl=lambda m: ' ',  # 统一替换为单个空格
        string=text,
        flags=re.X | re.M
    ).strip()  # 最后统一清理首尾空格


class Dify2(PluginBase):
    description = "Dify插件"
    author = "老夏的金库"
    version = "1.3.2"  # 更新版本号

    def __init__(self):
        super().__init__()
        self.enable = True
        self.chat_manager = ChatRoomManager()
        self.user_models = {}  # 保存用户当前使用的模型
        self.chat_history = ChatHistoryDatabase()
        # 添加变量用于标记API代理是否可用
        self.has_api_proxy = False  # 设置为False，禁用API代理功能
        
        # 初始化所有配置和依赖
        self._init_admin_list()
        self._init_plugin_config()
        self._init_database()
        self._init_file_storage()
        self._init_model_mapping()
        self._init_api_proxy()
        self.silicon_key = "Bearer sk-nuquonnskijxjttcuubbnqfoohieknygtthpsstwrzzuqdqn"  # TODO 处理这里！！！
        self.xunfei_appid = "013c3cdb"
        self.xunfei_secret_key = "2a0a119701adde67d094969c40c0b991"

    def _init_admin_list(self):
        """初始化管理员列表"""
        # 默认使用空列表
        self.admins = []
        
        try:
            # 尝试从主配置文件读取管理员列表
            main_config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "main_config.toml")
            if os.path.exists(main_config_path):
                with open(main_config_path, "rb") as f:
                    main_config = tomllib.load(f)
                    self.admins = main_config.get("XYBot", {}).get("admins", [])
                    logger.info(f"从主配置文件成功加载管理员列表: {self.admins}")
            else:
                # 如果主配置文件不存在，则尝试从插件配置中读取
                with open(os.path.join(os.path.dirname(__file__), "config.toml"), "rb") as f:
                    config = tomllib.load(f)
                    # 使用 get 方法避免 KeyError
                    self.admins = config.get("XYBot", {}).get("admins", [])
                    logger.info(f"从插件配置文件加载管理员列表: {self.admins}")
        except (FileNotFoundError, tomllib.TOMLDecodeError, KeyError) as e:
            logger.warning(f"加载配置文件失败，使用默认空管理员列表: {e}")
            self.admins = []  # 使用空列表作为默认值

    def _init_plugin_config(self):
        """初始化插件配置"""
        try:
            config_path = os.path.join(os.path.dirname(__file__), "config.toml")
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
            
            # 获取Dify配置
            plugin_config = config["Dify"]
            
            # 基本配置
            self.default_model = plugin_config.get("default-model", "")
            self.command_tip = plugin_config.get("command-tip", "")
            self.commands = plugin_config.get("commands", [])
            self.admin_ignore = plugin_config.get("admin_ignore", True)
            self.whitelist_ignore = plugin_config.get("whitelist_ignore", True)
            self.http_proxy = plugin_config.get("http-proxy", "")
            self.robot_names = plugin_config.get("robot-names", [])
            self.voice_reply_length = plugin_config.get("voice-reply-length", 100)
            self.audio_to_text_url = plugin_config.get("audio-to-text-url", "")
            self.text_to_audio_url = plugin_config.get("text-to-audio-url", "")
            self.remember_user_model = plugin_config.get("remember_user_model", True)
            self.chatroom_enable = plugin_config.get("chatroom_enable", True)
            self.enable_auto_message = plugin_config.get("enable-auto-message", True)
            self.auto_message = plugin_config.get("auto-message", '请你模拟系统设置的角色，在微信上找对方继续刚刚的话题或者询问对方在做什么')
            self.min_countdown_hours = plugin_config.get("min-countdown-hours", 1.0)
            self.max_countdown_hours = plugin_config.get("max-countdown-hours", 2.0)
            self.quiet_time_start = parse_time(plugin_config.get("quire-time-start", "22:30"))
            self.quiet_time_end = parse_time(plugin_config.get("quire-time-end", "8:00"))
            self.enable_memory = plugin_config.get("enable-memory", True)
            self.max_message_entries = plugin_config.get("max-message-entries", 30)
            self.sumnmary_workflow_api_key = plugin_config.get("sumnmary-workflow-api-key")
            self.sumnmary_workflow_base_url = plugin_config.get("sumnmary-workflow-base-url")
            self.user_timers = dict()
            self.user_names = ["wxid_acs3cg99vu1921"]
            self.user_wait_times = {}
            self.user_is_send_message = {}

            # 加载所有模型配置
            self._load_model_configs(plugin_config)
            
            # 如果没有设置默认模型或默认模型不存在，选择第一个模型作为默认
            if not self.default_model or self.default_model not in self.models:
                if self.models:
                    self.default_model = next(iter(self.models.keys()))
                    logger.warning(f"默认模型未设置或不存在，使用 {self.default_model} 作为默认模型")
                else:
                    raise ValueError("没有找到任何可用的模型配置")
            
            # 设置当前使用的模型
            self.current_model = self.models[self.default_model]
                
        except (FileNotFoundError, tomllib.TOMLDecodeError) as e:
            logger.error(f"加载Dify插件配置文件失败: {e}")
            raise
        except KeyError as e:
            logger.error(f"配置文件缺少必要的键: {e}")
            raise
        except Exception as e:
            logger.error(f"初始化配置时发生未知错误: {e}")
            logger.error(traceback.format_exc())
            raise

    def _init_mock_person(self):
        pass

    def _load_model_configs(self, plugin_config):
        """加载所有模型配置"""
        self.models = {}
        for model_name, model_config in plugin_config.get("models", {}).items():
            try:
                self.models[model_name] = ModelConfig(
                    api_key=model_config.get("api-key", ""),
                    base_url=model_config.get("base-url", ""),
                    trigger_words=model_config.get("trigger-words", []),
                    price=model_config.get("price", 0),
                    wakeup_words=model_config.get("wakeup-words", [])
                )
                logger.info(f"已加载模型配置: {model_name}")
            except Exception as e:
                logger.error(f"加载模型 {model_name} 配置失败: {e}")

    def _init_database(self):
        """初始化数据库连接"""
        try:
            self.db = XYBotDB()
            logger.info("数据库初始化成功")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            logger.error(traceback.format_exc())
            raise

    def _init_file_storage(self):
        """初始化文件存储"""
        self.files_dir = "files"
        os.makedirs(self.files_dir, exist_ok=True)
        logger.info(f"文件存储目录初始化成功: {self.files_dir}")
        
        # 创建图片处理器
        self.image_processor = ImageProcessor(
            cache_timeout=60,
            files_dir=self.files_dir,
            http_proxy=self.http_proxy
        )
        logger.info("图片处理器初始化成功")
        
    def _init_model_mapping(self):
        """初始化唤醒词到模型的映射"""
        self.wakeup_word_to_model = {}
        logger.info("开始加载唤醒词配置:")
        
        for model_name, model_config in self.models.items():
            logger.info(f"处理模型 '{model_name}' 的唤醒词列表: {model_config.wakeup_words}")
            
            for wakeup_word in model_config.wakeup_words:
                if not wakeup_word:
                    continue
                    
                if wakeup_word in self.wakeup_word_to_model:
                    old_model = next((name for name, config in self.models.items() 
                                     if config == self.wakeup_word_to_model[wakeup_word]), '未知')
                    logger.warning(f"唤醒词冲突! '{wakeup_word}' 已绑定到模型 '{old_model}'，"
                                  f"现在被覆盖绑定到 '{model_name}'")
                
                self.wakeup_word_to_model[wakeup_word] = model_config
                logger.info(f"唤醒词 '{wakeup_word}' 成功绑定到模型 '{model_name}'")
        
        logger.info(f"唤醒词映射完成，共加载 {len(self.wakeup_word_to_model)} 个唤醒词")

    def _init_api_proxy(self):
        """初始化API代理"""
        # 初始化为None
        self.api_proxy = None
        
        # 如果API代理功能被禁用，则直接返回
        if not self.has_api_proxy:
            logger.info("API代理功能已禁用")
            return
            
        try:
            import sys
            # 导入api_proxy实例
            sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
            from admin.server import get_api_proxy
            self.api_proxy = get_api_proxy()
            
            if self.api_proxy:
                logger.info("成功获取API代理实例")
            else:
                logger.warning("API代理实例获取失败，将使用直接连接")
        except ImportError as e:
            logger.warning(f"无法导入API代理模块: {e}")
        except Exception as e:
            logger.error(f"获取API代理实例失败: {e}")
            logger.error(traceback.format_exc())

    def is_quiet_time(self):
        current_time = datetime.now().time()
        if self.quiet_time_start <= self.quiet_time_end:
            return self.quiet_time_start <= current_time <= self.quiet_time_end
        else:
            return current_time >= self.quiet_time_start or current_time <= self.quiet_time_end

    @schedule('interval', seconds=60)
    async def memory_manager(self, bot: WechatAPIClient):
        if self.enable_memory:
            try:
                # 检查所有监听用户
                for user in self.user_names:
                    memory_message = self.chat_history.get_memory_messages_from_db(user, limit=1)
                    if len(memory_message) == 0:
                        memory_time = -1
                    else:
                        memory_time = memory_message[0]["create_time"]
                    messages = self.chat_history.get_text_messages_from_db(user, start_time=memory_time)
                    # TODO 记忆淘汰机制
                    # if len(messages) > self.max_message_entries:  # 进行总结，入库
                    if len(messages) > 0:  # 进行总结，入库
                        message_str = '\n'.join(['{}, {}, {}'.format(message['create_time'], message['sender_wxid'], message['content']) for message in messages])
                        summary, importrance = await self.summarize_and_save(user, bot.wxid, message_str)
                        content = {
                            "Importance": importrance,
                            "Summary": summary,
                            "Content": message_str
                        }
                        self.chat_history.save_message_to_db(message_type="memory", chat_id=user, sender_wxid='', create_time=int(time.time()), content=content)
            except Exception as e:
                logger.error(f"记忆管理异常: {str(e)}")

    async def summarize_and_save(self, user_name, self_name, message_str):
        summary_prompt = f"请以{self_name}的视角，用中文总结与{user_name}的对话，提取重要信息总结为一段话作为记忆片段（直接回复一段话）：\n{message_str}"
        summary_text, importance = await self.dify_workflow(user_name, self_name, summary_prompt)
        return summary_text, importance

    @schedule('interval', seconds=30)
    async def check_user_timeouts(self, bot: WechatAPIClient):
        if self.enable_auto_message:
            current_time = time.time()
            for user in self.user_names:
                last_active = self.user_timers.get(user)
                wait_time = self.user_wait_times.get(user)
                if last_active and wait_time:
                    if current_time - last_active >= wait_time:
                        if not self.is_quiet_time():
                            # 增加时间标记
                            current_time = datetime.now().strftime("%Y-%m-%d %A %H:%M:%S")
                            auto_content = f"[{current_time}] {self.auto_message}"
                            logger.info(f"为用户 {user} 发送自动消息:{auto_content}")
                            reply = await self.dify_text(bot, user, bot.wxid, auto_content)
                            # self.send_reply(user, user, user, self.auto_message, reply)
                            await self.mock_person_send_reply(bot, user, self.auto_message, reply)
                        # 重置计时器和等待时间
                        self.reset_user_timer(user)

    async def mock_person_send_reply(self, bot, wxid, merged_message, reply):
        try:
            # 发送分段消息过程中停止向deepseek发送新请求
            self.user_is_send_message[wxid] = True
            reply = remove_timestamps(reply)
            if '\\' in reply:
                parts = [p.strip() for p in reply.split('\\') if p.strip()]
                for i, part in enumerate(parts):
                    await bot.send_text_message(wxid, part)
                    logger.info(f"分段回复 {wxid}: {part}")
                    if i < len(parts) - 1:
                        next_part = parts[i + 1]
                        # 计算延时时间，模拟打字速度
                        delay = len(next_part) * (AVERAGE_TYPING_SPEED + random.uniform(RANDOM_TYPING_SPEED_MIN,
                                                                                        RANDOM_TYPING_SPEED_MAX))
                        if delay < 2:
                            delay = 2
                        time.sleep(delay)
            else:
                await bot.send_text_message(wxid, reply)
                logger.info(f"回复 {wxid}: {reply}")
            # 解除发送限制
            self.user_is_send_message[wxid] = False
        except Exception as e:
            logger.error(f"发送回复失败: {str(e)}")
            # 解除发送限制
            self.user_is_send_message[wxid] = False
    # 发送表情
    # await bot.send_emoji_message("wxid_acs3cg99vu1921", md5="53aceba6128d29d78f66f564029d2228", total_length=1222625)

    def reset_user_timer(self, user):
        self.user_timers[user] = time.time()
        self.user_wait_times[user] = self.get_random_wait_time()

    def get_random_wait_time(self):
        return random.uniform(self.min_countdown_hours, self.max_countdown_hours) * 3600  # 转换为秒

    def get_user_model(self, user_id: str) -> ModelConfig:
        """获取用户当前使用的模型"""
        if self.remember_user_model and user_id in self.user_models:
            return self.user_models[user_id]
        return self.current_model

    def set_user_model(self, user_id: str, model: ModelConfig):
        """设置用户当前使用的模型"""
        if self.remember_user_model:
            self.user_models[user_id] = model

    def get_model_from_message(self, content: str, user_id: str) -> tuple[ModelConfig, str, bool]:
        """根据消息内容判断使用哪个模型，并返回是否是切换模型的命令"""
        original_content = content  # 保留原始内容
        content = content.lower()  # 只在检测时使用小写版本
        
        # 检查是否是切换模型的命令
        if content.endswith("切换"):
            for model_name, model_config in self.models.items():
                for trigger in model_config.trigger_words:
                    if content.startswith(trigger.lower()):
                        self.set_user_model(user_id, model_config)
                        logger.info(f"用户 {user_id} 切换模型到 {model_name}")
                        return model_config, "", True
            return self.get_user_model(user_id), original_content, False

        # 检查是否使用了唤醒词
        logger.debug(f"检查消息 '{content}' 是否包含唤醒词")
        for wakeup_word, model_config in self.wakeup_word_to_model.items():
            wakeup_lower = wakeup_word.lower()
            content_lower = content.lower()
            if content_lower.startswith(wakeup_lower) or f" {wakeup_lower}" in content_lower:
                model_name = next((name for name, config in self.models.items() if config == model_config), '未知')
                logger.info(f"消息中检测到唤醒词 '{wakeup_word}'，临时使用模型 '{model_name}'")
                
                # 更精确地替换唤醒词
                # 先找到原文中唤醒词的实际位置和形式
                original_wakeup = None
                if content_lower.startswith(wakeup_lower):
                    # 如果以唤醒词开头，直接取对应长度的原始文本
                    original_wakeup = original_content[:len(wakeup_lower)]
                else:
                    # 如果唤醒词在中间，找到它的位置并获取原始形式
                    wakeup_pos = content_lower.find(f" {wakeup_lower}") + 1  # +1 是因为包含了前面的空格
                    if wakeup_pos > 0:
                        original_wakeup = original_content[wakeup_pos:wakeup_pos+len(wakeup_lower)]
                
                if original_wakeup:
                    # 使用原始形式进行替换，保留大小写
                    query = original_content.replace(original_wakeup, "", 1).strip()
                    logger.debug(f"唤醒词处理后的查询: '{query}'")
                    return model_config, query, False
        
        # 检查是否是临时使用其他模型
        for model_name, model_config in self.models.items():
            for trigger in model_config.trigger_words:
                if trigger.lower() in content:
                    logger.info(f"消息中包含触发词 '{trigger}'，临时使用模型 '{model_name}'")
                    query = original_content.replace(trigger, "", 1).strip()  # 使用原始内容替换原始触发词
                    return model_config, query, False

        # 使用用户当前的模型
        current_model = self.get_user_model(user_id)
        model_name = next((name for name, config in self.models.items() if config == current_model), '默认')
        logger.debug(f"未检测到特定模型指示，使用用户 {user_id} 当前默认模型 '{model_name}'")
        return current_model, original_content, False

    async def check_and_notify_inactive_users(self, bot: WechatAPIClient):
        # 如果聊天室功能关闭，则直接返回，不进行检查和提醒
        if not self.chatroom_enable:
            return
        
        inactive_users = self.chat_manager.check_and_remove_inactive_users()
        for group_id, user_wxid, status in inactive_users:
            if status == "away":
                await bot.send_at_message(group_id, "\n" + CHAT_AUTO_AWAY_MESSAGE, [user_wxid])
            elif status == "timeout":
                await bot.send_at_message(group_id, "\n" + CHAT_TIMEOUT_MESSAGE, [user_wxid])

    async def process_buffered_messages(self, bot: WechatAPIClient, group_id: str, user_wxid: str):
        logger.debug(f"开始处理缓冲消息 - 用户: {user_wxid}, 群组: {group_id}")
        messages, files = self.chat_manager.get_and_clear_buffer(group_id, user_wxid)
        logger.debug(f"从缓冲区获取到的消息: {messages}")
        logger.debug(f"从缓冲区获取到的文件: {files}")
        
        if messages is not None and messages.strip():
            logger.debug(f"合并后的消息: {messages}")
            message = {
                "FromWxid": group_id,
                "SenderWxid": user_wxid,
                "Content": messages,
                "IsGroup": True,
                "MsgType": 1
            }
            logger.debug(f"准备检查积分")
            if await self._check_point(bot, message):
                logger.debug("积分检查通过，开始调用 Dify API")
                try:
                    # 检查是否有唤醒词或触发词
                    model, processed_query, is_switch = self.get_model_from_message(messages, user_wxid)
                    reply = await self.dify_text(bot, message["SenderWxid"], message["FromWxid"], processed_query, files=files, specific_model=model)
                    await self.mock_person_send_reply(bot, message["FromWxid"], self.auto_message, reply)

                    logger.debug("成功调用 Dify API 并发送消息")
                except Exception as e:
                    logger.error(f"调用 Dify API 失败: {e}")
                    logger.error(traceback.format_exc())
                    await bot.send_at_message(group_id, "\n消息处理失败，请稍后重试。", [user_wxid])
        else:
            logger.debug("缓冲区为空或消息无效，无需处理")

    async def _delayed_message_processing(self, bot: WechatAPIClient, group_id: str, user_wxid: str):
        key = (group_id, user_wxid)
        try:
            logger.debug(f"开始延迟处理 - 用户: {user_wxid}, 群组: {group_id}")
            await asyncio.sleep(MESSAGE_BUFFER_TIMEOUT)
            
            buffer = self.chat_manager.message_buffers.get(key)
            if buffer and buffer.messages:
                logger.debug(f"缓冲区消息数: {len(buffer.messages)}")
                logger.debug(f"最后消息时间: {time.time() - buffer.last_message_time:.2f}秒前")
                
                if time.time() - buffer.last_message_time >= MESSAGE_BUFFER_TIMEOUT:
                    logger.debug("开始处理缓冲消息")
                    await self.process_buffered_messages(bot, group_id, user_wxid)
                else:
                    logger.debug("跳过处理 - 有新消息，重新调度")
                    await self.schedule_message_processing(bot, group_id, user_wxid)
        except asyncio.CancelledError:
            logger.debug(f"定时器被取消 - 用户: {user_wxid}, 群组: {group_id}")
        except Exception as e:
            logger.error(f"处理消息缓冲区时出错: {e}")
            await bot.send_at_message(group_id, "\n消息处理发生错误，请稍后重试。", [user_wxid])

    async def schedule_message_processing(self, bot: WechatAPIClient, group_id: str, user_wxid: str):
        key = (group_id, user_wxid)
        if key not in self.chat_manager.message_buffers:
            self.chat_manager.message_buffers[key] = MessageBuffer()
        
        buffer = self.chat_manager.message_buffers[key]
        logger.debug(f"安排消息处理 - 用户: {user_wxid}, 群组: {group_id}")
        
        # 获取buffer中的消息内容
        buffer_content = "\n".join(buffer.messages) if buffer.messages else ""
        
        # 检查是否有最近的图片
        image_content = await self.get_cached_image(group_id)
        if image_content:
            try:
                logger.debug("发现最近的图片，准备上传到 Dify")
                # 先检查是否有唤醒词获取对应模型
                wakeup_model = None
                for wakeup_word, model_config in self.wakeup_word_to_model.items():
                    wakeup_lower = wakeup_word.lower()
                    buffer_content_lower = buffer_content.lower()
                    if buffer_content_lower.startswith(wakeup_lower) or f" {wakeup_lower}" in buffer_content_lower:
                        wakeup_model = model_config
                        break
                
                # 如果没有找到唤醒词对应的模型，则使用用户当前的模型
                model_config = wakeup_model or self.get_user_model(user_wxid)
                
                file_id = await self.upload_file_to_dify(
                    image_content,
                    "image/jpeg",
                    group_id,
                    model_config=model_config  # 传递正确的模型配置
                )
                if file_id:
                    logger.debug(f"图片上传成功，文件ID: {file_id}")
                    buffer.files.append(file_id)  # 直接添加到buffer的files列表
                    logger.debug(f"当前buffer中的文件: {buffer.files}")
                else:
                    logger.error("图片上传失败")
            except Exception as e:
                logger.error(f"处理图片失败: {e}")
        
        if buffer.message_count >= MAX_BUFFERED_MESSAGES:
            logger.debug("缓冲区已满，立即处理消息")
            await self.process_buffered_messages(bot, group_id, user_wxid)
            return
            
        if buffer.timer_task and not buffer.timer_task.done():
            logger.debug("取消已有定时器")
            buffer.timer_task.cancel()
        
        logger.debug("创建新定时器")
        buffer.timer_task = asyncio.create_task(
            self._delayed_message_processing(bot, group_id, user_wxid)
        )
        logger.debug(f"定时器任务已创建 - 用户: {user_wxid}")

    @on_text_message(priority=20)
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        content = message["Content"].strip()
        
        # 检查是否需要处理超时用户
        await self.check_and_notify_inactive_users(bot)

        # 处理私聊消息
        if not message["IsGroup"]:
            # 准备图片附件
            files = await self._prepare_image_attachments(message)
            
            # 检查是否是命令
            command = content.split(" ")[0] if content else ""
            if command in self.commands:
                content = content[len(command):].strip()
                
            # 处理消息
            await self._process_message_with_model(bot, message, content, files=files)
            return

        # 以下是群聊处理逻辑
        group_id = message["FromWxid"]
        user_wxid = message["SenderWxid"]
        
        # 处理聊天室命令
        if await self._handle_chatroom_command(bot, message, content):
            return
        
        # 处理模型切换命令
        if content.endswith("切换"):
            for model_name, model_config in self.models.items():
                for trigger in model_config.trigger_words:
                    if content.lower().startswith(trigger.lower()):
                        self.set_user_model(user_wxid, model_config)
                        await bot.send_at_message(
                            group_id,
                            f"\n已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。",
                            [user_wxid]
                        )
                        return

        # 检测是否需要处理消息
        is_at = self.is_at_message(message)
        is_command = content.split(" ")[0] if content else "" in self.commands
        
        # 检查是否有唤醒词
        wakeup_detected = False
        wakeup_model = None
        processed_wakeup_query = ""
        
        for wakeup_word, model_config in self.wakeup_word_to_model.items():
            # 改用更精确的匹配方式，避免错误识别
            wakeup_lower = wakeup_word.lower()
            content_lower = content.lower()
            
            if content_lower.startswith(wakeup_lower) or f" {wakeup_lower}" in content_lower:
                wakeup_detected = True
                wakeup_model = model_config
                model_name = next((name for name, config in self.models.items() if config == model_config), '未知')
                logger.info(f"检测到唤醒词 '{wakeup_word}'，触发模型 '{model_name}'，原始内容: '{content}'")
                
                # 更精确地替换唤醒词
                original_wakeup = None
                if content_lower.startswith(wakeup_lower):
                    original_wakeup = content[:len(wakeup_lower)]
                else:
                    wakeup_pos = content_lower.find(f" {wakeup_lower}") + 1
                    if wakeup_pos > 0:
                        original_wakeup = content[wakeup_pos:wakeup_pos+len(wakeup_lower)]
                
                if original_wakeup:
                    processed_wakeup_query = content.replace(original_wakeup, "", 1).strip()
                    logger.info(f"处理后的查询内容: '{processed_wakeup_query}'")
                break
        
        # 准备图片附件
        files = await self._prepare_image_attachments(message, wakeup_model if wakeup_detected else None)
        
        # 处理唤醒词请求
        if wakeup_detected and wakeup_model and processed_wakeup_query:
            if await self._check_point(bot, message, wakeup_model):
                logger.info(f"使用唤醒词对应模型处理请求")
                reply = await self.dify_text(bot, message, processed_wakeup_query, files=files, specific_model=wakeup_model)
                await self.mock_person_send_reply(bot, message["FromWxid"], self.auto_message, reply)
            return

        # 处理@或命令触发的情况
        if is_at or is_command:
            # 如果用户不在聊天室中且聊天室功能开启，则添加用户
            if not self.chat_manager.is_user_active(group_id, user_wxid):
                if self.chatroom_enable:
                    self.chat_manager.add_user(group_id, user_wxid)
                    await bot.send_at_message(group_id, "\n" + CHAT_JOIN_MESSAGE, [user_wxid])
                
                # 处理@或命令消息
                query = content
                for robot_name in self.robot_names:
                    query = query.replace(f"@{robot_name}", "").strip()
                
                if is_command:
                    command = query.split(" ")[0]
                    query = query[len(command):].strip()
                
                await self._process_message_with_model(bot, message, query, files=files)
            return

        # 如果聊天室功能被禁用，则所有消息都需要@或命令触发
        if not self.chatroom_enable:
            if is_at or is_command:
                query = content
                for robot_name in self.robot_names:
                    query = query.replace(f"@{robot_name}", "").strip()
                
                if is_command:
                    command = query.split(" ")[0]
                    query = query[len(command):].strip()
                
                await self._process_message_with_model(bot, message, query, files=files)
            return
        
        # 处理聊天室中的普通消息
        if self.chat_manager.is_user_active(group_id, user_wxid):
            # 更新用户活动状态
            self.chat_manager.update_user_activity(group_id, user_wxid)
            
            # 如果用户处于离开状态，则设为活跃
            if self.chat_manager.get_user_status(group_id, user_wxid) == UserStatus.AWAY:
                self.chat_manager.set_user_status(group_id, user_wxid, UserStatus.ACTIVE)
                await bot.send_at_message(group_id, "\n" + CHAT_BACK_MESSAGE, [user_wxid])

            # 处理@或命令触发的消息
            if is_at or is_command:
                query = content
                for robot_name in self.robot_names:
                    query = query.replace(f"@{robot_name}", "").strip()
                
                if is_command:
                    command = query.split(" ")[0]
                    query = query[len(command):].strip()
                
                await self._process_message_with_model(bot, message, query, files=files)
            # 处理普通消息
            elif content:
                # 将消息加入缓冲区
                await self.chat_manager.add_message_to_buffer(group_id, user_wxid, content, files)
                await self.schedule_message_processing(bot, group_id, user_wxid)
        
        return

    @on_at_message(priority=20)
    async def handle_at(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return False

        if not self.current_model.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        await self.check_and_notify_inactive_users(bot)

        content = message["Content"].strip()
        query = content
        for robot_name in self.robot_names:
            query = query.replace(f"@{robot_name}", "").strip()

        group_id = message["FromWxid"]
        user_wxid = message["SenderWxid"]

        # 处理退出聊天命令
        if query == "退出聊天":
            if self.chat_manager.is_user_active(group_id, user_wxid):
                self.chat_manager.remove_user(group_id, user_wxid)
                await bot.send_at_message(group_id, "\n" + CHAT_LEAVE_MESSAGE, [user_wxid])
            return False

        # 如果用户不在聊天室中，则添加
        if not self.chat_manager.is_user_active(group_id, user_wxid):
            # 根据配置决定是否加入聊天室并发送欢迎消息
            self.chat_manager.add_user(group_id, user_wxid)
            if self.chatroom_enable:
                await bot.send_at_message(group_id, "\n" + CHAT_JOIN_MESSAGE, [user_wxid])

        # 如果查询为空，提示用户输入问题
        if not query:
            await bot.send_at_message(message["FromWxid"], "\n请输入你的问题或指令。", [message["SenderWxid"]])
            return False

        # 准备图片附件
        files = await self._prepare_image_attachments(message)
        
        # 处理消息并使用对应模型
        await self._process_message_with_model(bot, message, query, files=files)
        return False

    @on_voice_message(priority=20)
    async def handle_voice(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.current_model.api_key:
            await bot.send_text_message(message["FromWxid"], "你还没配置Dify API密钥！")
            return False

        # 语音转文字
        query = await self.audio_to_text(bot, message)
        if not query:
            await bot.send_text_message(message["FromWxid"], VOICE_TRANSCRIPTION_FAILED)
            return False

        logger.debug(f"语音转文字结果: {query}")

        # 处理语音消息
        await self._process_message_with_model(bot, message, query)
        return False

    @on_image_message(priority=20)
    async def handle_image(self, bot: WechatAPIClient, message: dict):
        """处理图片消息"""
        if not self.enable:
            return

        try:
            # 使用图片处理器提取图片
            image_content = await self.image_processor.extract_image_from_message(message)
            if image_content:
                # 缓存图片
                self.image_processor.cache_image(message["FromWxid"], image_content)
                logger.debug(f"成功缓存用户 {message['FromWxid']} 的图片")
        except Exception as e:
            logger.error(f"处理图片消息失败: {e}")
            logger.debug(traceback.format_exc())
            
    @on_emoji_message(priority=20)
    async def handle_emoji(self, bot: WechatAPIClient, message: dict):
        """处理表情消息"""
        if not self.enable:
            return
        emoji_msg_list = self.chat_history.get_emoji_messages_from_db(message["FromWxid"], limit=1000)
        msg = random.choice(emoji_msg_list)
        await bot.send_emoji_message(message["FromWxid"], msg["md5"], msg["len"])
        return True

    def is_at_message(self, message: dict) -> bool:
        if not message["IsGroup"]:
            return False
        content = message["Content"]
        for robot_name in self.robot_names:
            if f"@{robot_name}" in content:
                return True
        return False

    async def dify_workflow(self, user_name, self_name, summary_prompt):
        try:
            # 使用直接连接
            headers = {"Authorization": f"Bearer {self.sumnmary_workflow_api_key}", "Content-Type": "application/json"}
            ai_resp = ""
            payload = {
                "inputs": {
                    "summary_prompt": summary_prompt
                },
                "response_mode": "streaming",
                "user": "1234"
            }
            conversation_id = self.db.get_llm_thread_id(user_name, namespace="dify")
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(url=f"{self.sumnmary_workflow_base_url}/workflows/run", headers=headers,
                                        data=json.dumps(payload)) as resp:
                    if resp.status in (200, 201):
                        async for line in resp.content:
                            line = line.decode("utf-8").strip()
                            if not line or line == "event: ping":
                                continue
                            elif line.startswith("data: "):
                                line = line[6:]
                            try:
                                resp_json = json.loads(line)
                            except json.JSONDecodeError:
                                logger.error(f"Dify返回的JSON解析错误: {line}")
                                continue

                            event = resp_json.get("event", "")
                            if event == "message":
                                ai_resp += resp_json.get("answer", "")
                            elif event == "message_replace":
                                ai_resp = resp_json.get("answer", "")
                        new_con_id = resp_json.get("conversation_id", "")
                        if new_con_id and new_con_id != conversation_id:
                            self.db.save_llm_thread_id(user_name, new_con_id, "dify")
                        ai_resp = ai_resp.rstrip()
                        logger.debug(f"Dify响应: {ai_resp}")
                    elif resp.status == 404:
                        logger.warning("会话ID不存在，重置会话ID并重试")
                        self.db.save_llm_thread_id(user_name, "", "dify")
                        # 重要：在递归调用时必须传递原始模型，不要重新选择
                        return "会话错误：会话ID不存在"
                    elif resp.status == 400:
                        return "会话错误：会话返回了400"
                    elif resp.status == 500:
                        return "会话错误：会话返回了500"
                    else:
                        return "会话错误：会话返回了其它响应状态码"
            if ai_resp:
                return ai_resp
            else:
                logger.warning("Dify未返回有效响应")
                return "Dify未返回有效响应"
        except Exception as e:
            logger.error(f"Dify API 调用失败: {e}")
            return f"Dify API 调用失败: {e}"


    async def dify_text(self, bot: WechatAPIClient, sender_wxid:str, from_wxid: str, query: str, files=None, specific_model=None):
        """发送纯文字消息到Dify API，并且返回纯文字"""
        if files is None:
            files = []

        # 如果提供了specific_model，直接使用；否则根据消息内容选择模型
        if specific_model:
            model = specific_model
            processed_query = query
            is_switch = False
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.info(f"使用指定的模型 '{model_name}'")
        else:
            # 根据消息内容选择模型
            model, processed_query, is_switch = self.get_model_from_message(query, sender_wxid)
            model_name = next((name for name, config in self.models.items() if config == model), '默认')
            logger.info(f"从消息内容选择模型 '{model_name}'")

            # 如果是切换模型的命令
            if is_switch:
                model_name = next(name for name, config in self.models.items() if config == model)
                await bot.send_text_message(
                    from_wxid,
                    f"已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。"
                )
                return "已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。"

        # 记录将要使用的模型配置
        logger.info(f"模型API密钥: {model.api_key[:5]}...{model.api_key[-5:] if len(model.api_key) > 10 else ''}")
        logger.info(f"模型API端点: {model.base_url}")
        try:
            logger.debug(f"开始调用 Dify API - 用户消息: {processed_query}")
            conversation_id = self.db.get_llm_thread_id(from_wxid, namespace="dify")

            user_wxid = sender_wxid
            try:
                user_username = await bot.get_nickname(user_wxid) or "未知用户"
            except:
                user_username = "未知用户"

            inputs = {
                "user_wxid": user_wxid,
                "user_username": user_username
            }

            payload = {
                "inputs": inputs,
                "query": processed_query,
                "response_mode": "streaming",
                "conversation_id": conversation_id,
                "user": from_wxid,
                "files": [],
                "auto_generate_name": False,
            }

            # 直接连接调用，禁用代理功能
            use_api_proxy = False
            logger.debug(f"发送请求到 Dify - URL: {model.base_url}/chat-messages, Payload: {json.dumps(payload)}")

            # 使用直接连接
            headers = {"Authorization": f"Bearer {model.api_key}", "Content-Type": "application/json"}
            ai_resp = ""
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(url=f"{model.base_url}/chat-messages", headers=headers,
                                        data=json.dumps(payload)) as resp:
                    if resp.status in (200, 201):
                        async for line in resp.content:
                            line = line.decode("utf-8").strip()
                            if not line or line == "event: ping":
                                continue
                            elif line.startswith("data: "):
                                line = line[6:]
                            try:
                                resp_json = json.loads(line)
                            except json.JSONDecodeError:
                                logger.error(f"Dify返回的JSON解析错误: {line}")
                                continue

                            event = resp_json.get("event", "")
                            if event == "message":
                                ai_resp += resp_json.get("answer", "")
                            elif event == "message_replace":
                                ai_resp = resp_json.get("answer", "")
                        new_con_id = resp_json.get("conversation_id", "")
                        if new_con_id and new_con_id != conversation_id:
                            self.db.save_llm_thread_id(from_wxid, new_con_id, "dify")
                        ai_resp = ai_resp.rstrip()
                        logger.debug(f"Dify响应: {ai_resp}")
                    elif resp.status == 404:
                        logger.warning("会话ID不存在，重置会话ID并重试")
                        self.db.save_llm_thread_id(from_wxid, "", "dify")
                        # 重要：在递归调用时必须传递原始模型，不要重新选择
                        return "会话错误：会话ID不存在"
                    elif resp.status == 400:
                        return "会话错误：会话返回了400"
                    elif resp.status == 500:
                        return "会话错误：会话返回了500"
                    else:
                        return "会话错误：会话返回了其它响应状态码"
            if ai_resp:
                return ai_resp
            else:
                logger.warning("Dify未返回有效响应")
                return "Dify未返回有效响应"
        except Exception as e:
            logger.error(f"Dify API 调用失败: {e}")
            return f"Dify API 调用失败: {e}"

    async def dify(self, bot: WechatAPIClient, message: dict, query: str, files=None, specific_model=None):
        """发送消息到Dify API"""
        if files is None:
            files = []

        # 如果提供了specific_model，直接使用；否则根据消息内容选择模型
        if specific_model:
            model = specific_model
            processed_query = query
            is_switch = False
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.info(f"使用指定的模型 '{model_name}'")
        else:
            # 根据消息内容选择模型
            model, processed_query, is_switch = self.get_model_from_message(query, message["SenderWxid"])
            model_name = next((name for name, config in self.models.items() if config == model), '默认')
            logger.info(f"从消息内容选择模型 '{model_name}'")
            
            # 如果是切换模型的命令
            if is_switch:
                model_name = next(name for name, config in self.models.items() if config == model)
                await bot.send_text_message(
                    message["FromWxid"], 
                    f"已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。"
                )
                return

        # 记录将要使用的模型配置
        logger.info(f"模型API密钥: {model.api_key[:5]}...{model.api_key[-5:] if len(model.api_key) > 10 else ''}")
        logger.info(f"模型API端点: {model.base_url}")
        
        # 处理文件上传
        formatted_files = []
        for file_id in files:
            formatted_files.append({
                "type": "image",  # 修改为image类型
                "transfer_method": "local_file",
                "upload_file_id": file_id
            })

        try:
            logger.debug(f"开始调用 Dify API - 用户消息: {processed_query}")
            logger.debug(f"文件列表: {formatted_files}")
            conversation_id = self.db.get_llm_thread_id(message["FromWxid"], namespace="dify")

            user_wxid = message["SenderWxid"]
            try:
                user_username = await bot.get_nickname(user_wxid) or "未知用户"
            except:
                user_username = "未知用户"

            inputs = {
                "user_wxid": user_wxid,
                "user_username": user_username
            }
            
            payload = {
                "inputs": inputs,
                "query": processed_query,
                "response_mode": "streaming",
                "conversation_id": conversation_id,
                "user": message["FromWxid"],
                "files": formatted_files,
                "auto_generate_name": False,
            }

            # 直接连接调用，禁用代理功能
            use_api_proxy = False
            logger.debug(f"发送请求到 Dify - URL: {model.base_url}/chat-messages, Payload: {json.dumps(payload)}")
            
            # 使用直接连接
            headers = {"Authorization": f"Bearer {model.api_key}", "Content-Type": "application/json"}
            ai_resp = ""
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(url=f"{model.base_url}/chat-messages", headers=headers, data=json.dumps(payload)) as resp:
                    if resp.status in (200, 201):
                        async for line in resp.content:
                            line = line.decode("utf-8").strip()
                            if not line or line == "event: ping":
                                continue
                            elif line.startswith("data: "):
                                line = line[6:]
                            try:
                                resp_json = json.loads(line)
                            except json.JSONDecodeError:
                                logger.error(f"Dify返回的JSON解析错误: {line}")
                                continue

                            event = resp_json.get("event", "")
                            if event == "message":
                                ai_resp += resp_json.get("answer", "")
                            elif event == "message_replace":
                                ai_resp = resp_json.get("answer", "")
                            elif event == "message_file":
                                file_url = resp_json.get("url", "")
                                await self.dify_handle_image(bot, message, file_url, model_config=model)
                            elif event == "error":
                                await self.dify_handle_error(bot, message,
                                                            resp_json.get("task_id", ""),
                                                            resp_json.get("message_id", ""),
                                                            resp_json.get("status", ""),
                                                            resp_json.get("code", ""),
                                                            resp_json.get("message", ""))
                        
                        new_con_id = resp_json.get("conversation_id", "")
                        if new_con_id and new_con_id != conversation_id:
                            self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")
                        ai_resp = ai_resp.rstrip()
                        logger.debug(f"Dify响应: {ai_resp}")
                    elif resp.status == 404:
                        logger.warning("会话ID不存在，重置会话ID并重试")
                        self.db.save_llm_thread_id(message["FromWxid"], "", "dify")
                        # 重要：在递归调用时必须传递原始模型，不要重新选择
                        return await self.dify_text(bot, message["SenderWxid"], message["FromWxid"], processed_query, files=files, specific_model=model)
                    elif resp.status == 400:
                        return await self.handle_400(bot, message, resp)
                    elif resp.status == 500:
                        return await self.handle_500(bot, message)
                    else:
                        return await self.handle_other_status(bot, message, resp)

            if ai_resp:
                reply = await self.dify_handle_text(bot, message, ai_resp, model)
                await self.mock_person_send_reply(bot, bot["FromWxid"], self.auto_message,reply)

            else:
                logger.warning("Dify未返回有效响应")
        except Exception as e:
            logger.error(f"Dify API 调用失败: {e}")
            await self.hendle_exceptions(bot, message, model_config=model)

    async def download_file(self, url: str) -> bytes:
        """
        下载文件并返回文件内容和MIME类型
        """
        async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
            async with session.get(url) as resp:
                content_type = resp.headers.get('Content-Type', '')
                return await resp.read()

    async def upload_file_to_dify(self, file_content: bytes, mime_type: str, user: str, model_config=None) -> Optional[str]:
        """上传文件到Dify并返回文件ID，兼容旧代码用的包装函数"""
        # 如果是图片类型，使用图片处理器处理
        if mime_type.startswith("image/"):
            return await self.image_processor.upload_to_dify(file_content, user, model_config or self.current_model)
        
        # 原有的上传逻辑不变
        try:
            # 使用传入的model_config，如果没有则使用默认模型
            model = model_config or self.current_model
            
            # 直接使用连接上传文件
            headers = {"Authorization": f"Bearer {model.api_key}"}
            formdata = aiohttp.FormData()
            formdata.add_field("file", file_content, 
                            filename=f"file.{mime_type.split('/')[-1]}", 
                            content_type=mime_type)
            formdata.add_field("user", user)

            url = f"{model.base_url}/files/upload"
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(url, headers=headers, data=formdata) as resp:
                    if resp.status in (200, 201):
                        result = await resp.json()
                        logger.debug(f"文件上传成功: {result}")
                        return result.get("id")
                    else:
                        error_text = await resp.text()
                        logger.error(f"文件上传失败: HTTP {resp.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"上传文件时发生错误: {e}")
            return None

    async def dify_handle_text(self, bot: WechatAPIClient, message: dict, text: str, model_config=None):
        # 使用传入的model_config，如果没有则使用默认模型
        model = model_config or self.current_model
        
        # 匹配Dify返回的图片引用格式
        image_pattern = r'\[(.*?)\]\((.*?)\)'
        matches = re.findall(image_pattern, text)
        
        # 移除所有图片引用文本
        text = re.sub(image_pattern, '', text)
        
        # 先发送文字内容
        if text:
            if message["MsgType"] == 34 or len(text) >= self.voice_reply_length:
                await self.text_to_voice_message(bot, message, text)
            else:
                # paragraphs = text.split("//n")
                # for paragraph in paragraphs:
                #     if paragraph.strip():
                        # await bot.send_text_message(message["FromWxid"], paragraph.strip())
                await self.mock_person_send_reply(bot, message["FromWxid"], self.auto_message, text)

        # 如果有图片引用，只处理最后一个
        if matches:
            filename, url = matches[-1]  # 只取最后一个图片
            try:
                # 如果URL是相对路径,添加base_url
                if url.startswith('/files'):
                    # 移除base_url中可能的v1路径
                    base_url = model.base_url.replace('/v1', '')
                    url = f"{base_url}{url}"
                
                logger.debug(f"处理图片链接: {url}")
                headers = {"Authorization": f"Bearer {model.api_key}"}
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

        # 处理其他类型的链接
        pattern = r"\]$$(https?:\/\/[^\s$$]+)\)"
        links = re.findall(pattern, text)
        for url in links:
            try:
                file = await self.download_file(url)
                extension = filetype.guess_extension(file)
                if extension in ('wav', 'mp3'):
                    await bot.send_voice_message(message["FromWxid"], voice=file, format=extension)
                elif extension in ('jpg', 'jpeg', "png", "gif", "bmp", "svg"):
                    await bot.send_image_message(message["FromWxid"], file)
                elif extension in ('mp4', 'avi', 'mov', 'mkv', 'flv'):
                    await bot.send_video_message(message["FromWxid"], video=file, image="None")
            except Exception as e:
                logger.error(f"下载文件 {url} 失败: {e}")
                await bot.send_text_message(message["FromWxid"], f"下载文件 {url} 失败")

        # 识别普通文件链接
        file_pattern = r'https?://[^\s<>"]+?/[^\s<>"]+\.(?:pdf|doc|docx|xls|xlsx|txt|zip|rar|7z|tar|gz)'
        file_links = re.findall(file_pattern, text)
        for url in file_links:
            await self.download_and_send_file(bot, message, url)

        pattern = r'\$\$[^$$]+\]\$\$https?:\/\/[^\s$$]+\)'
        text = re.sub(pattern, '', text)

    async def dify_handle_image(self, bot: WechatAPIClient, message: dict, image: Union[str, bytes], model_config=None):
        if isinstance(image, str) and image.startswith("http"):
            try:
                async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                    async with session.get(image) as resp:
                        image = bot.byte_to_base64(await resp.read())
            except Exception as e:
                logger.error(f"下载图片 {image} 失败: {e}")
                await bot.send_text_message(message["FromWxid"], f"下载图片 {image} 失败")
                return
        elif isinstance(image, bytes):
            image = bot.byte_to_base64(image)
        await bot.send_image_message(message["FromWxid"], image)

    @staticmethod
    async def dify_handle_error(bot: WechatAPIClient, message: dict, task_id: str, message_id: str, status: str,
                                code: int, err_message: str):
        output = (XYBOT_PREFIX +
                  DIFY_ERROR_MESSAGE +
                  f"任务 ID：{task_id}\n"
                  f"消息唯一 ID：{message_id}\n"
                  f"HTTP 状态码：{status}\n"
                  f"错误码：{code}\n"
                  f"错误信息：{err_message}")
        await bot.send_text_message(message["FromWxid"], output)

    @staticmethod
    async def handle_400(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        output = (XYBOT_PREFIX +
                  "🙅对不起，出现错误！\n"
                  f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_text_message(message["FromWxid"], output)

    @staticmethod
    async def handle_500(bot: WechatAPIClient, message: dict):
        output = XYBOT_PREFIX + "🙅对不起，Dify服务内部异常，请稍后再试。"
        await bot.send_text_message(message["FromWxid"], output)

    @staticmethod
    async def handle_other_status(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        ai_resp = (XYBOT_PREFIX +
                   f"🙅对不起，出现错误！\n"
                   f"状态码：{resp.status}\n"
                   f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_text_message(message["FromWxid"], ai_resp)

    @staticmethod
    async def hendle_exceptions(bot: WechatAPIClient, message: dict, model_config=None):
        output = (XYBOT_PREFIX +
                  "🙅对不起，出现错误！\n"
                  f"错误信息：\n"
                  f"{traceback.format_exc()}")
        await bot.send_text_message(message["FromWxid"], output)

    async def _check_point(self, bot: WechatAPIClient, message: dict, model_config=None) -> bool:
        wxid = message["SenderWxid"]
        if wxid in self.admins and self.admin_ignore:
            return True
        elif self.db.get_whitelist(wxid) and self.whitelist_ignore:
            return True
        else:
            if self.db.get_points(wxid) < (model_config or self.current_model).price:
                await bot.send_text_message(message["FromWxid"],
                                            XYBOT_PREFIX +
                                            INSUFFICIENT_POINTS_MESSAGE.format(price=(model_config or self.current_model).price))
                return False
            self.db.add_points(wxid, -((model_config or self.current_model).price))
            return True

    async def audio_to_text(self, bot: WechatAPIClient, message: dict) -> str:
        if not shutil.which("ffmpeg"):
            logger.error("未找到ffmpeg，请安装并配置到环境变量")
            await bot.send_text_message(message["FromWxid"], "服务器缺少ffmpeg，无法处理语音")
            return ""
        
        silk_file = "temp_audio.silk"
        mp3_file = "temp_audio.mp3"
        try:
            with open(silk_file, "wb") as f:
                f.write(message["Content"])

            command = f"ffmpeg -y -i {silk_file} -ar 16000 -ac 1 -f mp3 {mp3_file}"
            process = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
            if process.returncode != 0:
                logger.error(f"ffmpeg 执行失败: {process.stderr}")
                return ""

            if self.audio_to_text_url:
                headers = {"Authorization": f"Bearer {self.current_model.api_key}"}
                formdata = aiohttp.FormData()
                with open(mp3_file, "rb") as f:
                    mp3_data = f.read()
                formdata.add_field("file", mp3_data, filename="audio.mp3", content_type="audio/mp3")
                formdata.add_field("user", message["SenderWxid"])
                async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                    async with session.post(self.audio_to_text_url, headers=headers, data=formdata) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            text = result.get("text", "")
                            if "failed" in text.lower() or "code" in text.lower():
                                logger.error(f"Dify API 返回错误: {text}")
                            else:
                                logger.info(f"语音转文字结果 (Dify API): {text}")
                                return text
                        else:
                            logger.error(f"audio-to-text 接口调用失败: {resp.status} - {await resp.text()}")

            command = f"ffmpeg -y -i {mp3_file} {silk_file.replace('.silk', '.wav')}"
            process = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
            if process.returncode != 0:
                logger.error(f"ffmpeg 转为 WAV 失败: {process.stderr}")
                return ""
            # r = sr.Recognizer()
            # with sr.AudioFile(silk_file.replace('.silk', '.wav')) as source:
            #     audio = r.record(source)
            # text = r.recognize_google(audio, language="zh-CN")
            speechpath = os.path.abspath(silk_file.replace('.silk', '.wav'))
            api = XunfeiRequestApi(appid=self.xunfei_appid, secret_key=self.xunfei_secret_key, upload_file_path=speechpath)
            text = api.all_api_request()
            logger.info(f"语音转文字结果 (Google): {text}")
            return text
        except Exception as e:
            logger.error(f"语音处理失败: {e}")
            return ""
        finally:
            for temp_file in [silk_file, mp3_file, silk_file.replace('.silk', '.wav')]:
                if os.path.exists(temp_file):
                    os.remove(temp_file)

    async def text_to_voice_message(self, bot: WechatAPIClient, message: dict, text: str):
        try:
            # url = self.text_to_audio_url if self.text_to_audio_url else f"{self.current_model.base_url}/text-to-audio"
            # headers = {"Authorization": f"Bearer {self.current_model.api_key}", "Content-Type": "application/json"}
            # data = {"text": text, "user": message["SenderWxid"]}
            url = "https://api.siliconflow.cn/v1/audio/speech"
            data = {
                "model": "FunAudioLLM/CosyVoice2-0.5B",
                "input": text,
                "voice": "FunAudioLLM/CosyVoice2-0.5B:anna",
                "response_format": "wav",
                "sample_rate": 16000,
                "stream": False,
                "speed": 1,
                "gain": 0
            }
            headers = {
                "Authorization": self.silicon_key,
                "Content-Type": "application/json"
            }
            tf = tempfile.NamedTemporaryFile(suffix=".wav")
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(url, headers=headers, json=data) as resp:
                    if resp.status == 200:
                        audio = await resp.read()
                        tf.write(audio)
                        silk_voice = convert_to_silk(str(tf.name))
                        await bot.send_voice_message(message["FromWxid"], voice=audio, format="wav")
                    else:
                        logger.error(f"text-to-audio 接口调用失败: {resp.status} - {await resp.text()}")
                        await bot.send_text_message(message["FromWxid"], TEXT_TO_VOICE_FAILED)
            tf.close()
        except Exception as e:
            logger.error(f"text-to-audio 接口调用异常: {e}")
            traceback.print_exc()
            await bot.send_text_message(message["FromWxid"], f"{TEXT_TO_VOICE_FAILED}: {str(e)}")

    async def get_cached_image(self, user_wxid: str) -> Optional[bytes]:
        """获取用户最近的图片，兼容旧代码用的包装函数"""
        return self.image_processor.get_cached_image(user_wxid)

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
                        await bot.send_text_message(message["FromWxid"], f"文件名: {filename}\n内容长度: {len(content)} 字节")
                    
                    logger.debug(f"文件 {filename} 发送成功")
                    
        except Exception as e:
            logger.error(f"下载或发送文件失败: {e}")
            await bot.send_text_message(message["FromWxid"], f"处理文件失败: {str(e)}")

    @on_file_message(priority=20)
    async def handle_file(self, bot: WechatAPIClient, message: dict):
        """处理文件消息"""
        if not self.enable:
            return
        # 文件消息处理功能已禁用，直接返回
        logger.info("文件消息处理功能已禁用，跳过处理")
        return

    # 添加辅助函数来处理消息和模型选择

    async def _process_message_with_model(self, bot: WechatAPIClient, message: dict, content: str, files=None):
        """统一处理消息和模型选择逻辑
        
        Args:
            bot: 微信API客户端
            message: 消息字典
            content: 消息内容
            files: 可选的文件ID列表
            
        Returns:
            bool: 处理是否成功
        """
        if not content:
            logger.debug("消息内容为空，不处理")
            return False
            
        # 获取对应模型和处理后的内容
        model, processed_query, is_switch = self.get_model_from_message(content, message["SenderWxid"])
        
        # 处理模型切换请求
        if is_switch:
            model_name = next(name for name, config in self.models.items() if config == model)
            if message["IsGroup"]:
                await bot.send_at_message(
                    message["FromWxid"],
                    f"\n已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。",
                    [message["SenderWxid"]]
                )
            else:
                await bot.send_text_message(
                    message["FromWxid"],
                    f"已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。"
                )
            return True
            
        # 检查API密钥是否可用
        if not model.api_key:
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.error(f"模型 '{model_name}' 的API密钥未配置")
            
            if message["IsGroup"]:
                await bot.send_at_message(
                    message["FromWxid"],
                    f"\n此模型API密钥未配置，请联系管理员",
                    [message["SenderWxid"]]
                )
            else:
                await bot.send_text_message(
                    message["FromWxid"],
                    "所选模型的API密钥未配置，请联系管理员"
                )
            return False
            
        # 检查积分
        if not await self._check_point(bot, message, model):
            logger.info("积分检查失败，终止请求")
            return False
            
        # 调用API
        try:
            reply = await self.dify_text(bot, message["SenderWxid"], message["FromWxid"], processed_query, files=files, specific_model=model)
            await self.mock_person_send_reply(bot, message["FromWxid"], self.auto_message, reply)
            return True
        except Exception as e:
            logger.error(f"调用Dify API失败: {e}")
            logger.debug(traceback.format_exc())
            
            if message["IsGroup"]:
                await bot.send_at_message(
                    message["FromWxid"],
                    "\n消息处理失败，请稍后重试。",
                    [message["SenderWxid"]]
                )
            else:
                await bot.send_text_message(
                    message["FromWxid"],
                    "消息处理失败，请稍后重试。"
                )
            return False
            
    async def _handle_at_or_command(self, bot: WechatAPIClient, message: dict, content: str, files=None):
        """处理@消息或命令消息
        
        Args:
            bot: 微信API客户端
            message: 消息字典
            content: 消息内容
            files: 可选的文件ID列表
            
        Returns:
            bool: 是否已处理消息
        """
        if not content:
            if message["IsGroup"]:
                await bot.send_at_message(
                    message["FromWxid"],
                    "\n请输入你的问题或指令。",
                    [message["SenderWxid"]]
                )
            else:
                await bot.send_text_message(
                    message["FromWxid"],
                    "请输入你的问题或指令。"
                )
            return True
            
        # 检查是否需要移除机器人名称或命令前缀
        if message["IsGroup"]:
            # 移除机器人名称
            for robot_name in self.robot_names:
                content = content.replace(f"@{robot_name}", "").strip()
                
            # 移除命令前缀
            command = content.split(" ")[0] if content else ""
            if command in self.commands:
                content = content[len(command):].strip()
                
        return await self._process_message_with_model(bot, message, content, files)
        
    async def _handle_chatroom_command(self, bot: WechatAPIClient, message: dict, content: str):
        """处理聊天室特定命令
        
        Args:
            bot: 微信API客户端
            message: 消息字典
            content: 消息内容
            
        Returns:
            bool: 是否已处理命令
        """
        group_id = message["FromWxid"]
        user_wxid = message["SenderWxid"]
        
        # 处理聊天室专属命令
        if content == "退出聊天":
            if self.chat_manager.is_user_active(group_id, user_wxid):
                self.chat_manager.remove_user(group_id, user_wxid)
                await bot.send_at_message(group_id, "\n" + CHAT_LEAVE_MESSAGE, [user_wxid])
            return True
        elif content == "查看状态":
            status_msg = self.chat_manager.format_room_status(group_id)
            await bot.send_at_message(group_id, "\n" + status_msg, [user_wxid])
            return True
        elif content == "暂时离开":
            self.chat_manager.set_user_status(group_id, user_wxid, UserStatus.AWAY)
            await bot.send_at_message(group_id, "\n" + CHAT_AWAY_MESSAGE, [user_wxid])
            return True
        elif content == "回来了":
            self.chat_manager.set_user_status(group_id, user_wxid, UserStatus.ACTIVE)
            self.chat_manager.update_user_activity(group_id, user_wxid)
            await bot.send_at_message(group_id, "\n" + CHAT_BACK_MESSAGE, [user_wxid])
            return True
        elif content == "我的统计":
            try:
                nickname = await bot.get_nickname(user_wxid) or "未知用户"
            except:
                nickname = "未知用户"
            stats_msg = self.chat_manager.format_user_stats(group_id, user_wxid, nickname)
            await bot.send_at_message(group_id, "\n" + stats_msg, [user_wxid])
            return True
        elif content == "聊天室排行":
            ranking_msg = await self.chat_manager.format_room_ranking(group_id, bot)
            await bot.send_at_message(group_id, "\n" + ranking_msg, [user_wxid])
            return True
        
        return False
        
    async def _prepare_image_attachments(self, message: dict, specific_model=None):
        """准备消息的图片附件
        
        Args:
            message: 消息字典
            specific_model: 可选的指定模型配置
            
        Returns:
            list: 文件ID列表
        """
        # 检查是否有最近的图片
        files = []
        image_content = await self.get_cached_image(message["FromWxid"])
        
        if image_content:
            try:
                logger.debug("发现最近的图片，准备上传到Dify")
                
                # 确定使用哪个模型
                model_config = specific_model or self.get_user_model(message["SenderWxid"])
                
                # 上传图片
                file_id = await self.upload_file_to_dify(
                    image_content,
                    "image/jpeg",
                    message["FromWxid"],
                    model_config=model_config
                )
                
                if file_id:
                    logger.debug(f"图片上传成功，文件ID: {file_id}")
                    files.append(file_id)
                else:
                    logger.error("图片上传失败")
            except Exception as e:
                logger.error(f"处理图片失败: {e}")
                
        return files
