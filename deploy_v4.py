"""
Claw-brain v4 一键部署到 AutoDL
===============================
自动上传训练数据和脚本 → 安装依赖 → 启动训练

使用方法:
1. 确保 AutoDL SSH 可连接
2. python deploy_v4.py

SSH 配置:
  主机: connect.bjb1.seetacloud.com
  端口: 48216
  用户: root
"""

import paramiko
import os
import sys
import time
from pathlib import Path

# ============ 配置 ============
SSH_HOST = "connect.bjb1.seetacloud.com"
SSH_PORT = 48216
SSH_USER = "root"
SSH_PASSWORD = ""  # 留空则用密钥或交互输入
LOCAL_BACKUP_DIR = r"C:\Users\楚\claw-brain-models\claw_brain_merged_v4"

# 本地文件路径（训练数据和脚本）
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_TO_UPLOAD = [
    ("training_data_v4.jsonl", "/root/training_data_v4.jsonl"),
    ("finetune_v4.py", "/root/finetune_v4.py"),
    ("model_api_v4.py", "/root/model_api_v4.py"),
]

# AutoDL 上执行的命令（分步骤）
REMOTE_COMMANDS = [
    # 1. 安装依赖
    "pip install unsloth transformers datasets trl peft bitsandbytes accelerate -q",
    # 2. 启动训练
    "cd /root && python finetune_v4.py",
    # 3. 训练完成后启动 OpenAI 兼容 API
    "cd /root && nohup python model_api_v4.py > /root/model_api_v4.log 2>&1 &",
]


def upload_files(ssh: paramiko.SSHClient):
    """上传训练数据和脚本到 AutoDL"""
    sftp = ssh.open_sftp()
    uploaded = []
    for local_name, remote_path in FILES_TO_UPLOAD:
        local_path = os.path.join(LOCAL_DIR, local_name)
        if not os.path.exists(local_path):
            print(f"  跳过: {local_name} (不存在)")
            continue
        print(f"  上传: {local_name} → {remote_path}")
        sftp.put(local_path, remote_path)
        uploaded.append(local_name)
    sftp.close()
    print(f"\n已上传 {len(uploaded)} 个文件")
    return uploaded


def sftp_exists(sftp, path: str) -> bool:
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def download_dir(ssh: paramiko.SSHClient, remote_dir: str, local_dir: str):
    """把训练好的合并模型下载到本机长期保存。"""
    sftp = ssh.open_sftp()
    local_root = Path(local_dir)
    local_root.mkdir(parents=True, exist_ok=True)

    if not sftp_exists(sftp, remote_dir):
        print(f"远程模型目录不存在，跳过本地备份: {remote_dir}")
        sftp.close()
        return False

    def walk(remote_path: str, local_path: Path):
        local_path.mkdir(parents=True, exist_ok=True)
        for item in sftp.listdir_attr(remote_path):
            remote_child = remote_path.rstrip("/") + "/" + item.filename
            local_child = local_path / item.filename
            if item.st_mode & 0o040000:
                walk(remote_child, local_child)
            else:
                print(f"  下载: {remote_child} -> {local_child}")
                sftp.get(remote_child, str(local_child))

    print(f"\n开始把模型备份到本机: {local_root}")
    walk(remote_dir, local_root)
    sftp.close()
    print("本地模型备份完成。")
    return True


def run_command(ssh: paramiko.SSHClient, cmd: str, timeout: int = 3600):
    """执行远程命令并实时输出"""
    print(f"\n>>> {cmd[:100]}...")
    transport = ssh.get_transport()
    channel = transport.open_session()
    channel.settimeout(timeout)
    channel.exec_command(cmd)

    while True:
        if channel.recv_ready():
            data = channel.recv(4096).decode('utf-8', errors='replace')
            print(data, end='', flush=True)
        if channel.recv_stderr_ready():
            data = channel.recv_stderr(4096).decode('utf-8', errors='replace')
            print(data, end='', flush=True)
        if channel.exit_status_ready():
            break
        time.sleep(0.1)

    exit_code = channel.recv_exit_status()
    print(f"\n>>> 命令完成 (退出码: {exit_code})")
    return exit_code


def main():
    print("=" * 60)
    print("Claw-brain v4 一键部署到 AutoDL")
    print(f"目标: {SSH_USER}@{SSH_HOST}:{SSH_PORT}")
    print("=" * 60)

    # 1. 连接 SSH
    print("\n连接 AutoDL...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if SSH_PASSWORD:
        ssh.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASSWORD, timeout=20, banner_timeout=20)
    else:
        # 尝试密钥或交互
        try:
            ssh.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, timeout=20, banner_timeout=20)
        except paramiko.AuthenticationException:
            password = input("请输入 AutoDL 密码: ")
            ssh.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=password, timeout=20, banner_timeout=20)

    print("连接成功!")

    # 2. 上传文件
    print("\n上传训练文件...")
    upload_files(ssh)

    # 3. 执行远程命令
    print("\n安装依赖...")
    run_command(ssh, REMOTE_COMMANDS[0], timeout=600)

    print("\n启动训练（预计 30-50 分钟）...")
    exit_code = run_command(ssh, REMOTE_COMMANDS[1], timeout=7200)

    if exit_code == 0:
        print("\n启动 API 服务...")
        run_command(ssh, REMOTE_COMMANDS[2], timeout=30)

        print("\n检查 API 服务...")
        run_command(ssh, "sleep 8 && curl -s http://127.0.0.1:8000/v1/models || true", timeout=30)

        download_dir(
            ssh,
            "/root/autodl-tmp/claw_brain_merged_v4",
            LOCAL_BACKUP_DIR,
        )

        print("\n" + "=" * 60)
        print("训练完成!")
        print("模型保存在: /root/autodl-tmp/claw_brain_merged_v4/")
        print(f"本机备份在: {LOCAL_BACKUP_DIR}")
        print("\n启动API服务:")
        print("  python /root/model_api_v4.py")
        print("\n本地SSH隧道:")
        print(f"  ssh -L 8001:localhost:8000 {SSH_USER}@{SSH_HOST} -p {SSH_PORT}")
        print("=" * 60)
    else:
        print(f"\n训练异常退出 (code={exit_code})，请检查日志")

    ssh.close()


if __name__ == "__main__":
    try:
        import paramiko
    except ImportError:
        print("需要安装 paramiko: pip install paramiko")
        sys.exit(1)
    main()
