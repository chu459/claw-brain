"""
Telegram 通知模块
==================
用于向用户发送验证码请求、系统通知等消息

配置方式：
1. 在 Telegram 中找 @BotFather 创建 Bot，获取 token
2. 在账号管理中添加 Telegram Bot，填入 token 和你的 chat_id
3. 系统检测到需要验证码时，会自动发消息到你的 Telegram
"""

import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


class TelegramNotifier:
    """Telegram 通知发送器"""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """发送消息到 Telegram

        Args:
            text: 消息内容（支持 HTML 格式）
            parse_mode: 解析模式（HTML / Markdown）

        Returns:
            是否发送成功
        """
        url = f"{self.base_url}/sendMessage"
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result.get("ok", False)
        except Exception as e:
            print(f"[TELEGRAM] 发送失败: {e}")
            return False

    def send_captcha_request(self, platform: str, context: str) -> bool:
        """发送验证码请求通知

        Args:
            platform: 平台名称（如 闲鱼、淘宝）
            context: 上下文说明（如 登录验证、修改密码）

        Returns:
            是否发送成功
        """
        text = f"""🚨 <b>验证码请求</b>

📱 <b>平台:</b> {platform}
📝 <b>操作:</b> {context}

请查看手机短信，收到验证码后回复：
<code>/验证码 123456</code>

（将 123456 替换为实际验证码）
"""
        return self.send_message(text)


class TelegramBot:
    """Telegram Bot 管理器 - 支持接收用户回复"""

    def __init__(self, token: str, chat_id: str):
        self.notifier = TelegramNotifier(token, chat_id)
        self.token = token
        self.chat_id = chat_id
        self.last_update_id = 0
        self.pending_code = None

    def get_updates(self, timeout: int = 0) -> list:
        """获取新消息

        Args:
            timeout: 长轮询超时时间（秒）

        Returns:
            消息列表
        """
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {
            "offset": self.last_update_id + 1,
            "timeout": timeout,
        }

        try:
            url_with_params = f"{url}?offset={self.last_update_id + 1}&timeout={timeout}"
            with urllib.request.urlopen(url_with_params, timeout=timeout + 5) as response:
                result = json.loads(response.read().decode("utf-8"))
                if result.get("ok"):
                    updates = result.get("result", [])
                    if updates:
                        self.last_update_id = updates[-1]["update_id"]
                    return updates
        except Exception as e:
            print(f"[TELEGRAM] 获取消息失败: {e}")
        return []

    def check_for_code(self) -> Optional[str]:
        """检查是否收到验证码回复

        Returns:
            验证码（如果收到），否则 None
        """
        updates = self.get_updates(timeout=0)
        for update in updates:
            message = update.get("message", {})
            text = message.get("text", "")

            # 支持格式：/验证码 123456 或直接 123456
            if text.startswith("/验证码") or text.startswith("/code"):
                parts = text.split()
                if len(parts) >= 2:
                    code = parts[1].strip()
                    if code.isdigit():
                        self.pending_code = code
                        self.notifier.send_message(f"✅ 已收到验证码: <code>{code}</code>")
                        return code

            # 如果是纯数字（6位以内），也可能是验证码
            elif text.strip().isdigit() and len(text.strip()) <= 6:
                code = text.strip()
                self.pending_code = code
                self.notifier.send_message(f"✅ 已收到验证码: <code>{code}</code>")
                return code

        return self.pending_code


def get_telegram_notifier() -> Optional[TelegramNotifier]:
    """从账号管理中获取 Telegram 配置

    Returns:
        TelegramNotifier 实例（如果已配置），否则 None
    """
    try:
        # 尝试从 credential_store 获取
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from credential_store import get_account

        account = get_account("telegram-bot")
        if account:
            token = None
            chat_id = None
            for field in account.get("fields", []):
                if field.get("key") == "token":
                    token = field.get("value")
                elif field.get("key") == "chat_id":
                    chat_id = field.get("value")

            if token and chat_id:
                return TelegramNotifier(token, chat_id)
    except Exception as e:
        print(f"[TELEGRAM] 加载配置失败: {e}")

    return None


def get_telegram_bot() -> Optional[TelegramBot]:
    """获取 Telegram Bot 实例（支持双向通信）

    Returns:
        TelegramBot 实例（如果已配置），否则 None
    """
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from credential_store import get_account

        account = get_account("telegram-bot")
        if account:
            token = None
            chat_id = None
            for field in account.get("fields", []):
                if field.get("key") == "token":
                    token = field.get("value")
                elif field.get("key") == "chat_id":
                    chat_id = field.get("value")

            if token and chat_id:
                return TelegramBot(token, chat_id)
    except Exception as e:
        print(f"[TELEGRAM] 加载配置失败: {e}")

    return None


# 便捷函数
def notify_captcha_needed(platform: str, context: str) -> bool:
    """通知用户需要输入验证码

    Args:
        platform: 平台名称
        context: 上下文说明

    Returns:
        是否通知成功
    """
    notifier = get_telegram_notifier()
    if notifier:
        return notifier.send_captcha_request(platform, context)
    return False


def check_captcha_reply() -> Optional[str]:
    """检查用户是否回复了验证码

    Returns:
        验证码（如果收到），否则 None
    """
    bot = get_telegram_bot()
    if bot:
        return bot.check_for_code()
    return None
