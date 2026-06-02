"""
xianyu_service.py - 闲鱼AI服务接单系统
======================================

闲鱼AI服务自动化工作流：
  1. AI代做PPT（20-200元/单，2-5分钟/单）
  2. AI代写文章（15-50元/单）
  3. AI设计素材（20-80元/单）

核心逻辑：
  - 闲鱼无公开API，采用半自动模式：手动接单 + 自动生成交付
  - 接收客户需求 → AI自动生成 → 质检 → 交付
  - 每单记录到向量记忆，积累服务经验

闲鱼上架建议：
  标题: AI代做PPT/代写/设计 专业高效 24小时交付
  价格: PPT 20-200元, 代写 15-50元, 设计 20-80元
  描述: 突出"AI专业工具 + 人工精修 + 快速交付"
"""

import os
import json
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict


# ===================== 订单数据 =====================

@dataclass
class XianyuOrder:
    """闲鱼订单"""
    id: str = ""
    service_type: str = ""      # ppt / writing / design / resume
    customer_name: str = ""     # 客户昵称
    requirement: str = ""       # 客户需求描述
    price: float = 0.0          # 成交价格
    materials: list = field(default_factory=list)  # 客户提供的素材文件路径
    status: str = "pending"     # pending / generating / reviewing / delivered / paid
    output_path: str = ""       # 生成文件路径
    created_at: str = ""
    delivered_at: str = ""
    notes: str = ""             # 备注

    def to_dict(self):
        return asdict(self)


# ===================== 服务配置 =====================

SERVICE_CONFIGS = {
    "ppt": {
        "name": "AI代做PPT",
        "price_range": "20-200元",
        "default_price": 30,
        "description": "PPT定制设计，支持汇报/答辩/商业/年终总结等",
        "keywords": ["PPT", "幻灯片", "演示文稿", "汇报", "答辩", "年终总结", "商业计划书"],
        "generate_prompt": """你是一个专业PPT内容策划师。根据以下客户需求，生成完整的PPT内容大纲。

客户需求：{requirement}

请输出：
1. PPT标题
2. 页面大纲（每页的标题和要点）
3. 每页的内容建议
4. 推荐的设计风格和配色

格式要求：每个页面用 ===PAGE=== 分隔""",
        "delivery_note": "请查收，如需修改请在24小时内反馈。修改免费一次。",
    },
    "writing": {
        "name": "AI代写文章",
        "price_range": "15-50元",
        "default_price": 20,
        "description": "论文/报告/文案/推文代写，专业高效",
        "keywords": ["代写", "论文", "报告", "文案", "文章", "作文", "演讲稿"],
        "generate_prompt": """你是一个专业的内容写手。根据以下客户需求，撰写高质量的文章。

客户需求：{requirement}

要求：
1. 内容原创，逻辑清晰
2. 符合客户具体要求
3. 字数根据需求调整
4. 语言风格专业但易懂

直接输出文章内容：""",
        "delivery_note": "文章已完成，请查收。如需修改请在24小时内反馈。",
    },
    "design": {
        "name": "AI设计素材",
        "price_range": "20-80元",
        "default_price": 35,
        "description": "海报/LOGO/名片/电商主图设计",
        "keywords": ["设计", "海报", "LOGO", "名片", "主图", "封面", "插画"],
        "generate_prompt": """你是一个专业设计师的AI助手。根据以下客户需求，提供详细的设计方案。

客户需求：{requirement}

请输出：
1. 设计方案描述（布局、元素、配色）
2. 文案建议
3. 推荐的设计工具和步骤
4. 如果可以用代码生成（如SVG），请直接生成""",
        "delivery_note": "设计方案已完成，请查收。如需修改请在24小时内反馈。",
    },
    "resume": {
        "name": "AI简历优化",
        "price_range": "30-100元",
        "default_price": 50,
        "description": "简历排版优化/内容润色/面试辅导",
        "keywords": ["简历", "求职", "面试", "CV", "个人简介"],
        "generate_prompt": """你是一个专业的简历优化顾问。根据以下客户需求，优化简历内容。

客户需求：{requirement}

请输出：
1. 简历结构建议
2. 每个模块的内容优化
3. 用STAR法则重写工作经历
4. 技能和自我评价优化
5. 针对性建议

直接输出优化后的简历内容：""",
        "delivery_note": "简历优化完成，请查收。如需修改请在24小时内反馈。",
    },
}


