# -*- coding: utf-8 -*-
# @Time        : 2025/4/10
# @Author      : helei
# @File        : test_sing.py
# @Description :
url1 = "https://www.hhlqilongzhu.cn/api/changya.php"     # 随心唱
url = "https://www.hhlqilongzhu.cn/api/ximalaya/ximalaya_duanzi.php"  # 段子

url = "https://www.hhlqilongzhu.cn/api/wangyi_hot_review.php"   # 网易云随机热门

import requests

resp = requests.get(url1)
print(resp.json())