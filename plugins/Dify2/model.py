# -*- coding: utf-8 -*-
# @Time        : 2025/4/13
# @Author      : helei
# @File        : model.py
# @Description :
from enum import Enum
from typing import Optional, Union, Dict, List, Tuple
from dataclasses import dataclass, field
import asyncio


CHAT_TIMEOUT = 3600  # 1小时超时
CHAT_AWAY_TIMEOUT = 1800  # 30分钟自动离开


class UserStatus(Enum):
    ACTIVE = "活跃"
    AWAY = "离开"
    INACTIVE = "未加入"


@dataclass
class UserStats:
    total_messages: int = 0
    total_chars: int = 0
    join_count: int = 0
    last_active: float = 0
    total_active_time: float = 0
    status: UserStatus = UserStatus.INACTIVE


@dataclass
class ChatRoomUser:
    wxid: str
    group_id: str
    last_active: float
    status: UserStatus = UserStatus.ACTIVE
    stats: UserStats = field(default_factory=UserStats)


@dataclass
class MessageBuffer:
    messages: list[str] = field(default_factory=list)
    last_message_time: float = 0.0
    timer_task: Optional[asyncio.Task] = None
    message_count: int = 0
    files: list[str] = field(default_factory=list)


@dataclass
class ModelConfig:
    api_key: str
    base_url: str
    trigger_words: list[str]
    price: int
    wakeup_words: list[str] = field(default_factory=list)  # 添加唤醒词列表字段
