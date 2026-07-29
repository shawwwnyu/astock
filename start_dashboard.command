#!/bin/zsh
cd "/Users/xiaoyuxing/Documents/stock" || exit 1
exec python3 server.py --host 127.0.0.1 --port 8765
