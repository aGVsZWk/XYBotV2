import datetime
import sqlite3
import tomllib
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple, Union

from loguru import logger
from sqlalchemy import Column, String, Integer, DateTime, create_engine, JSON, Boolean
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import timedelta
from utils.singleton import Singleton
import asyncio
import re

Base = declarative_base()


class User(Base):
    __tablename__ = 'user'

    wxid = Column(String(20), primary_key=True, nullable=False, unique=True, index=True, autoincrement=False,
                  comment='wxid')
    points = Column(Integer, nullable=False, default=0, comment='points')
    signin_stat = Column(DateTime, nullable=False, default=datetime.datetime.fromtimestamp(0), comment='signin_stat')
    signin_streak = Column(Integer, nullable=False, default=0, comment='signin_streak')
    whitelist = Column(Boolean, nullable=False, default=False, comment='whitelist')
    llm_thread_id = Column(JSON, nullable=False, default=lambda: {}, comment='llm_thread_id')


class Chatroom(Base):
    __tablename__ = 'chatroom'

    chatroom_id = Column(String(20), primary_key=True, nullable=False, unique=True, index=True, autoincrement=False,
                         comment='chatroom_id')
    members = Column(JSON, nullable=False, default=list, comment='members')
    llm_thread_id = Column(JSON, nullable=False, default=lambda: {}, comment='llm_thread_id')


