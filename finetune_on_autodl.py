"""
AutoDL 云上微调脚本

使用方法：
1. 在 AutoDL 租一个 L4 (24GB) 实例，选 PyTorch 2.0 + Python 3.10 镜像
2. 上传这个脚本 + training_data_auto.jsonl 到 /root/
3. pip install unsloth
4. python finetune_on_autodl.py

预计时间: 2-3小时（L4 + QLoRA + 7B模型）
预计费用: ¥3-5

微调完成后输出:
- /root/claw_brain_lora/ (LoRA适配器权重)
- /root/claw_brain_merged/ (合并后的完整模型，可选)
"""

import json
import os
from pathlib import Path

# ==================== 配置 ====================
DATASET_PATH = "/root/training_data_final.jsonl"
OUTPUT_DIR = "/root/claw_brain_lora"
MERGED_DIR = "/root/claw_brain_merged"

# 基座模型选择
# Qwen2.5-7B-Instruct: 中文能力强，适合我们的场景
BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"

# QLoRA 参数
LORA_R = 16            # LoRA 秩
LORA_ALPHA = 32        # LoRA alpha
LORA_DROPOUT = 0.05    # Dropout
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "up_proj", "down_proj", "gate_proj"]

# 训练参数
NUM_EPOCHS = 3
BATCH_SIZE = 2          # L4 24GB 用 batch_size=2 + gradient_accumulation=4
GRADIENT_ACCUMULATION = 4
LEARNING_RATE = 2e-4
MAX_SEQ_LENGTH = 2048
WARMUP_RATIO = 0.1

# ==================== 主流程 ====================

