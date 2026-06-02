"""
claw-brain 凭据管理系统

本地加密存储用户账号信息，保证隐私不被泄露。
- 凭据文件使用 Fernet 对称加密，密钥独立存储
- 仅在本机可用，复制凭据文件到其他机器无法解密
- Web UI 提供 CRUD 操作，敏感信息自动脱敏显示
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

# 凭据存储目录（项目根目录下 .credentials/）
CRED_DIR = Path(__file__).parent / ".credentials"
CRED_FILE = CRED_DIR / "vault.enc"
KEY_FILE = CRED_DIR / ".keyfile"

# 内置账号分类模板
ACCOUNT_TEMPLATES = {
    "ai_api": {
        "label": "AI 接口",
        "icon": "🤖",
        "desc": "如 DeepSeek、OpenAI、302.ai 等 AI 服务的密钥",
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "password"},
            {"key": "base_url", "label": "接口地址 (Base URL)", "type": "text"},
            {"key": "model", "label": "默认模型", "type": "text"},
            {"key": "available_models", "label": "可用模型列表", "type": "text", "placeholder": "如: gpt-image-1, gpt-4o, dall-e-3（逗号分隔）"},
        ],
    },
    "payment": {
        "label": "收款平台",
        "icon": "💳",
        "desc": "如 Lemonsqueezy、Gumroad 等收款账号",
        "fields": [
            {"key": "email", "label": "注册邮箱", "type": "text"},
            {"key": "phone", "label": "手机号", "type": "text"},
            {"key": "api_key", "label": "API Key", "type": "password"},
            {"key": "store_url", "label": "店铺链接", "type": "text"},
        ],
    },
    "social_media": {
        "label": "社交媒体",
        "icon": "📱",
        "desc": "如 X(Twitter)、LinkedIn、Reddit 等平台账号",
        "fields": [
            {"key": "username", "label": "用户名/ID", "type": "text"},
            {"key": "password", "label": "登录密码", "type": "password"},
            {"key": "phone", "label": "绑定手机号", "type": "text"},
            {"key": "email", "label": "绑定邮箱", "type": "text"},
            {"key": "api_token", "label": "API Token", "type": "password"},
        ],
    },
    "dev_platform": {
        "label": "开发平台",
        "icon": "💻",
        "desc": "如 GitHub、Vercel、Cloudflare 等开发者平台",
        "fields": [
            {"key": "username", "label": "用户名", "type": "text"},
            {"key": "password", "label": "密码/Token", "type": "password"},
            {"key": "email", "label": "注册邮箱", "type": "text"},
            {"key": "phone", "label": "绑定手机号", "type": "text"},
        ],
    },
    "custom": {
        "label": "其他",
        "icon": "🔧",
        "desc": "自定义账号信息",
        "fields": [
            {"key": "account", "label": "账号", "type": "text"},
            {"key": "password", "label": "密码", "type": "password"},
        ],
    },
}

# 可选的常用字段（用于"添加更多字段"时的快捷选项）
PRESET_FIELDS = [
    {"key": "email", "label": "邮箱", "type": "text"},
    {"key": "phone", "label": "手机号", "type": "text"},
    {"key": "password", "label": "密码", "type": "password"},
    {"key": "api_key", "label": "API Key", "type": "password"},
    {"key": "username", "label": "用户名", "type": "text"},
    {"key": "url", "label": "网址/链接", "type": "text"},
    {"key": "token", "label": "Token", "type": "password"},
]


def _get_or_create_key() -> bytes:
    """获取或创建加密密钥。密钥独立存储，与凭据文件分离。"""
    CRED_DIR.mkdir(exist_ok=True)
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    return key


def _fernet() -> Fernet:
    return Fernet(_get_or_create_key())


def _load_raw() -> dict:
    """加载并解密凭据数据。"""
    if not CRED_FILE.exists():
        return {"accounts": {}}
    encrypted = CRED_FILE.read_bytes()
    if not encrypted:
        return {"accounts": {}}
    try:
        decrypted = _fernet().decrypt(encrypted)
        return json.loads(decrypted)
    except Exception:
        # 解密失败（密钥不匹配），返回空
        return {"accounts": {}}


def _save_raw(data: dict):
    """加密并保存凭据数据。"""
    CRED_DIR.mkdir(exist_ok=True)
    encrypted = _fernet().encrypt(json.dumps(data, ensure_ascii=False).encode())
    CRED_FILE.write_bytes(encrypted)


def mask_value(value: str) -> str:
    """脱敏显示：只显示首尾字符。"""
    if not value or len(value) <= 6:
        return "****"
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


# ===================== CRUD API =====================

def list_accounts(mask: bool = True) -> list:
    """
    列出所有已存储的账号。
    mask=True 时敏感字段值会脱敏显示。
    """
    data = _load_raw()
    result = []
    for account_id, account in data.get("accounts", {}).items():
        entry = {
            "id": account_id,
            "name": account.get("name", ""),
            "category": account.get("category", "custom"),
            "created_at": account.get("created_at", ""),
            "updated_at": account.get("updated_at", ""),
        }
        if mask:
            entry["fields"] = [
                {
                    "key": f.get("key", ""),
                    "label": f.get("label", ""),
                    "type": f.get("type", "text"),
                    "value": mask_value(f.get("value", "")),
                    "has_value": bool(f.get("value", "")),
                }
                for f in account.get("fields", [])
            ]
        else:
            entry["fields"] = [
                {
                    **f,
                    "has_value": bool(f.get("value", "")),
                }
                for f in account.get("fields", [])
            ]
        result.append(entry)
    # 按更新时间倒序
    result.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return result


def get_account(account_id: str) -> Optional[dict]:
    """获取单个账号的完整信息（不脱敏）。"""
    data = _load_raw()
    account = data.get("accounts", {}).get(account_id)
    if not account:
        return None
    return {
        "id": account_id,
        "name": account.get("name", ""),
        "category": account.get("category", "custom"),
        "fields": account.get("fields", []),
        "created_at": account.get("created_at", ""),
        "updated_at": account.get("updated_at", ""),
    }


def add_account(name: str, category: str, fields: list) -> dict:
    """
    添加新账号。
    fields: [{"key": "api_key", "label": "API Key", "value": "sk-xxx", "type": "password"}, ...]
    返回新创建的账号信息。
    """
    data = _load_raw()
    account_id = str(uuid.uuid4())[:8]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    data["accounts"][account_id] = {
        "name": name,
        "category": category,
        "fields": fields,
        "created_at": now,
        "updated_at": now,
    }
    _save_raw(data)
    return get_account(account_id)


def update_account(account_id: str, name: str = None, category: str = None, fields: list = None) -> Optional[dict]:
    """更新已有账号。"""
    data = _load_raw()
    account = data.get("accounts", {}).get(account_id)
    if not account:
        return None
    if name is not None:
        account["name"] = name
    if category is not None:
        account["category"] = category
    if fields is not None:
        account["fields"] = fields
    account["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_raw(data)
    return get_account(account_id)


def delete_account(account_id: str) -> bool:
    """删除账号。"""
    data = _load_raw()
    if account_id not in data.get("accounts", {}):
        return False
    del data["accounts"][account_id]
    _save_raw(data)
    return True


def get_credential_value(account_name: str, field_key: str) -> Optional[str]:
    """
    按账号名称和字段名获取凭据值。
    用于系统自动加载：get_credential_value("DeepSeek", "api_key") -> "sk-xxx"
    """
    data = _load_raw()
    for account_id, account in data.get("accounts", {}).items():
        if account.get("name", "").lower() == account_name.lower():
            for f in account.get("fields", []):
                if f.get("key", "") == field_key:
                    return f.get("value", "")
    return None


def get_all_credentials() -> dict:
    """
    获取所有凭据的扁平映射，格式: {"account_name.field_key": "value", ...}
    用于系统启动时批量加载环境变量。
    """
    data = _load_raw()
    result = {}
    for account_id, account in data.get("accounts", {}).items():
        name = account.get("name", "")
        for f in account.get("fields", []):
            key = f"{name}.{f.get('key', '')}"
            result[key] = f.get("value", "")
    return result
