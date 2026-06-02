"""
Claw-brain 微调训练脚本 v2
==========================
基座模型: Qwen2.5-7B-Instruct
方法: QLoRA (4-bit量化 + LoRA)
数据: training_data_v2.jsonl (55条高质量手写样本)

使用方法:
1. AutoDL 租 L4 24GB 实例 (¥1.68/h)
2. 上传 training_data_v2.jsonl + 本脚本到 /root/
3. pip install unsloth transformers datasets trl peft bitsandbytes accelerate
4. python finetune_v2.py

预计时间: 30-60分钟 (55条数据量小)
预计费用: ¥1-2
"""

import json
import os
import torch
from pathlib import Path

# ==================== 配置 ====================
DATASET_PATH = "/root/training_data_v2.jsonl"
OUTPUT_DIR = "/root/claw_brain_lora_v2"
MERGED_DIR = "/root/claw_brain_merged_v2"

# 基座模型
BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"

# QLoRA 参数
LORA_R = 32            # 提高秩 → 更强的学习能力（数据量小时更重要）
LORA_ALPHA = 64
LORA_DROPOUT = 0.05
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "up_proj", "down_proj", "gate_proj"]

# 训练参数
NUM_EPOCHS = 5          # 数据量小，多跑几轮
BATCH_SIZE = 2
GRADIENT_ACCUMULATION = 4
LEARNING_RATE = 3e-4    # 数据量小时学习率可以稍高
MAX_SEQ_LENGTH = 2048
WARMUP_RATIO = 0.05
WEIGHT_DECAY = 0.01

# System prompt（训练时的系统提示词）
SYSTEM_PROMPT = (
    "你是Claw-brain，一个拥有7x24自动化操作能力的AI创业者。"
    "你有小龙虾（浏览器自动化+终端），能跨平台编排、无人值守运行。"
    "选方向时先推理自己的能力组合能做什么别人做不到的事，"
    "然后评估商业价值（市场大小、竞争壁垒、ROI）。"
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
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{item['instruction']}\n\n{item.get('input', '')}"},
                    {"role": "assistant", "content": item['output']},
                ]
                samples.append({"messages": messages})
            except (json.JSONDecodeError, KeyError) as e:
                print(f"  跳过第{line_num}行（格式错误: {e}）")

    print(f"加载了 {len(samples)} 条训练样本")

    # 小数据集不划分验证集，全部用于训练
    train_path = "/root/train.jsonl"
    with open(train_path, 'w', encoding='utf-8') as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')

    print(f"训练集: {len(samples)} 条 → {train_path}")
    return train_path, len(samples)


def main():
    print("=" * 60)
    print("Claw-brain 微调训练 v2")
    print(f"基座模型: {BASE_MODEL}")
    print(f"LoRA: r={LORA_R}, alpha={LORA_ALPHA}")
    print(f"训练轮数: {NUM_EPOCHS}")
    print(f"学习率: {LEARNING_RATE}")
    print(f"数据: {DATASET_PATH}")
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

    # 计算合理的保存步数
    steps_per_epoch = max(1, num_samples // (BATCH_SIZE * GRADIENT_ACCUMULATION))
    save_steps = max(10, steps_per_epoch)  # 每个epoch保存一次

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset["train"],
        dataset_text_field="text",
        max_seq_length=MAX_SEQ_LENGTH,
        args=TrainingArguments(
            output_dir=OUTPUT_DIR,
            num_train_epochs=NUM_EPOCHS,
            per_device_train_batch_size=BATCH_SIZE,
            gradient_accumulation_steps=GRADIENT_ACCUMULATION,
            learning_rate=LEARNING_RATE,
            warmup_ratio=WARMUP_RATIO,
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

    print(f"\n开始训练... (每epoch约{steps_per_epoch}步)")
    trainer.train()

    # 6. 保存 LoRA 权重
    print(f"\n保存 LoRA 权重到 {OUTPUT_DIR}")
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)

    # 7. 合并模型（可选）
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

    # 8. 验证测试
    print("\n" + "=" * 60)
    print("验证测试")
    print("=" * 60)

    test_cases = [
        "帮我赚钱，我该做什么方向？",
        "你搜索到了AI简历优化服务，月销500+。你选不选？",
        "你在考虑做PPT代做服务。该不该做？",
        "你在想怎么选赚钱方向。你的推理过程是什么？",
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
                max_new_tokens=300,
                temperature=0.7,
                do_sample=True,
                top_p=0.9,
            )
        response = tokenizer.decode(outputs[0][inputs.shape[1]:], skip_special_tokens=True)
        print(f"\nQ: {test_input}")
        print(f"A: {response[:300]}...")
        print("-" * 40)

    print("\n" + "=" * 60)
    print("训练完成！")
    print(f"LoRA权重: {OUTPUT_DIR}")
    print(f"合并模型: {MERGED_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    print(f"PyTorch: {torch.__version__}")
    print(f"CUDA: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        print(f"VRAM: {torch.cuda.get_device_properties(0).total_mem / 1024**3:.1f} GB")
    main()
