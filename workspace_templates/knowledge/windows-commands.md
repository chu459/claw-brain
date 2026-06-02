---
title: Windows 命令参考
description: Windows cmd.exe 下可用的命令及Linux命令的替代方案
tags: windows, 命令, 平台
priority: high
---

## 平台约束

当前运行在 **Windows cmd.exe**，不是 Linux。以下规则必须遵守：

### 命令串联
- 多命令串联用 `&` 或 `;`，不要用 `&&`（`&&` 会在前一个命令失败时中断后续命令）
- 例: `cd /d "路径" & python script.py`

### 禁止的 Linux 命令及替代方案

| Linux 命令 | Windows 替代 | 说明 |
|-----------|-------------|------|
| `head -N` | `powershell -c "Get-Content file.txt | Select -First N"` | 取前N行 |
| `tail -N` | `powershell -c "Get-Content file.txt | Select -Last N"` | 取后N行 |
| `grep pattern` | `findstr "pattern"` | 文本搜索 |
| `curl URL` | `python -c "import requests; print(requests.get('URL').text)"` | HTTP请求 |
| `awk` | 用 Python 脚本替代 | 文本处理 |
| `sed` | 用 Python 脚本替代 | 文本替换 |
| `wc -l` | `find /c /v ""` | 行数统计 |
| `cat` | `type` | 查看文件 |
| `touch` | `type nul > filename` | 创建空文件 |

### 网络检测
- 用 `ping -n 1 baidu.com`，不要用 curl
- 用 `nslookup baidu.com` 做 DNS 检测

### Python 脚本执行
- 路径用正斜杠: `python workspace/tools_out/xxx.py`
- pip 安装: `python -m pip install 包名`
- 需要在项目根目录 `claw-brain-main` 下执行