def prepare_dataset():
    """读取 JSONL 数据，转换为训练格式"""
    samples = []
    with open(DATASET_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                item = json.loads(line)
                # 构造 ChatML 格式（Qwen2.5 默认格式）
                messages = [
                    {"role": "system", "content": "你是Claw-brain，一个拥有7x24自动化操作能力的AI创业者。你有小龙虾（浏览器自动化+终端），能跨平台编排、无人值守运行。选方向时先推理自己的能力组合，只做需要自动化系统才能交付的事。输出JSON格式。"},
                    {"role": "user", "content": f"{item['instruction']}\n\n{item.get('input', '')}"},
                    {"role": "assistant", "content": item['output']},
                ]
                samples.append({"messages": messages})

    print(f"加载了 {len(samples)} 条训练样本")

    # 划分训练集和验证集 (90/10)
    split = int(len(samples) * 0.9)
    train_data = samples[:split]
    val_data = samples[split:] if split < len(samples) else []

    # 保存为 JSONL
    train_path = "/root/train.jsonl"
    val_path = "/root/val.jsonl"

    with open(train_path, 'w', encoding='utf-8') as f:
        for s in train_data:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')

    if val_data:
        with open(val_path, 'w', encoding='utf-8') as f:
            for s in val_data:
                f.write(json.dumps(s, ensure_ascii=False) + '\n')

    print(f"训练集: {len(train_data)} 条 → {train_path}")
    print(f"验证集: {len(val_data)} 条 → {val_path}")

    return train_path, val_path


def main():
    print("=" * 60)
    print("Claw-brain 微调训练")
    print(f"基座模型: {BASE_MODEL}")
    print(f"LoRA参数: r={LORA_R}, alpha={LORA_ALPHA}")
    print(f"训练轮数: {NUM_EPOCHS}")
    print("=" * 60)

    # 1. 准备数据
    train_path, val_path = prepare_dataset()

    # 2. 加载模型（使用 unsloth 加速）
    try:
        from unsloth import FastLanguageModel
        model, tokenizer = FastLanguageModel.from_pretrained(
            model_name=BASE_MODEL,
            max_seq_length=MAX_SEQ_LENGTH,
            load_in_4bit=True,  # 4-bit 量化，省显存
            dtype=None,  # 自动检测
        )
    except ImportError:
        # 如果 unsloth 不可用，用标准 transformers
        print("unsloth 不可用，使用标准 transformers + bitsandbytes")
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        import torch

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

    # 3. 注入 LoRA 适配器
    try:
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
    except:
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

    # 4. 加载训练数据
    from datasets import load_dataset
    dataset = load_dataset("json", data_files={"train": train_path, "test": val_path} if os.path.exists(val_path) else {"train": train_path})

    # 格式化为 ChatML
    def format_chatml(example):
        text = tokenizer.apply_chat_template(example["messages"], tokenize=False, add_generation_prompt=False)
        return {"text": text}

    dataset = dataset.map(format_chatml)

    # 5. 训练
    from trl import SFTTrainer
    from transformers import TrainingArguments

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset["train"],
        eval_dataset=dataset.get("test"),
        dataset_text_field="text",
        max_seq_length=MAX_SEQ_LENGTH,
        args=TrainingArguments(
            output_dir=OUTPUT_DIR,
            num_train_epochs=NUM_EPOCHS,
            per_device_train_batch_size=BATCH_SIZE,
            gradient_accumulation_steps=GRADIENT_ACCUMULATION,
            learning_rate=LEARNING_RATE,
            warmup_ratio=WARMUP_RATIO,
            lr_scheduler_type="cosine",
            logging_steps=10,
            save_steps=50,
            eval_strategy="steps" if val_path else "no",
            eval_steps=50 if val_path else None,
            bf16=True,
            optim="adamw_8bit",
            seed=42,
            report_to="none",
        ),
    )

    print("\n开始训练...")
    trainer.train()

    # 6. 保存 LoRA 权重
    print(f"\n保存 LoRA 权重到 {OUTPUT_DIR}")
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)

    # 7. 可选：合并 LoRA 到基座模型
    print(f"\n合并 LoRA 到基座模型 → {MERGED_DIR}")
    try:
        model.save_pretrained_merged(MERGED_DIR, tokenizer, save_method="merged_16bit")
        print("合并完成！可以用 vLLM 或 transformers 直接加载合并后的模型")
    except:
        # 手动合并
        try:
            from peft import AutoPeftModelForCausalLM
            merged_model = AutoPeftModelForCausalLM.from_pretrained(
                OUTPUT_DIR,
                torch_dtype=torch.bfloat16,
                device_map="auto",
            )
            merged_model = merged_model.merge_and_unload()
            merged_model.save_pretrained(MERGED_DIR)
            tokenizer.save_pretrained(MERGED_DIR)
            print("手动合并完成！")
        except Exception as e:
            print(f"合并失败: {e}")
            print(f"但 LoRA 权重已保存在 {OUTPUT_DIR}，可以后续合并")

    # 8. 快速验证
    print("\n=== 快速验证 ===")
    test_messages = [
        {"role": "system", "content": "你是Claw-brain，一个拥有7x24自动化操作能力的AI创业者。"},
        {"role": "user", "content": "帮我赚钱，我该做什么方向？"},
    ]
    inputs = tokenizer.apply_chat_template(test_messages, return_tensors="pt", add_generation_prompt=True).to(model.device)
    with torch.no_grad():
        outputs = model.generate(inputs, max_new_tokens=300, temperature=0.7, do_sample=True)
    response = tokenizer.decode(outputs[0][inputs.shape[1]:], skip_special_tokens=True)
    print(f"测试输入: 帮我赚钱，我该做什么方向？")
    print(f"模型输出: {response}")

    print("\n" + "=" * 60)
    print("训练完成！")
    print(f"LoRA权重: {OUTPUT_DIR}")
    print(f"合并模型: {MERGED_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    import torch
    print(f"PyTorch: {torch.__version__}")
    print(f"CUDA available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        print(f"VRAM: {torch.cuda.get_device_properties(0).total_mem / 1024**3:.1f} GB")
    main()
