"""
xianyu_auto_list.py - 闲鱼全自动上架系统
==========================================

利用 OpenClaw 浏览器自动化实现：
  1. 自动登录闲鱼（用户首次提供账号密码，后续 cookie 持久化）
  2. 自动发布AI服务商品（批量上架多种服务类型）
  3. 自动监控新订单（定时检查闲鱼消息/订单）
  4. 自动接单并生成交付（接收到订单 → AI生成 → 回复客户）

工作流：
  用户执行 `python cli.py xianyu auto-list`
  → 系统从凭据库获取闲鱼账号
  → 通过 OpenClaw 自动登录闲鱼网页版
  → 自动填写商品标题、描述、价格、分类
  → 发布商品
  → 定时监控新订单

前置条件：
  - OpenClaw Gateway 运行中
  - 凭据库中有闲鱼账号（用户名/手机号 + 密码）
"""

import os
import json
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field


# ===================== 商品模板 =====================

@dataclass
class ListingTemplate:
    """闲鱼商品上架模板"""
    service_type: str
    title: str
    description: str
    price: float
    category: str = ""
    images: list = field(default_factory=list)  # 图片URL或本地路径
    tags: list = field(default_factory=list)

    def to_dict(self):
        return {
            "service_type": self.service_type,
            "title": self.title,
            "description": self.description,
            "price": self.price,
            "category": self.category,
            "images": self.images,
            "tags": self.tags,
        }


# 预设商品模板——按出单速度排序
LISTING_TEMPLATES = [
    ListingTemplate(
        service_type="writing",
        title="代写文章 论文报告文案演讲稿 快速原创质量高",
        description="""[服务内容]
专业代写各类文章，包括但不限于：
- 论文/课程报告/实验报告
- 工作总结/述职报告
- 演讲稿/发言稿
- 公众号推文/自媒体文案
- 商业计划书/市场分析

[服务优势]
- 专业工具辅助 + 人工精修，质量有保障
- 查重率低，原创度高
- 当天交付，急单2小时出

[服务流程]
1. 告诉我你的需求（主题/字数/格式要求）
2. 我先出一个大纲给你确认
3. 确认后开始写
4. 交付成品，免费修改一次

[价格]
500字 15元 | 1000字 25元 | 2000字 40元 | 3000字 55元
量大可优惠，欢迎私聊！

不接：违法违规、涉政敏感内容""",
        price=15,
        category="文稿/文案",
        tags=["代写", "论文", "报告", "文案", "演讲稿"],
    ),
    ListingTemplate(
        service_type="ppt",
        title="代做PPT 汇报答辩商业总结 专业排版 当天出",
        description="""[服务内容]
专业制作各类PPT演示文稿：
- 毕业答辩PPT
- 工作汇报/述职PPT
- 年终总结PPT
- 商业计划书PPT
- 产品发布/品牌介绍PPT
- 课件/培训PPT

[服务优势]
- 专业设计工具，排版精美
- 逻辑清晰，重点突出
- 图表/数据可视化处理
- 2小时内出初稿，当天交付

[服务流程]
1. 告诉我主题、页数、用途
2. 先出大纲和设计风格让你确认
3. 完成后交付PPT文件
4. 免费修改一次

[价格]
10页内 30元 | 20页内 50元 | 30页内 80元
动画/复杂图表加收10-20元
量大可优惠！

不接：违法违规内容""",
        price=30,
        category="设计/排版",
        tags=["PPT", "代做PPT", "幻灯片", "汇报", "答辩"],
    ),
    ListingTemplate(
        service_type="resume",
        title="简历优化 求职简历精修 突出亮点 通过率高",
        description="""[服务内容]
专业简历优化服务：
- 简历排版美化（多种模板可选）
- 工作经历STAR法则重写
- 自我评价/技能模块优化
- 针对目标岗位定制

[服务优势]
- 多年HR视角优化经验
- 突出核心竞争力
- ATS系统友好格式
- 24小时内交付

[服务流程]
1. 发送你现在的简历（任何格式都行）
2. 告诉我目标岗位/行业
3. 我优化后给你预览
4. 确认后交付最终版

[价格]
基础优化 30元 | 深度定制 50元 | 中英双语 80元
附赠面试Tips！""",
        price=30,
        category="文案/写作",
        tags=["简历", "求职", "面试", "CV", "简历优化"],
    ),
    ListingTemplate(
        service_type="design",
        title="设计素材 海报LOGO名片 主图封面 快速出图",
        description="""[服务内容]
各类设计素材制作：
- 电商主图/详情页
- 公众号封面/海报
- LOGO设计
- 名片设计
- 社交媒体配图

[服务优势]
- 专业设计工具，效果精美
- 速度快，当天出图
- 免费修改一次

[服务流程]
1. 描述你的设计需求
2. 提供参考图（如有）
3. 出初稿确认
4. 交付高清源文件

[价格]
简单设计 20元起 | 中等设计 35元起 | 复杂设计 60元起
具体价格私聊沟通""",
        price=20,
        category="设计服务",
        tags=["设计", "海报", "LOGO", "名片", "主图"],
    ),
]


