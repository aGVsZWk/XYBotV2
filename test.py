# -*- coding: utf-8 -*-
# @Time        : 2025/3/16
# @Author      : helei
# @File        : test.py
# @Description :
import sqlite3
from loguru import logger
import re
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Union, List, Dict
import pandas as pd
# from sentence_transformers import SentenceTransformer
from statsmodels.tsa.arima.model import ARIMA
import tomllib
import collections
import random


def get_messages_from_db(chat_id: str = None, chat_table: str = None, limit: Optional[int] = None,
                         duration: Optional[timedelta] = None) -> List[Dict]:
    """从数据库获取消息，同时支持按条数和按时间范围获取"""
    if chat_table is None:
        table_name = self.get_table_name(chat_id)
    else:
        table_name = chat_table
    try:
        connection = sqlite3.connect("chat_history.db")
        cursor = connection.cursor()
        if duration:
            cutoff_time = datetime.now() - duration
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
        else:
            return []  # 避免不传limit和duration的情况
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


def predit_next_time(df):
    # df['create_time'] = pd.to_datetime(df['create_time'])
    # 提取 UserA 发起的聊天
    print(df)
    order = df["sender_wxid"].value_counts().sort_index()
    # sorted_sender_list = order[order.columns[0]].values.tolist()
    # print(sorted_sender_list)
    # if len(sorted_sender_list) > 1:
    #     predict_user = sorted_sender_list[1]
    # else:
    #     predict_user = sorted_sender_list[0]
    predict_user = order.index[1]

    df['create_time'] = pd.to_datetime(df['create_time'])

    predict_df = df[df['sender_wxid'] == predict_user].copy()

    # 按分钟统计聊天次数（稀疏填充）
    print("predict_df")
    print(predict_df)
    minute_chats = predict_df.groupby(predict_df['create_time'].dt.floor('min')).size()
    print("mmmmmmmmmmmm")
    print(minute_chats)
    ts = pd.Series(minute_chats.values, index=minute_chats.index).reindex(
        pd.date_range(start=minute_chats.index.min(), end=minute_chats.index.max(), freq='min'), fill_value=0
    )

    # ARIMA 预测（简化数据量，使用小时级别降采样）
    print("qwewqeqweqwe")
    print(ts)
    ts_hourly = ts.resample('H').sum()  # 为性能考虑，先按小时聚合
    print("ts")
    print(ts_hourly)
    model = ARIMA(ts_hourly, order=(1, 1, 0))  # 参数可调
    model_fit = model.fit()
    forecast = model_fit.forecast(steps=48)  # 预测未来24小时
    print(forecast)

    # 找到下次聊天时间（频率 > 0 的最早时间）
    next_chat_hour = None
    for i, freq in enumerate(forecast):
        if freq > 0:
            next_chat_hour = ts_hourly.index[-1] + pd.Timedelta(hours=i + 1)
            break

    if next_chat_hour:
        # 时间偏好：根据历史分钟分布调整
        minute_dist = predict_df['create_time'].dt.minute.value_counts(normalize=True)
        likely_minute = random.choices(minute_dist.index, weights=minute_dist.values, k=1)[0]
        next_timestamp = next_chat_hour.replace(minute=likely_minute, second=0)

        # 转换为 Unix 时间戳
        unix_timestamp = int(next_timestamp.timestamp())
        print(f"预测下次聊天时间: {next_timestamp.strftime('%Y-%m-%d %H:%M:%S')} (Unix: {unix_timestamp})")
    else:
        print("预测无下次聊天")
        unix_timestamp = -1
    return unix_timestamp


def main():
    table = "chat_45238224202_chatroom"
    recent_chat_time = -1
    # 查询历史记录
    # self.get_messages_from_db(table)
    messages = []
    all_recent_time = dict()
    last_recent_time = dict()
    all_predit_time = dict()
    try:
        messages = get_messages_from_db(chat_table=table, limit=1000)
        # all_chat_msg[table] = messages
        if len(messages) > 0:
            recent_chat_time = messages[0]["create_time"]
            all_recent_time[table] = recent_chat_time
    except sqlite3.Error as e:
        logger.exception(f"查询聊天历史 {table} 失败: {e}")

    if all_recent_time.get(table, -1) > last_recent_time.get(table, -1):
        last_recent_time[table] = recent_chat_time
        df = pd.DataFrame(messages)
        next_chat_time = predit_next_time(df)
        all_predit_time[table] = next_chat_time
    print(all_predit_time)
    return all_predit_time


if __name__ == '__main__':
    main()
