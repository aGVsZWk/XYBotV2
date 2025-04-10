# -*- coding: utf-8 -*-
# @Time        : 2025/3/12
# @Author      : helei
# @File        : test.py
# @Description :
import json

import requests
headers = {
    "Authorization": "Bearer app-CvUVH3JbuDwILMtZqPEKl6MV",
    "Content-Type": "application/json"
}

url = "http://raspberrypi.local/v1/chat-messages"


def chat():
    data = {
        "inputs": {},
        "query": "我帅吗",
        "response_mode": "streaming",
        "conversation_id": "",
        "user": "57691689790@chatroom",
        "files": []
    }
    "http://192.168.1.14/v1/conversations/"
    resp = requests.post(url, headers=headers, data=json.dumps(data), stream=True)

    for chunk in resp.iter_content(chunk_size=1024):
        # 处理响应内容
        print(chunk.decode("utf-8"))


def delete():
    url = "http://raspberrypi.local/v1/conversations/fdff9914-8b13-4f98-ada5-4ba9355f59d5"
    data = {
        "user": "wxid_y8cnblbc15gc22"
    }

    resp = requests.delete(url, headers=headers, data=json.dumps(data), stream=True)
    print(resp.json())


delete()


# curl -X POST 'http://192.168.1.14/v1/chat-messages' \
# --header 'Authorization: Bearer {api_key}' \
# --header 'Content-Type: application/json' \
# --data-raw '{
#     "inputs": {},
#     "query": "What are the specs of the iPhone 13 Pro Max?",
#     "response_mode": "streaming",
#     "conversation_id": "",
#     "user": "abc-123",
#     "files": [
#       {
#         "type": "image",
#         "transfer_method": "remote_url",
#         "url": "https://cloud.dify.ai/logo/logo-site.png"
#       }
#     ]
# }'