# ===================== 订单管理 =====================

class XianyuOrderManager:
    """闲鱼订单管理器"""

    def __init__(self, data_dir: str = ""):
        self.data_dir = data_dir or str(Path(__file__).parent / ".xianyu_orders")
        Path(self.data_dir).mkdir(exist_ok=True)
        self.orders_file = Path(self.data_dir) / "orders.json"
        self.orders = self._load()

    def _load(self) -> list:
        if self.orders_file.exists():
            try:
                return json.loads(self.orders_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _save(self):
        self.orders_file.write_text(
            json.dumps(self.orders, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def create_order(self, service_type: str, requirement: str,
                     customer_name: str = "", price: float = 0.0,
                     materials: list = None) -> XianyuOrder:
        """创建新订单"""
        config = SERVICE_CONFIGS.get(service_type, SERVICE_CONFIGS["ppt"])
        order = XianyuOrder(
            id=f"XY{int(time.time())}",
            service_type=service_type,
            customer_name=customer_name or "闲鱼用户",
            requirement=requirement,
            price=price or config["default_price"],
            materials=materials or [],
            status="pending",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.orders.append(order.to_dict())
        self._save()

        # 存入向量记忆
        try:
            from vector_memory import add_memory
            add_memory(
                f"[订单] {service_type}: {requirement[:100]}, 价格:{order.price}元",
                category="order",
                source="xianyu",
                metadata={"order_id": order.id, "price": order.price},
            )
        except Exception:
            pass

        return order

    def get_order(self, order_id: str) -> Optional[dict]:
        """获取订单"""
        for o in self.orders:
            if o["id"] == order_id:
                return o
        return None

    def update_status(self, order_id: str, status: str, **kwargs):
        """更新订单状态"""
        for o in self.orders:
            if o["id"] == order_id:
                o["status"] = status
                if status == "delivered":
                    o["delivered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                o.update(kwargs)
                self._save()
                return o
        return None

    def list_orders(self, status: str = "") -> list:
        """列出订单"""
        if status:
            return [o for o in self.orders if o["status"] == status]
        return self.orders

    def get_stats(self) -> dict:
        """获取统计信息"""
        total = len(self.orders)
        revenue = sum(o.get("price", 0) for o in self.orders if o.get("status") == "paid")
        pending = len([o for o in self.orders if o["status"] == "pending"])
        delivered = len([o for o in self.orders if o["status"] in ("delivered", "paid")])
        return {
            "total": total,
            "revenue": revenue,
            "pending": pending,
            "delivered": delivered,
        }


# ===================== 自动生成 =====================

class AIServiceGenerator:
    """AI服务内容生成器"""

    def __init__(self, brain_api_key: str = "", brain_base_url: str = "",
                 brain_model: str = ""):
        self.api_key = brain_api_key
        self.base_url = brain_base_url
        self.model = brain_model
        self._client = None

    def _get_client(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    def generate(self, service_type: str, requirement: str) -> dict:
        """
        根据服务类型和客户需求生成内容。

        Returns:
            {"success": bool, "content": str, "file_path": str}
        """
        config = SERVICE_CONFIGS.get(service_type)
        if not config:
            return {"success": False, "content": f"未知服务类型: {service_type}"}

        prompt = config["generate_prompt"].format(requirement=requirement)

        try:
            client = self._get_client()
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": (
                        "你是一个专业的AI内容生成助手。"
                        "根据客户需求生成高质量的内容。"
                        "直接输出结果，不要多余的解释。"
                    )},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                timeout=120,
            )
            content = response.choices[0].message.content

            # 保存到文件
            output_dir = Path(__file__).parent / ".xianyu_output"
            output_dir.mkdir(exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            ext = "md" if service_type in ("writing", "resume") else "txt"
            file_path = output_dir / f"{service_type}_{timestamp}.{ext}"
            file_path.write_text(content, encoding="utf-8")

            return {
                "success": True,
                "content": content,
                "file_path": str(file_path),
            }
        except Exception as e:
            return {"success": False, "content": f"生成失败: {e}"}


# ===================== 闲鱼上架指南 =====================

XIANYU_LISTING_GUIDE = """
## 闲鱼AI服务上架指南

### 推荐上架服务（按出单速度排序）

1. **AI代做PPT** (最推荐)
   - 标题: "AI代做PPT 汇报答辩商业总结 专业高效 当天出"
   - 价格: 20元起（基础版）/ 50元（标准版）/ 200元（精装版）
   - 描述: "AI专业工具生成 + 人工精修，2小时内交付。支持汇报/答辩/年终总结/商业计划/课件等。先做后付，满意再确认收货。"
   - 预期日单: 2-5单

2. **AI代写文章** (次推荐)
   - 标题: "AI代写 论文报告文案演讲稿 快速原创"
   - 价格: 15元起（500字）/ 30元（1000字）/ 50元（2000字）
   - 描述: "AI原创生成，查重率低。支持论文/报告/文案/演讲稿/工作总结等。"
   - 预期日单: 3-8单

3. **AI简历优化** (高客单)
   - 标题: "AI简历优化 求职简历精修 STAR法则 专业排版"
   - 价格: 30元起
   - 描述: "针对目标岗位优化简历内容和排版。突出核心竞争力。"
   - 预期日单: 1-3单

### 日赚100+的路径

保守估计（第一天）：
- PPT 2单 x 30元 = 60元
- 代写 3单 x 20元 = 60元
- 合计: 120元/天

关键策略：
- 多上架几个不同类型的服务
- 价格定在市场低位，快速积累评价
- 响应速度要快（5分钟内回复）
- 生成后先自己检查一遍再交付

### 注意事项

1. 不要使用"AI"字眼太明显，容易被平台限流
2. 用"专业工具制作"替代"AI生成"
3. 先做后付，降低客户决策门槛
4. 每单都要截图记录，作为后续营销素材
5. 评价是关键，前10单可以适当降价换取好评
"""


def print_listing_guide():
    """打印上架指南"""
    print(XIANYU_LISTING_GUIDE)


# ===================== CLI 接入点 =====================

def quick_create_order(service_type: str, requirement: str,
                       price: float = 0.0) -> dict:
    """
    快速创建订单并生成内容。
    用于CLI快速接单流程。
    """
    from vector_memory import add_memory
    from dotenv import load_dotenv

    # 加载环境变量
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    # 创建订单
    manager = XianyuOrderManager()
    order = manager.create_order(service_type, requirement, price=price)

    # 生成内容
    generator = AIServiceGenerator(
        brain_api_key=os.environ.get("BRAIN_API_KEY", ""),
        brain_base_url=os.environ.get("BRAIN_BASE_URL", "https://api.deepseek.com/v1"),
        brain_model=os.environ.get("BRAIN_MODEL", "deepseek-chat"),
    )

    print(f"[闲鱼] 订单 {order.id} - {SERVICE_CONFIGS[service_type]['name']}")
    print(f"[闲鱼] 需求: {requirement[:80]}")
    print(f"[闲鱼] 生成中...")

    result = generator.generate(service_type, requirement)

    if result["success"]:
        manager.update_status(order.id, "delivered",
                             output_path=result["file_path"],
                             notes="自动生成完成")
        print(f"[闲鱼] 生成成功! 文件: {result['file_path']}")
        print(f"[闲鱼] 内容预览: {result['content'][:200]}...")

        add_memory(
            f"[交付] 订单{order.id}: {service_type}, 价格{order.price}元, 文件{result['file_path']}",
            category="delivery",
            source="xianyu",
        )
    else:
        manager.update_status(order.id, "generating", notes=result["content"])
        print(f"[闲鱼] 生成失败: {result['content']}")

    return {
        "order": order.to_dict(),
        "result": result,
    }