class BotDatabase(metaclass=Singleton):
    def __init__(self):
        with open("main_config.toml", "rb") as f:
            main_config = tomllib.load(f)

        self.database_url = main_config["XYBot"]["database-url"]
        self.engine = create_engine(self.database_url)
        self.DBSession = sessionmaker(bind=self.engine)

        # 创建表
        Base.metadata.create_all(self.engine)
        logger.success("数据库初始化成功")

        # 创建线程池执行器
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="database")

    def _execute_in_queue(self, method, *args, **kwargs):
        """在队列中执行数据库操作"""
        future = self.executor.submit(method, *args, **kwargs)
        try:
            return future.result(timeout=20)  # 20秒超时
        except Exception as e:
            logger.error(f"数据库操作失败: {method.__name__} - {str(e)}")
            raise

    # USER

    def add_points(self, wxid: str, num: int) -> bool:
        """Thread-safe point addition"""
        return self._execute_in_queue(self._add_points, wxid, num)

    def _add_points(self, wxid: str, num: int) -> bool:
        """Thread-safe point addition"""
        session = self.DBSession()
        try:
            # Use UPDATE with atomic operation
            result = session.execute(
                update(User)
                .where(User.wxid == wxid)
                .values(points=User.points + num)
            )
            if result.rowcount == 0:
                # User doesn't exist, create new
                user = User(wxid=wxid, points=num)
                session.add(user)
            logger.info(f"数据库: 用户{wxid}积分增加{num}")
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"数据库: 用户{wxid}积分增加失败, 错误: {e}")
            return False
        finally:
            session.close()

    def set_points(self, wxid: str, num: int) -> bool:
        """Thread-safe point setting"""
        return self._execute_in_queue(self._set_points, wxid, num)

    def _set_points(self, wxid: str, num: int) -> bool:
        """Thread-safe point setting"""
        session = self.DBSession()
        try:
            result = session.execute(
                update(User)
                .where(User.wxid == wxid)
                .values(points=num)
            )
            if result.rowcount == 0:
                user = User(wxid=wxid, points=num)
                session.add(user)
            logger.info(f"数据库: 用户{wxid}积分设置为{num}")
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"数据库: 用户{wxid}积分设置失败, 错误: {e}")
            return False
        finally:
            session.close()

    def get_points(self, wxid: str) -> int:
        """Get user points"""
        return self._execute_in_queue(self._get_points, wxid)

    def _get_points(self, wxid: str) -> int:
        """Get user points"""
        session = self.DBSession()
        try:
            user = session.query(User).filter_by(wxid=wxid).first()
            return user.points if user else 0
        finally:
            session.close()

    def get_signin_stat(self, wxid: str) -> datetime.datetime:
        """获取用户签到状态"""
        return self._execute_in_queue(self._get_signin_stat, wxid)

    def _get_signin_stat(self, wxid: str) -> datetime.datetime:
        session = self.DBSession()
        try:
            user = session.query(User).filter_by(wxid=wxid).first()
            return user.signin_stat if user else datetime.datetime.fromtimestamp(0)
        finally:
            session.close()

    def set_signin_stat(self, wxid: str, signin_time: datetime.datetime) -> bool:
        """Thread-safe set user's signin time"""
        return self._execute_in_queue(self._set_signin_stat, wxid, signin_time)

    def _set_signin_stat(self, wxid: str, signin_time: datetime.datetime) -> bool:
        session = self.DBSession()
        try:
            result = session.execute(
                update(User)
                .where(User.wxid == wxid)
                .values(
                    signin_stat=signin_time,
                    signin_streak=User.signin_streak
                )
            )
            if result.rowcount == 0:
                user = User(
                    wxid=wxid,
                    signin_stat=signin_time,
                    signin_streak=0
                )
                session.add(user)
            logger.info(f"数据库: 用户{wxid}登录时间设置为{signin_time}")
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"数据库: 用户{wxid}登录时间设置失败, 错误: {e}")
            return False
        finally:
            session.close()

    def reset_all_signin_stat(self) -> bool:
        """Reset all users' signin status"""
        session = self.DBSession()
        try:
            session.query(User).update({User.signin_stat: datetime.datetime.fromtimestamp(0)})
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"数据库: 重置所有用户登录时间失败, 错误: {e}")
            return False
        finally:
            session.close()

    def get_leaderboard(self, count: int) -> list:
        """Get points leaderboard"""
        session = self.DBSession()
        try:
            users = session.query(User).order_by(User.points.desc()).limit(count).all()
            return [(user.wxid, user.points) for user in users]
        finally:
            session.close()

    def set_whitelist(self, wxid: str, stat: bool) -> bool:
        """Set user's whitelist status"""
        session = self.DBSession()
        try:
            user = session.query(User).filter_by(wxid=wxid).first()
            if not user:
                user = User(wxid=wxid)
                session.add(user)
            user.whitelist = stat
            session.commit()
            logger.info(f"数据库: 用户{wxid}白名单状态设置为{stat}")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"数据库: 用户{wxid}白名单状态设置失败, 错误: {e}")
            return False
        finally:
            session.close()

    def get_whitelist(self, wxid: str) -> bool:
        """Get user's whitelist status"""
        session = self.DBSession()
        try:
            user = session.query(User).filter_by(wxid=wxid).first()
            return user.whitelist if user else False
        finally:
            session.close()

    def get_whitelist_list(self) -> list:
        """Get list of all whitelisted users"""
        session = self.DBSession()
        try:
            users = session.query(User).filter_by(whitelist=True).all()
            return [user.wxid for user in users]
        finally:
            session.close()

    def safe_trade_points(self, trader_wxid: str, target_wxid: str, num: int) -> bool:
        """Thread-safe points trading between users"""
        return self._execute_in_queue(self._safe_trade_points, trader_wxid, target_wxid, num)

    def _safe_trade_points(self, trader_wxid: str, target_wxid: str, num: int) -> bool:
        """Thread-safe points trading between users"""
        session = self.DBSession()
        try:
            # Start transaction with row-level locking
            trader = session.query(User).filter_by(wxid=trader_wxid) \
                .with_for_update().first()  # Acquire row lock
            target = session.query(User).filter_by(wxid=target_wxid) \
                .with_for_update().first()  # Acquire row lock

            if not trader:
                trader = User(wxid=trader_wxid)
                session.add(trader)
            if not target:
                target = User(wxid=target_wxid)
                session.add(target)
                session.flush()  # Ensure IDs are generated

            if trader.points >= num:
                trader.points -= num
                target.points += num
                session.commit()
                logger.info(f"数据库: 用户{trader_wxid}给用户{target_wxid}转账{num}积分")
                return True
            logger.info(f"数据库: 转账失败, 用户{trader_wxid}积分不足")
            session.rollback()
            return False
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"数据库: 转账失败, 错误: {e}")
            return False
        finally:
            session.close()

    def get_user_list(self) -> list:
        """Get list of all users"""
        session = self.DBSession()
        try:
            users = session.query(User).all()
            return [user.wxid for user in users]
        finally:
            session.close()

    def get_llm_thread_id(self, wxid: str, namespace: str = None) -> Union[dict, str]:
        """Get LLM thread id for user or chatroom"""
        session = self.DBSession()
        try:
            # Check if it's a chatroom ID
            if wxid.endswith("@chatroom"):
                chatroom = session.query(Chatroom).filter_by(chatroom_id=wxid).first()
                if namespace:
                    return chatroom.llm_thread_id.get(namespace, "") if chatroom else ""
                else:
                    return chatroom.llm_thread_id if chatroom else {}
            else:
                # Regular user
                user = session.query(User).filter_by(wxid=wxid).first()
                if namespace:
                    return user.llm_thread_id.get(namespace, "") if user else ""
                else:
                    return user.llm_thread_id if user else {}
        finally:
            session.close()

    def save_llm_thread_id(self, wxid: str, data: str, namespace: str) -> bool:
        """Save LLM thread id for user or chatroom"""
        session = self.DBSession()
        try:
            if wxid.endswith("@chatroom"):
                chatroom = session.query(Chatroom).filter_by(chatroom_id=wxid).first()
                if not chatroom:
                    chatroom = Chatroom(
                        chatroom_id=wxid,
                        llm_thread_id={}
                    )
                    session.add(chatroom)
                # 创建新字典并更新
                new_thread_ids = dict(chatroom.llm_thread_id or {})
                new_thread_ids[namespace] = data
                chatroom.llm_thread_id = new_thread_ids
            else:
                user = session.query(User).filter_by(wxid=wxid).first()
                if not user:
                    user = User(
                        wxid=wxid,
                        llm_thread_id={}
                    )
                    session.add(user)
                # 创建新字典并更新
                new_thread_ids = dict(user.llm_thread_id or {})
                new_thread_ids[namespace] = data
                user.llm_thread_id = new_thread_ids

            session.commit()
            logger.info(f"数据库: 成功保存 {wxid} 的 llm thread id")
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"数据库: 保存用户llm thread id失败, 错误: {e}")
            return False
        finally:
            session.close()

    def delete_all_llm_thread_id(self):
        """Clear llm thread id for everyone"""
        session = self.DBSession()
        try:
            session.query(User).update({User.llm_thread_id: {}})
            session.query(Chatroom).update({Chatroom.llm_thread_id: {}})
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"数据库: 清除所有用户llm thread id失败, 错误: {e}")
            return False
        finally:
            session.close()

    def get_signin_streak(self, wxid: str) -> int:
        """Thread-safe get user's signin streak"""
        return self._execute_in_queue(self._get_signin_streak, wxid)

    def _get_signin_streak(self, wxid: str) -> int:
        session = self.DBSession()
        try:
            user = session.query(User).filter_by(wxid=wxid).first()
            return user.signin_streak if user else 0
        finally:
            session.close()

    def set_signin_streak(self, wxid: str, streak: int) -> bool:
        """Thread-safe set user's signin streak"""
        return self._execute_in_queue(self._set_signin_streak, wxid, streak)

    def _set_signin_streak(self, wxid: str, streak: int) -> bool:
        session = self.DBSession()
        try:
            result = session.execute(
                update(User)
                .where(User.wxid == wxid)
                .values(signin_streak=streak)
            )
            if result.rowcount == 0:
                user = User(wxid=wxid, signin_streak=streak)
                session.add(user)
            logger.info(f"数据库: 用户{wxid}连续签到天数设置为{streak}")
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            logger.error(f"数据库: 用户{wxid}连续签到天数设置失败, 错误: {e}")
            return False
        finally:
            session.close()

    # CHATROOM

    def get_chatroom_list(self) -> list:
        """Get list of all chatrooms"""
        session = self.DBSession()
        try:
            chatrooms = session.query(Chatroom).all()
            return [chatroom.chatroom_id for chatroom in chatrooms]
        finally:
            session.close()

    def get_chatroom_members(self, chatroom_id: str) -> set:
        """Get members of a chatroom"""
        session = self.DBSession()
        try:
            chatroom = session.query(Chatroom).filter_by(chatroom_id=chatroom_id).first()
            return set(chatroom.members) if chatroom else set()
        finally:
            session.close()

    def set_chatroom_members(self, chatroom_id: str, members: set) -> bool:
        """Set members of a chatroom"""
        session = self.DBSession()
        try:
            chatroom = session.query(Chatroom).filter_by(chatroom_id=chatroom_id).first()
            if not chatroom:
                chatroom = Chatroom(chatroom_id=chatroom_id)
                session.add(chatroom)
            chatroom.members = list(members)  # Convert set to list for JSON storage
            logger.info(f"Database: Set chatroom {chatroom_id} members successfully")
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"Database: Set chatroom {chatroom_id} members failed, error: {e}")
            return False
        finally:
            session.close()

    def __del__(self):
        """确保关闭时清理资源"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=True)
        if hasattr(self, 'engine'):
            self.engine.dispose()


class ChatHistoryDatabase(metaclass=Singleton):
    def __init__(self):
        with open("main_config.toml", "rb") as f:
            main_config = tomllib.load(f)
        database_file = main_config["XYBot"]["chat-history-file"]
        self.db_connection = sqlite3.connect(database_file)
        logger.success("聊天记录数据库链接成功")

    def create_table_if_not_exists(self, message_type, chat_id: str):
        table_name = ""
        try:
            if message_type == "text":
                """为每个chat_id创建一个单独的表"""
                table_name = self.get_text_table_name(chat_id)
                cursor = self.db_connection.cursor()
                cursor.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{table_name}" (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sender_wxid TEXT NOT NULL,
                        create_time INTEGER NOT NULL,  -- 使用 INTEGER 存储时间戳
                        content TEXT NOT NULL
                    )
                """)
            elif message_type == "emoji":
                table_name = self.get_emoji_table_name(chat_id)
                cursor = self.db_connection.cursor()
                cursor.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{table_name}" (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sender_wxid TEXT NOT NULL,
                        create_time INTEGER NOT NULL,  -- 使用 INTEGER 存储时间戳
                        content TEXT NOT NULL,
                        blob_data BLOB NOT NULL,
                        len INTEGER NOT NULL,
                        md5 TEXT NOT NULL
                    )
                """)
            elif message_type == "memory":
                table_name = self.get_memory_table_name(chat_id)
                cursor = self.db_connection.cursor()
                cursor.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{table_name}" (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        create_time INTEGER NOT NULL,  -- 使用 INTEGER 存储时间戳
                        importance INTEGER NOT NULL,    -- 重要程度
                        summary TEXT NOT NULL, --- 总结文本
                        content TEXT NOT NULL
                    )
                """)
            self.db_connection.commit()
            logger.info(f"表 {table_name} 创建成功")
        except sqlite3.Error as e:
            logger.error(f"创建表 {table_name} 失败：{e}")
        return table_name

    def get_text_table_name(self, chat_id: str) -> str:
        """
        生成表名，将chat_id中的特殊字符替换掉，避免SQL注入和表名错误
        """
        return "chat_" + re.sub(r"[^a-zA-Z0-9_]", "_", chat_id)

    def get_emoji_table_name(self, chat_id: str) -> str:
        """
        生成表名，将chat_id中的特殊字符替换掉，避免SQL注入和表名错误
        """
        return "chat_emoji_" + re.sub(r"[^a-zA-Z0-9_]", "_", chat_id)

    def get_memory_table_name(self, chat_id: str) -> str:
        """
        生成表名，将chat_id中的特殊字符替换掉，避免SQL注入和表名错误
        """
        return "chat_memory_" + re.sub(r"[^a-zA-Z0-9_]", "_", chat_id)

    def save_message_to_db(self, message_type, chat_id: str, sender_wxid: str, create_time: int, content: any):
        """将消息保存到数据库"""
        table_name = self.create_table_if_not_exists(message_type, chat_id)
        cursor = self.db_connection.cursor()
        try:
            if message_type == "text":
                cursor.execute(f"""
                    INSERT INTO "{table_name}" (sender_wxid, create_time, content)
                    VALUES (?, ?, ?)
                """, (sender_wxid, create_time, content))
                self.db_connection.commit()
            elif message_type == "emoji":
                cursor = self.db_connection.cursor()
                cursor.execute(f"""
                    INSERT INTO "{table_name}" (sender_wxid, create_time, content, blob_data, len, md5)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (sender_wxid, create_time, content['Content'], content['BlobData'], content['Len'], content['MD5']))
                self.db_connection.commit()
            elif message_type == "memory":
                cursor = self.db_connection.cursor()
                cursor.execute(f"""
                    INSERT INTO "{table_name}" (create_time, importance, summary, content)
                    VALUES (?, ?, ?, ?)
                """, (create_time, content["Importance"], content["Summary"], content["Content"]))
                self.db_connection.commit()
            logger.debug(f"消息保存到表 {table_name}: sender_wxid={sender_wxid}, chat_id={chat_id}, create_time={create_time}")
        except sqlite3.Error as e:
            logger.exception(f"保存消息到表 {table_name} 失败: {e}")

    def get_text_messages_from_db(self, chat_id: str, limit: Optional[int] = None, duration: Optional[timedelta] = None, start_time:int=-1) -> List[Dict]:
        """从数据库获取消息，同时支持按条数和按时间范围获取"""
        table_name = self.get_text_table_name(chat_id)
        try:
            cursor = self.db_connection.cursor()
            if duration:
                cutoff_time = datetime.datetime.now() - duration
                cutoff_timestamp = int(cutoff_time.timestamp())
                cursor.execute(f"""
                    SELECT sender_wxid, create_time, content
                    FROM "{table_name}"
                    WHERE create_time >= ?
                    ORDER BY create_time DESC
                """, (cutoff_timestamp,))

            elif limit:
                cursor.execute(f"""
                    SELECT sender_wxid, create_time, content
                    FROM "{table_name}"
                    ORDER BY create_time DESC
                    LIMIT ?
                """, (limit,))
            elif start_time:
                cursor.execute(f"""
                    SELECT sender_wxid, create_time, content
                    FROM "{table_name}"
                    WHERE create_time>? ORDER BY create_time DESC
                """, (start_time,))
            else:
                return []  #避免不传limit和duration的情况
            rows = cursor.fetchall()
            # 将结果转换为字典列表，方便后续使用
            messages = []
            for row in rows:
                messages.append({
                    'sender_wxid': row[0],
                    'create_time': row[1],
                    'content': row[2]
                })
            if duration:
                logger.debug(f"从表 {table_name} 获取消息: duration={duration}, 数量={len(messages)}")
            else:
                logger.debug(f"从表 {table_name} 获取消息: limit={limit}, 数量={len(messages)}")
            return messages
        except sqlite3.Error as e:
            logger.exception(f"从表 {table_name} 获取消息失败: {e}")
            return []

    def get_emoji_messages_from_db(self, chat_id: str, limit: Optional[int] = None, duration: Optional[timedelta] = None) -> List[Dict]:
        """从数据库获取消息，同时支持按条数和按时间范围获取"""
        table_name = self.get_emoji_table_name(chat_id)
        try:
            cursor = self.db_connection.cursor()
            if duration:
                cutoff_time = datetime.datetime.now() - duration
                cutoff_timestamp = int(cutoff_time.timestamp())
                cursor.execute(f"""
                    SELECT sender_wxid, create_time, content, blob_data, len, md5
                    FROM "{table_name}"
                    WHERE create_time >= ?
                    ORDER BY create_time DESC
                """, (cutoff_timestamp,))
            elif limit:
                cursor.execute(f"""
                    SELECT sender_wxid, create_time, content, blob_data, len, md5
                    FROM "{table_name}"
                    ORDER BY create_time DESC
                    LIMIT ?
                """, (limit,))
            else:
                return []  #避免不传limit和duration的情况
            rows = cursor.fetchall()
            # 将结果转换为字典列表，方便后续使用
            messages = []
            for row in rows:
                messages.append({
                    'sender_wxid': row[0],
                    'create_time': row[1],
                    'content': row[2],
                    'blob_data': row[3],
                    'len': row[4],
                    'md5': row[5],
                })
            if duration:
                logger.debug(f"从表 {table_name} 获取消息: duration={duration}, 数量={len(messages)}")
            else:
                logger.debug(f"从表 {table_name} 获取消息: limit={limit}, 数量={len(messages)}")
            return messages
        except sqlite3.Error as e:
            logger.exception(f"从表 {table_name} 获取消息失败: {e}")
            return []

    def get_memory_messages_from_db(self, chat_id: str, limit: Optional[int] = None, duration: Optional[timedelta] = None, sort_by_importance: Optional[bool] = None) -> List[Dict]:
        """从数据库获取消息，同时支持按条数和按时间范围获取"""
        table_name = self.get_memory_table_name(chat_id)
        try:
            cursor = self.db_connection.cursor()
            if duration:
                cutoff_time = datetime.datetime.now() - duration
                cutoff_timestamp = int(cutoff_time.timestamp())
                cursor.execute(f"""
                    SELECT create_time, importance, summary, content
                    FROM "{table_name}"
                    WHERE create_time >= ?
                    ORDER BY create_time DESC
                """, (cutoff_timestamp,))
            elif limit:
                cursor.execute(f"""
                    SELECT create_time, importance, summary, content
                    FROM "{table_name}"
                    ORDER BY create_time DESC
                    LIMIT ?
                """, (limit,))
            elif sort_by_importance and limit:
                cursor.execute(f"""
                    SELECT create_time, importance, summary, content
                    FROM "{table_name}"
                    ORDER BY importance DESC limit ?
                    LIMIT ?
                """, (limit,))
            else:
                return []  #避免不传limit和duration的情况
            rows = cursor.fetchall()
            # 将结果转换为字典列表，方便后续使用
            messages = []
            for row in rows:
                messages.append({
                    'create_time': row[0],
                    'importance': row[1],
                    'summary': row[2],
                    'content': row[3]
                })
            if duration:
                logger.debug(f"从表 {table_name} 获取消息: duration={duration}, 数量={len(messages)}")
            else:
                logger.debug(f"从表 {table_name} 获取消息: limit={limit}, 数量={len(messages)}")
            return messages
        except sqlite3.Error as e:
            logger.exception(f"从表 {table_name} 获取消息失败: {e}")
            return []

    async def clear_old_messages(self):
        """定期清理旧消息"""
        while True:
            await asyncio.sleep(60 * 60 * 24)  # 每天检查一次
            # try:
            #     cutoff_time = datetime.datetime.now() - timedelta(days=3) # 3天前
            #     cutoff_timestamp = int(cutoff_time.timestamp())

            #     cursor = self.db_connection.cursor()

            #     # 获取所有表名
            #     cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            #     tables = [row[0] for row in cursor.fetchall() if row[0].startswith("chat_")] #只清理chat_开头的表

            #     for table in tables:
            #         try:
            #             cursor.execute(f"""
            #                 DELETE FROM "{table}"
            #                 WHERE create_time < ?
            #             """, (cutoff_timestamp,))
            #             self.db_connection.commit()
            #             logger.info(f"已清理表 {table} 中 {cutoff_timestamp} 之前的旧消息")
            #         except sqlite3.Error as e:
            #             logger.exception(f"清理表 {table} 失败: {e}")

            # except Exception as e:
            #     logger.exception(f"清理旧消息失败: {e}")

    def __del__(self):
        """确保关闭时清理资源"""
        self.db_connection.close()
