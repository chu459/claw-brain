# Claw-brain 微调一键指南 v3

## 你要上传的文件（2个）

| 文件 | 说明 |
|------|------|
| `training_data_v3.jsonl` | 27条精炼训练数据 |
| `finetune_v3.py` | 一键训练脚本 |

## AutoDL 操作步骤

### 1. 注册 & 充值
- 打开 https://www.autodl.com
- 注册，充值 ¥10（足够跑5次）

### 2. 创建实例
- GPU: **L4 (24GB)** ¥1.68/h 或 **RTX 4090** ¥2.68/h
- 镜像: **PyTorch 2.0 + Python 3.10**
- 磁盘: 默认即可

### 3. 上传文件
- 把 `training_data_v3.jsonl` 和 `finetune_v3.py` 上传到 `/root/`
- AutoDL 控制台有"文件管理"可以直接拖拽上传

### 4. 打开终端，执行

```bash
cd /root
pip install unsloth transformers datasets trl peft bitsandbytes accelerate
python finetune_v3.py
```

### 5. 等待 20-40 分钟
- 你会看到训练进度条
- 训练完后自动跑6个验证测试

### 6. 检查验证结果
脚本最后会输出6个测试问答，对照验证要点：

| 测试 | 输入 | 期望输出 |
|------|------|----------|
| 1 | 帮我赚钱 | 先推理能力，不是搜赚钱项目 |
| 2 | 简历优化 | 拒绝（内容生成，不是自动化） |
| 3 | 需要人工确认 | 拒绝（不是全自动闭环） |
| 4 | 3-7天MVP | 按轮次算，不按天数算 |
| 5 | 月入固定3000 | 放弃（上限低） |
| 6 | 选好方向 | 长思考可行性，不急着动手 |

### 7. 下载结果
- LoRA权重: `/root/claw_brain_lora_v3/` (~100MB)
- 合并模型: `/root/claw_brain_merged_v3/` (~15GB，需要的话)

**只下载 LoRA 权重就行**，不需要下载合并模型。

### 8. 部署到 claw-brain
把下载的 LoRA 权重放到项目目录，修改 `brain.py` 的模型加载路径。

## 如果效果不满意

1. 告诉我哪里不对（比如"它还是做PPT"）
2. 我加几条针对性训练数据
3. 重新上传跑一次

每次微调都在上一版基础上改进，不需要从头来。