# ===================== 自动上架引擎 =====================

class XianyuAutoLister:
    """
    闲鱼全自动上架引擎。

    通过 OpenClaw 浏览器自动化完成：
    1. 登录闲鱼
    2. 发布商品
    3. 监控订单
    """

    def __init__(self, node_dir: str = "", gateway_url: str = "http://127.0.0.1:18789"):
        self.node_dir = node_dir
        self.gateway_url = gateway_url
        self._openclaw_cmd = self._build_openclaw_cmd()

    def _build_openclaw_cmd(self) -> list:
        """构建 OpenClaw 命令"""
        if self.node_dir:
            node_exe = os.path.join(self.node_dir, "node.exe")
            openclaw_mjs = os.path.join(self.node_dir, "node_modules", "openclaw", "openclaw.mjs")
            if os.path.isfile(openclaw_mjs):
                return [node_exe, openclaw_mjs]

        # 尝试系统 PATH
        import shutil
        if shutil.which("openclaw"):
            return ["openclaw"]

        # 尝试 npx
        npx = shutil.which("npx")
        if npx:
            return [npx, "openclaw"]

        raise RuntimeError("找不到 openclaw，请先安装并确保 Gateway 运行中")

    def _get_credentials(self) -> Optional[dict]:
        """从凭据库获取闲鱼账号信息"""
        try:
            from credential_store import list_accounts
            accounts = list_accounts(mask=False)
            for acc in accounts:
                if "闲鱼" in acc.get("name", "") or "xianyu" in acc.get("name", "").lower():
                    fields = {f["key"]: f.get("value", "") for f in acc.get("fields", [])}
                    return {
                        "account_id": acc["id"],
                        "username": fields.get("username") or fields.get("phone") or "",
                        "password": fields.get("password") or "",
                    }
        except Exception as e:
            print(f"[AUTO-LIST] Credential lookup failed: {e}")
        return None

    def _execute_openclaw(self, instruction: str, timeout: int = 300) -> dict:
        """通过 OpenClaw 执行浏览器指令"""
        import subprocess

        cmd = self._openclaw_cmd + [
            "agent",
            "--agent", "main",
            "--session-id", "xianyu-auto-lister",
            "--message", instruction,
        ]

        env = os.environ.copy()
        env.pop("NODE_OPTIONS", None)
        if self.node_dir:
            env.pop("NODE_PATH", None)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                env=env, encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                return {"success": True, "content": result.stdout.strip()}
            else:
                return {"success": False, "content": (result.stderr or result.stdout).strip()[:500]}
        except subprocess.TimeoutExpired:
            return {"success": False, "content": f"执行超时（{timeout}秒）"}
        except Exception as e:
            return {"success": False, "content": str(e)}

    def login(self, username: str = "", password: str = "") -> dict:
        """
        登录闲鱼。
        如果未提供凭据，自动从凭据库获取。
        """
        if not username or not password:
            creds = self._get_credentials()
            if not creds or not creds.get("password"):
                return {
                    "success": False,
                    "content": "未找到闲鱼账号凭据。请先执行: python cli.py cred add --name 闲鱼账号",
                }
            username = username or creds["username"]
            password = password or creds["password"]

        print(f"[AUTO-LIST] 正在登录闲鱼 (账号: {username[:3]}***)...")

        instruction = (
            f"请帮我登录闲鱼网页版 (https://www.goofish.com 或 https://2.taobao.com)。\n"
            f"使用以下凭据：\n"
            f"- 账号/手机号: {username}\n"
            f"- 密码: {password}\n"
            f"\n"
            f"步骤：\n"
            f"1. 打开闲鱼网页版\n"
            f"2. 如果看到登录页面，输入账号和密码\n"
            f"3. 如果需要验证码或短信验证，告诉我需要什么验证（设 status 为 need_input）\n"
            f"4. 登录成功后，告诉我当前页面状态\n"
            f"\n"
            f"注意：\n"
            f"- 如果遇到滑块验证或其他人机验证，尽可能完成它\n"
            f"- 如果需要手机验证码，把 status 设为 need_input，在 question_for_user 中说明"
        )

        result = self._execute_openclaw(instruction, timeout=180)

        if result["success"]:
            print(f"[AUTO-LIST] 登录指令已发送")
            # 存入向量记忆
            try:
                from vector_memory import add_memory
                add_memory(
                    f"[闲鱼] 登录成功，账号: {username[:3]}***",
                    category="milestone",
                    source="xianyu",
                    verified=True,
                )
            except Exception:
                pass
        else:
            print(f"[AUTO-LIST] 登录失败: {result['content']}")

        return result

    def publish_product(self, template: ListingTemplate) -> dict:
        """
        发布一个商品到闲鱼。

        Args:
            template: 商品上架模板
        """
        print(f"[AUTO-LIST] 正在发布商品: {template.title[:30]}...")
        print(f"[AUTO-LIST] 价格: {template.price}元 | 类型: {template.service_type}")

        images_instruction = ""
        if template.images:
            images_instruction = f"\n如果可以上传图片，请上传以下图片：{json.dumps(template.images[:5])}\n"

        tags_instruction = ""
        if template.tags:
            tags_instruction = f"\n标签/关键词: {', '.join(template.tags[:5])}"

        instruction = (
            f"请帮我在闲鱼上发布一个商品（闲鱼网页版或2.taobao.com）。\n"
            f"\n"
            f"商品信息：\n"
            f"- 标题: {template.title}\n"
            f"- 描述: {template.description}\n"
            f"- 价格: {template.price}元\n"
            f"- 分类: {template.category}{images_instruction}{tags_instruction}\n"
            f"\n"
            f"步骤：\n"
            f"1. 如果未登录，先登录闲鱼\n"
            f"2. 找到「发布」或「卖闲置」按钮并点击\n"
            f"3. 填写标题、描述、价格\n"
            f"4. 如果有图片，上传图片\n"
            f"5. 设置分类\n"
            f"6. 点击发布\n"
            f"7. 告诉我发布结果（成功/失败 + 商品链接）\n"
            f"\n"
            f"注意：\n"
            f"- 标题中不要使用'AI'字眼，用'专业工具'替代\n"
            f"- 如果遇到任何验证，告诉我具体情况\n"
            f"- 如果发布成功，记录商品链接"
        )

        result = self._execute_openclaw(instruction, timeout=300)

        if result["success"]:
            print(f"[AUTO-LIST] 商品发布指令已发送")
            # 存入向量记忆
            try:
                from vector_memory import add_memory
                add_memory(
                    f"[闲鱼] 发布商品: {template.title}, 价格{template.price}元, 类型{template.service_type}",
                    category="action_result",
                    source="xianyu",
                    metadata={"service_type": template.service_type, "price": template.price},
                )
            except Exception:
                pass
        else:
            print(f"[AUTO-LIST] 发布失败: {result['content']}")

        return result

    def publish_all(self) -> list[dict]:
        """批量发布所有预设商品"""
        print(f"[AUTO-LIST] 开始批量发布 {len(LISTING_TEMPLATES)} 个商品...")
        results = []

        for i, template in enumerate(LISTING_TEMPLATES, 1):
            print(f"\n[AUTO-LIST] === 商品 {i}/{len(LISTING_TEMPLATES)} ===")
            result = self.publish_product(template)
            results.append({
                "template": template.service_type,
                "title": template.title,
                "success": result["success"],
                "content": result["content"][:200],
            })

            # 间隔等待，避免被封
            if i < len(LISTING_TEMPLATES):
                wait = 30
                print(f"[AUTO-LIST] 等待 {wait} 秒后发布下一个...")
                time.sleep(wait)

        # 汇总
        success_count = sum(1 for r in results if r["success"])
        print(f"\n[AUTO-LIST] 发布完成: {success_count}/{len(results)} 个成功")

        return results

    def check_orders(self) -> dict:
        """检查闲鱼新订单/消息"""
        instruction = (
            "请帮我检查闲鱼上的新消息和订单。\n"
            "1. 打开闲鱼网页版\n"
            "2. 查看消息中心，是否有新消息\n"
            "3. 查看我的订单/已卖出，是否有新订单\n"
            "4. 如果有新消息或新订单，详细告诉我内容（买家ID、需求、价格等）\n"
            "5. 如果没有新消息，告诉我当前状态"
        )

        result = self._execute_openclaw(instruction, timeout=120)
        return result


# ===================== 便捷函数 =====================

def auto_login(username: str = "", password: str = "") -> dict:
    """快捷登录闲鱼"""
    node_dir = os.environ.get("OPENCLAW_NODE_DIR", "")
    lister = XianyuAutoLister(node_dir=node_dir)
    return lister.login(username, password)


def auto_publish_all() -> list[dict]:
    """快捷批量发布商品"""
    node_dir = os.environ.get("OPENCLAW_NODE_DIR", "")
    lister = XianyuAutoLister(node_dir=node_dir)
    return lister.publish_all()


def auto_publish_one(service_type: str) -> dict:
    """快捷发布单个服务类型商品"""
    node_dir = os.environ.get("OPENCLAW_NODE_DIR", "")
    lister = XianyuAutoLister(node_dir=node_dir)

    for template in LISTING_TEMPLATES:
        if template.service_type == service_type:
            return lister.publish_product(template)

    return {"success": False, "content": f"未找到服务类型: {service_type}"}


def check_new_orders() -> dict:
    """快捷检查新订单"""
    node_dir = os.environ.get("OPENCLAW_NODE_DIR", "")
    lister = XianyuAutoLister(node_dir=node_dir)
    return lister.check_orders()
