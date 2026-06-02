#!/usr/bin/env python3
"""启动 Claw-brain Web 控制台（绕开 __main__ 的 uvicorn.run 递归问题）"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from web_console import app

uvicorn.run(app, host="127.0.0.1", port=7860, log_level="warning")
