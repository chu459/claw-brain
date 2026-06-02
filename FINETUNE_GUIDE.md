# Claw-brain 微调训练指南

## 概览

| 项目 | 说明 |
|------|------|
| 基座模型 | Qwen2.5-7B-Instruct |
| 训练方法 | QLoRA (4-bit量化 + LoRA) |
| 训练数据 | 158条基础 + 持续积累的对话数据 |
| 云平台 | AutoDL |
| GPU | L4 24GB |
| 预计费用 | ¥3-5 |
| 预计时间 | 2-3小时 |
| 输出 | 微调后的LoRA权重 / 合并后的完整模型 |

---

## 第一步：注册 AutoDL

1. 打开 https://www.autodl.com
2. 注册账号，充值 ¥10（够训练2-3次）
3. 选择「创建实例」
4. 选择 GPU：**L4 (24GB)**，价格约 ¥1.68/小时
5. 镜像选择：**PyTorch 2.0 + Python 3.10**
6. 创建实例

## 第二步：上传文件

通过 AutoDL 的 JupyterLab 界面上传以下文件到 `/root/`：

1. `training_data_final.jsonl` — 最终训练数据（基础+持续积累）
2. `finetune_on_autodl.py` — 微调脚本

或者用命令行：
```bash
# 在 AutoDL 终端执行
cd /root
# 用 scp 或 AutoDL 文件管理器上传文件
```

## 第三步：安装依赖

在 AutoDL 终端执行：
```bash
pip install unsloth transformers datasets trl peft bitsandbytes accelerate
```

## 第四步：开始训练

```bash
cd /root
python finetune_on_autodl.py
```

等待 2-3 小时。

## 第五步：下载模型

训练完成后，有两种选择：

### 选项A：下载 LoRA 权重（小文件，~100MB）
- 位置：`/root/claw_brain_lora/`
- 用 AutoDL 文件管理器下载

### 选项B：下载合并后的完整模型（大文件，~15GB）
- 位置：`/root/claw_brain_merged/`
- 需要较多下载时间

**推荐选项A**：只下载 LoRA 权重，在推理时动态加载。

## 第六步：部署到 Brain 系统

### 方案1：部署到免费推理平台

将合并后的模型上传到：
- **SiliconFlow** (免费额度，支持自定义模型)
- **Together AI** (有免费额度)
- **Replicate** (按量计费，便宜)

然后用 API 调用替代 DeepSeek。

### 方案2：部署到自己的服务器

需要一台有 GPU 的服务器（AutoDL 可以按量续费，¥1.68/h）：
```bash
# 安装 vLLM
pip install vllm

# 启动推理服务（兼容 OpenAI API 格式）
python -m vllm.entrypoints.openai.api_server \
    --model /root/claw_brain_merged \
    --host 0.0.0.0 \
    --port 8000
```

然后在 Brain 系统中把 `brain_base_url` 改成这个地址。

### 方案3：合并到现有 DeepSeek

在 autonomous_system.py 中，加载 LoRA 权重叠加到 DeepSeek API 调用：
- 不可行——LoRA 权重必须和基座模型一致
- 需要用 Qwen2.5-7B 作为基座，不能用 DeepSeek API

---

## 训练数据说明

训练数据包含三类样本：

| 类型 | 数量 | 说明 |
|------|------|------|
| 核心认知样本 | 8条 | 手写的高质量"能力推理"示例，教Brain正确的推理路径 |
| 正样本 | 100条 | 从历史日志中提取的高价值方向决策 |
| 纠正样本 | 50条 | 低价值方向 → 正确推理的"纠偏"示例 |

### 继续积累数据

每次你和Brain对话，系统自动记录。下次想微调时：
```bash
cd C:\Users\楚\WorkBuddy\2026-05-15-task-28
python build_finetune_dataset.py  # 重新生成数据集
```

数据越多，微调效果越好。建议积累到 **300-500条** 后做第二次微调。

---

## 常见问题

**Q: 微调后模型会比 DeepSeek API 差吗？**
A: Qwen2.5-7B 微调后在特定任务（能力推理+方向选择）上会更好，但通用能力（写代码、分析）不如 DeepSeek V3/V4（千亿参数）。可以考虑混合方案：微调后的模型做决策，DeepSeek API 做执行。

**Q: 训练失败了怎么办？**
A: 检查 `/root/claw_brain_lora/` 下是否有 checkpoint，可以用 `resume_from_checkpoint` 继续。

**Q: 费用超了怎么办？**
A: L4 实例 ¥1.68/h，训练一次最多 3 小时 ≈ ¥5。充值 ¥10 绰绰有余。

**Q: 微调后怎么评估效果？**
A: 脚本最后会自动跑一个测试用例。也可以手动测试：给模型"帮我赚钱"的指令，看它是否会先推理能力再选方向。
