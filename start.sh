#!/bin/bash

cd /home/luke/helei/XYBotV2
if [ `ps aux|grep redis-server|grep luke` == 1 ];then
  nohup redis-server --protected-mode no>/dev/null 2>&1 &
fi

nohup python main.py > main2.log 2>&1 &
sudo iptables -t nat -A OUTPUT -p tcp --dport 8080 -j REDIRECT --to-ports 80

service nginx stop
docker start 9c2f9993f3ec