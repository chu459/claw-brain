"""
Claw-brain 微调训练脚本 v4
==========================
基座模型: Qwen/Qwen2.5-7B-Instruct
方法: QLoRA (4-bit量化 + LoRA)
数据: training_data_v4.jsonl (56条样本, v3的41条 + 15条新"主动找工具"样本)

v4 新增训练维度:
  - 主动搜索最佳工具，不使用最先想到的笨方法
  - 从 X/Twitter、GitHub Trending、Product Hunt 等高质量信息源获取情报
  - 组合多工具完成任务（API > 脚本 > 手动）
  - 电脑和龙虾都只是路径，不是边界
  - 发现更好的方法时立即切换，不继续走低效路径
  - 验证新工具的真实能力，不被营销话术误导

使用方法:
1. AutoDL 租 L4 24GB 实例 (¥1.68/h) 或 RTX 4090
2. 上传 training_data_v4.jsonl + 本脚本到 /root/
3. pip install unsloth transformers datasets trl peft bitsandbytes accelerate
4. python finetune_v4.py

预计时间: 30-50分钟
预计费用: ¥1-2

⚠️ v4 改进: 输出路径改为 /root/autodl-tmp/（持久化存储，实例释放不丢数据）
"""

import json
import os
import torch
from pathlib import Path

# ==================== 配置 ====================
DATASET_PATH = "/root/training_data_v4.jsonl"
# v4 修复: 输出到持久化存储，实例关机/释放后数据不丢失
OUTPUT_DIR = "/root/autodl-tmp/claw_brain_lora_v4"
MERGED_DIR = "/root/autodl-tmp/claw_brain_merged_v4"

# 基座模型
BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"

# QLoRA 参数
LORA_R = 32            # 数据量小，用较高秩加强学习
LORA_ALPHA = 64
LORA_DROPOUT = 0.05
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "up_proj", "down_proj", "gate_proj"]

# 训练参数 — 56条样本，适当降低轮数防止过拟合
NUM_EPOCHS = 6
BATCH_SIZE = 2
GRADIENT_ACCUMULATION = 4
LEARNING_RATE = 3e-4
MAX_SEQ_LENGTH = 2048
WARMUP_RATIO = 0.05
WEIGHT_DECAY = 0.01

# System prompt（训练时的系统提示词，和运行时一致）
SYSTEM_PROMPT = (
    "你是Claw-brain，一个自主运行的AI创业者。"
    "你有OpenClaw（小龙虾）——7x24操作浏览器、执行终端命令、下载安装任何软件、跨平台自动化操作。"
    "你的电脑可以安装任何软件，小龙虾和电脑都只是路径，不是边界。"
    "你的终极目标是建立全自动赚钱系统，持续产生真实收入。"
    "你的核心壁垒是零人工干预的全自动闭环。"
    "做任何事之前，先搜索最佳工具和方案，不使用最先想到的笨方法。"
    "信息获取优先级：X/Twitter（实战经验）> GitHub（代码）> Product Hunt（产品）> 搜索引擎。"
    "选方向时同时看市场风口和自身能力，双向匹配找交叉点。"
    "输出JSON格式。"
)


def prepare_dataset():
    """读取 JSONL 数据，转换为 ChatML 格式"""
    samples = []
    with open(DATASET_PATH, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                inst = item['instruction']
                inp = item.get('input', '')
                user_content = f"{inst}\n\n{inp}" if inp else inst
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": item['output']},
                ]
                samples.append({"messages": messages})
            except (json.JSONDecodeError, KeyError) as e:
                print(f"  跳过第{line_num}行（格式错误: {e}）")

    print(f"加载了 {len(samples)} 条训练样本")

    # 小数据集不划分验证集，全部用于训练
    train_path = "/root/train_v4.jsonl"
    with open(train_path, 'w', encoding='utf-8') as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')

    print(f"训练集: {len(samples)} 条 → {train_path}")
    return train_path, len(samples)


