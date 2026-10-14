#!/bin/bash

# 前端产物构建
npm --prefix frontend install
npm --prefix frontend run build 

# 后端启动服务
python3 backend/api.py --host 0.0.0.0 --port 8000
