"""
vector_memory.py - 向量记忆系统
=============================
使用百炼 text-embedding-v4 + ChromaDB 实现语义记忆存储和检索。

功能：
  1. 记忆的语义向量化存储（ChromaDB 本地持久化）
  2. 基于语义相似度的记忆检索（查询时自动匹配最相关的历史记忆）
  3. 脏记忆治理：
     - 语义去重（相似度 >0.95 的记忆合并）
     - 时效衰减（超过30天的记忆权重降低）
     - 相关性阈值（低于0.3的结果不返回）
     - 手动标记删除
  4. 与现有 system_memory.json 无缝集成

依赖：
  - chromadb (pip install chromadb)
  - openai (pip install openai)
  - 百炼 API Key (环境变量 DASHSCOPE_API_KEY)

百炼免费额度：100万 Token，90天有效，0.0005元/千Token
"""

import os
import json
import time
import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta


# ===================== 配置 =====================

VECTOR_DB_DIR = str(Path(__file__).parent / ".vector_db")
COLLECTION_NAME = "claw_brain_memory"

# 百炼 API 配置
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIMENSIONS = 384  # text-embedding-v4 实际返回384维

# 脏记忆治理参数
DEDUP_THRESHOLD = 0.95       # 语义相似度超过此值视为重复
MIN_RELEVANCE = 0.3          # 检索结果最低相关性
MEMORY_DECAY_DAYS = 30       # 超过此天数的记忆权重衰减
MAX_MEMORIES = 500           # 最大记忆数量


# ===================== 向量客户端 =====================

class EmbeddingClient:
    """百炼 text-embedding-v4 向量化客户端"""

    def __init__(self, api_key: str = "", base_url: str = ""):
        self.api_key = api_key or DASHSCOPE_API_KEY
        self.base_url = base_url or DASHSCOPE_BASE_URL
        self._client = None
        self._available = False

    def _get_client(self):
        """延迟初始化 OpenAI 客户端"""
        if self._client is None and self.api_key:
            try:
                from openai import OpenAI
                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                )
                self._available = True
            except Exception:
                self._available = False
        return self._client

    def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        """
        将文本列表向量化。
        返回嵌入向量列表，失败返回 None。
        """
        if not texts:
            return []
        client = self._get_client()
        if not client:
            return None

        try:
            # 百炼单次最多10条，自动分批
            batch_size = 10
            all_embeddings = []
            for i in range(0, len(texts), batch_size):
                batch = texts[i:i + batch_size]
                resp = client.embeddings.create(
                    model=EMBEDDING_MODEL,
                    input=batch,
                )
                # 按index排序确保顺序
                sorted_data = sorted(resp.data, key=lambda x: x.index)
                all_embeddings.extend([d.embedding for d in sorted_data])
            return all_embeddings
        except Exception as e:
            print(f"[VECTOR] Embedding failed: {e}")
            return None

    def is_available(self) -> bool:
        """检查向量服务是否可用"""
        if self._available:
            return True
        if not self.api_key:
            return False
        self._get_client()
        return self._available


# ===================== 向量记忆管理 =====================