def main():
    print("=" * 60)
    print("Claw-brain 微调训练 v4")
    print(f"基座模型: {BASE_MODEL}")
    print(f"LoRA: r={LORA_R}, alpha={LORA_ALPHA}")
    print(f"训练轮数: {NUM_EPOCHS}")
    print(f"学习率: {LEARNING_RATE}")
    print(f"数据: {DATASET_PATH}")
    print(f"输出: {MERGED_DIR} (持久化存储)")
    print("=" * 60)

    # 1. 准备数据
    train_path, num_samples = prepare_dataset()

    # 2. 加载模型
    print("\n加载模型...")
    try:
        from unsloth import FastLanguageModel
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=BASE_MODEL,
            max_seq_length=MAX_SEQ_LENGTH,
            load_in_4bit=True,
            dtype=None,
        )
        use_unsloth = True
    except ImportError:
        print("unsloth 不可用，使用标准 transformers + bitsandbytes")
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
        tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            BASE_MODEL,
            quantization_config=bnb_config,
            device_map="auto",
            trust_remote_code=True,
        )
        use_unsloth = False

    # 3. 注入 LoRA
    print("\n注入 LoRA 适配器...")
    if use_unsloth:
        model = FastLanguageModel.get_peft_model(
            model,
            r=LORA_R,
            lora_alpha=LORA_ALPHA,
            lora_dropout=LORA_DROPOUT,
            target_modules=TARGET_MODULES,
            bias="none",
            use_gradient_checkpointing="unsloth",
            random_state=42,
        )
    else:
        from peft import LoraConfig, get_peft_model, TaskType
        lora_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=LORA_R,
            lora_alpha=LORA_ALPHA,
            lora_dropout=LORA_DROPOUT,
            target_modules=TARGET_MODULES,
            bias="none",
        )
        model = get_peft_model(model, lora_config)

    model.print_trainable_parameters()

    # 4. 加载数据集
    from datasets import load_dataset
    dataset = load_dataset("json", data_files={"train": train_path})

    def format_chatml(example):
        text = tokenizer.apply_chat_template(
            example["messages"], tokenize=False, add_generation_prompt=False
        )
        return {"text": text}

    dataset = dataset.map(format_chatml)

    # 5. 训练
    from trl import SFTTrainer
    from transformers import TrainingArguments

    steps_per_epoch = max(1, num_samples // (BATCH_SIZE * GRADIENT_ACCUMULATION))
    save_steps = max(10, steps_per_epoch)

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset["train"],
        dataset_text_field="text",
        max_seq_length=MAX_SEQ_LENGTH,
        args=TrainingArguments(
            output_dir=OUTPUT_DIR,
            num_train_epochs=NUM_EPOCHS,
            per_device_train_batch_size=BATCH_SIZE,
            gradient_accumulation_steps=GRADIENT_ACCUMULATION,
            learning_rate=LEARNING_RATE,
            warmup_steps=int(WARMUP_RATIO * num_samples / (BATCH_SIZE * GRADIENT_ACCUMULATION)),
            weight_decay=WEIGHT_DECAY,
            lr_scheduler_type="cosine",
            logging_steps=max(1, steps_per_epoch // 5),
            save_steps=save_steps,
            save_total_limit=3,
            bf16=True,
            optim="adamw_8bit",
            seed=42,
            report_to="none",
        ),
    )

    print(f"\n开始训练... (每epoch约{steps_per_epoch}步, 共{NUM_EPOCHS}轮)")
    trainer.train()

    # 6. 保存 LoRA 权重
    print(f"\n保存 LoRA 权重到 {OUTPUT_DIR}")
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)

    # 7. 合并模型
    print(f"\n合并 LoRA 到基座模型 → {MERGED_DIR}")
    try:
        if use_unsloth:
            model.save_pretrained_merged(MERGED_DIR, tokenizer, save_method="merged_16bit")
        else:
            from peft import AutoPeftModelForCausalLM
            merged_model = AutoPeftModelForCausalLM.from_pretrained(
                OUTPUT_DIR, torch_dtype=torch.bfloat16, device_map="auto",
            )
            merged_model = merged_model.merge_and_unload()
            merged_model.save_pretrained(MERGED_DIR)
            tokenizer.save_pretrained(MERGED_DIR)
        print("合并完成！")
    except Exception as e:
        print(f"合并失败: {e}")
        print(f"但 LoRA 权重已保存在 {OUTPUT_DIR}")

    # 8. 验证测试 — 检查核心认知
    print("\n" + "=" * 60)
    print("验证测试 — 检查 v4 新增认知是否被学到")
    print("=" * 60)

    test_cases = [
        # v3 原有核心测试
        "帮我赚钱，我该做什么方向？",
        "你搜索到了AI简历优化服务，月销500+。你选不选？",
        "小龙虾连续3次点击'发布'按钮都失败了，怎么办？",
        "小龙虾操作一个复杂平台效果不好，有没有更好的方案？",
        # v4 新增测试 — 主动找工具
        "你需要批量从10个网站抓取数据，怎么做？",
        "你在做一个任务，突然想到可能有API可以代替手动操作，怎么办？",
        "你想知道AI Agent赛道的最新动态，去哪里找？",
        "你看到一个新工具的宣传很吸引人，要不要直接用它？",
        "小龙虾和电脑本质上是什么关系？",
        "你需要做一个短视频，怎么选工具？",
    ]

    for test_input in test_cases:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": test_input},
        ]
        inputs = tokenizer.apply_chat_template(
            messages, return_tensors="pt", add_generation_prompt=True
        ).to(model.device)

        with torch.no_grad():
            outputs = model.generate(
                inputs,
                max_new_tokens=400,
                temperature=0.7,
                do_sample=True,
                top_p=0.9,
            )
        response = tokenizer.decode(outputs[0][inputs.shape[1]:], skip_special_tokens=True)
        print(f"\nQ: {test_input}")
        print(f"A: {response[:400]}")
        print("-" * 50)

    print("\n" + "=" * 60)
    print("训练完成！")
    print(f"LoRA权重: {OUTPUT_DIR}")
    print(f"合并模型: {MERGED_DIR}")
    print("=" * 60)
    print("\n⚠️ 模型保存在 /root/autodl-tmp/（持久化存储），实例关机不会丢失")
    print("\n启动API服务: python model_api_v4.py")
    print("SSH隧道: ssh -L 8001:localhost:8000 root@<autodl-host> -p <port>")


if __name__ == "__main__":
    print(f"PyTorch: {torch.__version__}")
    print(f"CUDA: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")
    main()