class VectorMemory:
    """
    向量记忆管理器。
    使用 ChromaDB 本地存储，百炼 text-embedding-v4 向量化。
    """

    def __init__(self, db_dir: str = VECTOR_DB_DIR):
        self.db_dir = db_dir
        self._embedding_client = EmbeddingClient()
        self._chroma = None
        self._collection = None
        self._fallback_mode = False  # 降级为纯文本模式

    def _get_collection(self):
        """延迟初始化 ChromaDB"""
        if self._collection is not None:
            return self._collection

        try:
            import chromadb
            self._chroma = chromadb.PersistentClient(path=self.db_dir)
            # 使用自定义 embedding function 或默认
            if self._embedding_client.is_available():
                self._collection = self._chroma.get_or_create_collection(
                    name=COLLECTION_NAME,
                    metadata={"hnsw:space": "cosine"},
                )
            else:
                # 无 API key 时使用默认的 embedding
                self._collection = self._chroma.get_or_create_collection(
                    name=COLLECTION_NAME,
                    metadata={"hnsw:space": "cosine"},
                )
                self._fallback_mode = True
                print("[VECTOR] Warning: No DashScope API key, using fallback text search")
            return self._collection
        except Exception as e:
            print(f"[VECTOR] ChromaDB init failed: {e}")
            self._fallback_mode = True
            return None

    @staticmethod
    def _text_hash(text: str) -> str:
        """生成文本的确定性ID"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]

    def add_memory(
        self,
        text: str,
        category: str = "general",
        source: str = "system",
        metadata: Optional[dict] = None,
        verified: bool = False,
    ) -> bool:
        """
        添加一条记忆。

        Args:
            text: 记忆文本内容
            category: 分类 (action_result / strategy / credential / milestone / error / general)
            source: 来源 (system / user / openclaw / brain)
            metadata: 额外元数据
            verified: 是否为已验证的高质量记忆（优先检索）

        Returns:
            是否成功
        """
        col = self._get_collection()
        if col is None:
            return False

        mem_id = self._text_hash(text)
        extra_meta = metadata or {}

        # 检查是否已存在（精确去重）
        try:
            existing = col.get(ids=[mem_id])
            if existing and existing["ids"]:
                old_meta = existing["metadatas"][0] if existing["metadatas"] else {}
                # 合并 verified 状态：如果新记忆已验证或旧记忆已验证，保持已验证
                new_verified = verified or old_meta.get("verified", False)
                col.update(
                    ids=[mem_id],
                    metadatas=[{
                        "category": category,
                        "source": source,
                        "updated_at": datetime.now().isoformat(),
                        "verified": new_verified,
                        **extra_meta,
                    }],
                )
                return True
        except Exception:
            pass

        # 去重检查：与最近记忆做语义比对
        dup_id = self._check_semantic_dup(text)
        if dup_id:
            # 语义重复：更新时间戳和内容（保留较新的），继承已验证状态
            try:
                old = col.get(ids=[dup_id])
                old_verified = False
                if old and old["metadatas"]:
                    old_verified = old["metadatas"][0].get("verified", False)
            except Exception:
                old_verified = False

            final_verified = verified or old_verified
            col.update(
                ids=[dup_id],
                documents=[text],
                metadatas=[{
                    "category": category,
                    "source": source,
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                    "verified": final_verified,
                    **extra_meta,
                }],
            )
            print(f"[VECTOR] Updated duplicate memory: {dup_id} (verified={final_verified})")
            return True

        # 向量化并存储
        now = datetime.now().isoformat()

        if not self._fallback_mode and self._embedding_client.is_available():
            embeddings = self._embedding_client.embed([text])
            if embeddings and embeddings[0]:
                try:
                    col.add(
                        ids=[mem_id],
                        documents=[text],
                        embeddings=[embeddings[0]],
                        metadatas=[{
                            "category": category,
                            "source": source,
                            "created_at": now,
                            "updated_at": now,
                            "verified": verified,
                            **extra_meta,
                        }],
                    )
                    tag = "[VERIFIED]" if verified else ""
                    print(f"[VECTOR] Stored: {tag} [{category}] {text[:60]}...")
                    return True
                except Exception as e:
                    print(f"[VECTOR] Store failed: {e}")
                    self._fallback_mode = True

        # 降级模式：不带向量的文本存储
        try:
            col.add(
                ids=[mem_id],
                documents=[text],
                metadatas=[{
                    "category": category,
                    "source": source,
                    "created_at": now,
                    "updated_at": now,
                    "verified": verified,
                    **extra_meta,
                }],
            )
            return True
        except Exception as e:
            print(f"[VECTOR] Fallback store failed: {e}")
            return False

    def search(
        self,
        query: str,
        n_results: int = 5,
        category: Optional[str] = None,
        verified_first: bool = True,
    ) -> list[dict]:
        """
        语义搜索记忆。已验证记忆优先返回。

        Args:
            query: 查询文本
            n_results: 返回数量
            category: 限定分类（可选）
            verified_first: 是否已验证记忆优先排序

        Returns:
            [{"text": str, "category": str, "source": str,
              "distance": float, "relevance": float, "verified": bool,
              "created_at": str}, ...]
        """
        col = self._get_collection()
        if col is None:
            return []

        # 获取总记忆数
        try:
            count = col.count()
            if count == 0:
                return []
        except Exception:
            return []

        # 多取一些结果用于排序后截取
        fetch_n = min(n_results * 2 + 3, count)

        # 构建查询条件
        where_filter = None
        if category:
            where_filter = {"category": category}

        try:
            if self._fallback_mode or not self._embedding_client.is_available():
                results = col.query(
                    query_texts=[query],
                    n_results=fetch_n,
                    where=where_filter,
                )
            else:
                query_embedding = self._embedding_client.embed([query])
                if not query_embedding or not query_embedding[0]:
                    results = col.query(
                        query_texts=[query],
                        n_results=fetch_n,
                        where=where_filter,
                    )
                else:
                    results = col.query(
                        query_embeddings=[query_embedding[0]],
                        n_results=fetch_n,
                        where=where_filter,
                    )
        except Exception as e:
            print(f"[VECTOR] Search failed: {e}")
            return []

        if not results or not results["documents"] or not results["documents"][0]:
            return []

        memories = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            distance = results["distances"][0][i] if results["distances"] else 1.0

            # 时效衰减计算
            created_at = meta.get("created_at", "")
            decay_factor = self._calc_decay(created_at)

            # 相关性 = (1 - distance) * decay_factor
            relevance = (1.0 - distance) * decay_factor

            # 已验证记忆加权：verified 记忆相关性提升 30%
            is_verified = meta.get("verified", False)
            if is_verified:
                relevance *= 1.3

            # 过滤低相关性记忆
            if relevance < MIN_RELEVANCE:
                continue

            memories.append({
                "text": doc,
                "category": meta.get("category", "general"),
                "source": meta.get("source", "system"),
                "distance": distance,
                "relevance": round(relevance, 3),
                "verified": is_verified,
                "created_at": created_at,
                "updated_at": meta.get("updated_at", ""),
            })

        # 已验证记忆优先排序：verified 排前面，同等 verified 按 relevance 排
        if verified_first:
            memories.sort(key=lambda m: (not m["verified"], -m["relevance"]))
        else:
            memories.sort(key=lambda m: -m["relevance"])

        return memories[:n_results]

    def _check_semantic_dup(self, text: str) -> Optional[str]:
        """检查是否有语义重复的记忆，返回重复记忆ID或None"""
        col = self._get_collection()
        if col is None:
            return None

        try:
            count = col.count()
            if count == 0:
                return None
        except Exception:
            return None

        if self._fallback_mode or not self._embedding_client.is_available():
            return None

        # 搜索最近最相似的3条
        try:
            embedding = self._embedding_client.embed([text])
            if not embedding or not embedding[0]:
                return None

            results = col.query(
                query_embeddings=[embedding[0]],
                n_results=min(3, count),
            )

            if results and results["distances"] and results["distances"][0]:
                for i, dist in enumerate(results["distances"][0]):
                    if dist < (1 - DEDUP_THRESHOLD):  # cosine similarity > threshold
                        return results["ids"][0][i]
        except Exception:
            pass

        return None

    @staticmethod
    def _calc_decay(created_at: str) -> float:
        """
        计算时效衰减因子。
        0-7天: 1.0 (完全有效)
        7-30天: 0.8-1.0 (线性衰减)
        30天+: 0.5 (保留但不推荐)
        """
        if not created_at:
            return 1.0

        try:
            created = datetime.fromisoformat(created_at)
            age_days = (datetime.now() - created).days

            if age_days <= 7:
                return 1.0
            elif age_days <= MEMORY_DECAY_DAYS:
                # 7天到30天，从1.0线性衰减到0.8
                return 1.0 - 0.2 * (age_days - 7) / (MEMORY_DECAY_DAYS - 7)
            else:
                return 0.5
        except Exception:
            return 1.0

    def verify_memory(self, text: str, verified: bool = True) -> bool:
        """
        手动标记/取消标记记忆为已验证的高质量知识。
        已验证记忆在决策检索时会被优先返回且权重提升30%。
        """
        col = self._get_collection()
        if col is None:
            return False

        mem_id = self._text_hash(text)
        try:
            col.update(
                ids=[mem_id],
                metadatas=[{"verified": verified, "updated_at": datetime.now().isoformat()}],
            )
            tag = "VERIFIED" if verified else "UNVERIFIED"
            print(f"[VECTOR] Memory {tag}: {text[:60]}...")
            return True
        except Exception as e:
            print(f"[VECTOR] Verify failed: {e}")
            return False

    def auto_promote_verified(self, min_occurrences: int = 3) -> int:
        """
        自动提升：成功执行3次以上的模式自动标记为已验证。
        通过统计语义相似的成功记忆，出现次数>=min_occurrences的自动promote。
        返回提升数量。
        """
        col = self._get_collection()
        if col is None:
            return 0

        try:
            count = col.count()
            if count == 0:
                return 0
        except Exception:
            return 0

        if self._fallback_mode or not self._embedding_client.is_available():
            return 0  # 需要向量能力才能做语义聚合

        try:
            all_data = col.get()
            if not all_data or not all_data["ids"]:
                return 0

            promoted = 0
            # 找出所有成功的 action_result 记忆
            success_indices = []
            for i, meta in enumerate(all_data["metadatas"]):
                if (meta.get("category") == "action_result"
                        and meta.get("success", False)
                        and not meta.get("verified", False)):
                    success_indices.append(i)

            if len(success_indices) < min_occurrences:
                return 0

            # 对每条未验证的成功记忆，检查是否有足够多的相似成功记忆
            ids_to_promote = []
            for idx in success_indices:
                doc = all_data["documents"][idx]
                if doc is None:
                    continue
                embedding = self._embedding_client.embed([doc])
                if not embedding or not embedding[0]:
                    continue

                # 在所有成功记忆中搜索相似的
                similar_count = 0
                for other_idx in success_indices:
                    if other_idx == idx:
                        continue
                    other_doc = all_data["documents"][other_idx]
                    if other_doc is None:
                        continue
                    other_emb = self._embedding_client.embed([other_doc])
                    if not other_emb or not other_emb[0]:
                        continue
                    # 简单余弦相似度
                    dot = sum(a * b for a, b in zip(embedding[0], other_emb[0]))
                    norm_a = sum(a * a for a in embedding[0]) ** 0.5
                    norm_b = sum(b * b for b in other_emb[0]) ** 0.5
                    if norm_a > 0 and norm_b > 0:
                        sim = dot / (norm_a * norm_b)
                        if sim > 0.85:  # 高度相似
                            similar_count += 1

                if similar_count >= min_occurrences - 1:  # 加上自己>=min_occurrences
                    ids_to_promote.append(all_data["ids"][idx])

            if ids_to_promote:
                for mem_id in ids_to_promote:
                    col.update(
                        ids=[mem_id],
                        metadatas=[{"verified": True, "updated_at": datetime.now().isoformat()}],
                    )
                promoted = len(ids_to_promote)
                print(f"[VECTOR] Auto-promoted {promoted} memories to verified")

            return promoted
        except Exception as e:
            print(f"[VECTOR] Auto-promote failed: {e}")
            return 0

    def delete_memory(self, text: str) -> bool:
        """删除指定文本的记忆"""
        col = self._get_collection()
        if col is None:
            return False

        mem_id = self._text_hash(text)
        try:
            col.delete(ids=[mem_id])
            return True
        except Exception as e:
            print(f"[VECTOR] Delete failed: {e}")
            return False

    def delete_by_category(self, category: str) -> int:
        """删除指定分类的所有记忆，返回删除数量"""
        col = self._get_collection()
        if col is None:
            return 0

        try:
            # ChromaDB 的 where delete
            col.delete(where={"category": category})
            return -1  # ChromaDB 不返回删除数量
        except Exception as e:
            print(f"[VECTOR] Delete by category failed: {e}")
            return 0

    def get_stats(self) -> dict:
        """获取记忆统计信息"""
        col = self._get_collection()
        if col is None:
            return {
                "total": 0,
                "fallback": True,
                "api_available": self._embedding_client.is_available(),
                "db_dir": self.db_dir,
                "available": False,
                "error": "vector store unavailable",
            }

        try:
            count = col.count()
            return {
                "total": count,
                "fallback": self._fallback_mode,
                "api_available": self._embedding_client.is_available(),
                "db_dir": self.db_dir,
                "available": True,
            }
        except Exception as e:
            return {
                "total": 0,
                "fallback": self._fallback_mode,
                "api_available": self._embedding_client.is_available(),
                "db_dir": self.db_dir,
                "available": False,
                "error": str(e),
            }

    def cleanup_old_memories(self, max_age_days: int = 90) -> int:
        """
        清理过期的旧记忆。
        删除超过 max_age_days 天且相关性低的记忆。
        返回删除数量。
        """
        col = self._get_collection()
        if col is None:
            return 0

        try:
            count = col.count()
            if count == 0:
                return 0

            # 获取所有记忆
            all_data = col.get()
            if not all_data or not all_data["ids"]:
                return 0

            cutoff = datetime.now() - timedelta(days=max_age_days)
            to_delete = []

            for i, meta in enumerate(all_data["metadatas"]):
                created_at = meta.get("created_at", "")
                if created_at:
                    try:
                        created = datetime.fromisoformat(created_at)
                        if created < cutoff:
                            to_delete.append(all_data["ids"][i])
                    except Exception:
                        continue

            if to_delete:
                col.delete(ids=to_delete)
                print(f"[VECTOR] Cleaned up {len(to_delete)} old memories")

            # 同时控制总记忆数
            current_count = col.count()
            if current_count > MAX_MEMORIES:
                # 获取最早的记忆并删除
                all_data = col.get()
                ids_by_date = sorted(
                    zip(all_data["ids"], all_data["metadatas"]),
                    key=lambda x: x[1].get("created_at", ""),
                )
                excess = current_count - MAX_MEMORIES
                ids_to_remove = [item[0] for item in ids_by_date[:excess]]
                if ids_to_remove:
                    col.delete(ids=ids_to_remove)
                    print(f"[VECTOR] Trimmed {len(ids_to_remove)} memories to limit {MAX_MEMORIES}")

            return len(to_delete)
        except Exception as e:
            print(f"[VECTOR] Cleanup failed: {e}")
            return 0

    def add_failure_case(self, action: str, error: str, failure_type: str,
                         page_context: str = "", diagnosis: str = "", fix: str = "") -> bool:
        """
        添加失败案例到案例库。
        """
        case_text = f"[失败案例] 类型:{failure_type} | 操作:{action} | 错误:{error} | 诊断:{diagnosis} | 修复:{fix}"
        return self.add_memory(
            text=case_text,
            category="failure_case",
            source="system",
            metadata={
                "failure_type": failure_type,
                "page_context": page_context,
                "fix": fix,
                "fix_verified": False,
            },
            verified=False,
        )

    def search_failure_cases(self, query: str, n_results: int = 3) -> list[dict]:
        """
        搜索相似失败案例。
        """
        return self.search(query, n_results=n_results, category="failure_case", verified_first=True)

    def mark_case_verified(self, case_text: str, verified: bool = True) -> bool:
        """
        标记失败案例的修复方案为已验证。
        """
        return self.verify_memory(case_text, verified)

    def migrate_from_json(self, json_file: str) -> int:
        """
        从 system_memory.json 迁移历史记忆到向量库。
        返回迁移的记忆数量。
        """
        path = Path(json_file)
        if not path.exists():
            return 0

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0

        migrated = 0

        # 迁移 action history
        for item in data.get("actions_history", []):
            action = item.get("action", "")
            result = item.get("result", "")[:300]  # 截断长结果
            success = item.get("success", False)
            t = item.get("time", "")

            text = f"[执行] {action}\n[结果] {'成功' if success else '失败'}: {result}"
            if self.add_memory(text, category="action_result", source="openclaw",
                               metadata={"success": success, "original_time": t}):
                migrated += 1

        # 迁移 strategies
        strategy = data.get("current_strategy", "")
        if strategy and strategy != "初步市场调研":
            if self.add_memory(f"[策略] {strategy}", category="strategy", source="brain"):
                migrated += 1

        # 迁移 successful patterns
        for pattern in data.get("successful_patterns", []):
            if self.add_memory(f"[成功模式] {pattern}", category="pattern", source="system"):
                migrated += 1

        # 迁移 failed attempts
        for fail in data.get("failed_attempts", []):
            if self.add_memory(f"[失败记录] {fail}", category="error", source="system"):
                migrated += 1

        # 迁移 milestones
        for ms in data.get("milestones", []):
            desc = ms.get("description", "")
            if self.add_memory(f"[里程碑] {desc}", category="milestone", source="system"):
                migrated += 1

        if migrated > 0:
            print(f"[VECTOR] Migrated {migrated} memories from {json_file}")

        return migrated


# ===================== 便捷函数 =====================

_default_instance = None


def get_vector_memory() -> VectorMemory:
    """获取全局单例 VectorMemory"""
    global _default_instance
    if _default_instance is None:
        _default_instance = VectorMemory()
    return _default_instance


def search_memory(query: str, n_results: int = 5, verified_first: bool = True) -> list[dict]:
    """快捷搜索记忆"""
    return get_vector_memory().search(query, n_results, verified_first=verified_first)


def add_memory(text: str, category: str = "general", source: str = "system",
               verified: bool = False, metadata: Optional[dict] = None) -> bool:
    """快捷添加记忆"""
    return get_vector_memory().add_memory(text, category, source, metadata, verified)


def verify_memory(text: str, verified: bool = True) -> bool:
    """快捷标记/取消已验证记忆"""
    return get_vector_memory().verify_memory(text, verified)


def format_search_results(results: list[dict]) -> str:
    """将搜索结果格式化为文本，用于注入 Brain 上下文。已验证记忆标注 [已验证]"""
    if not results:
        return "(无相关历史记忆)"

    lines = ["## 相关历史记忆（语义检索）"]
    for i, m in enumerate(results, 1):
        rel = m.get("relevance", 0)
        cat = m.get("category", "")
        source = m.get("source", "")
        verified = m.get("verified", False)
        text = m.get("text", "")[:300]
        tag = "[已验证]" if verified else ""
        lines.append(f"{i}. [{cat}]{tag} (相关度:{rel}) {text}")

    return "\n".join(lines)
