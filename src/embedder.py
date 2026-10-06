"""文本嵌入后端（§11.4 轻量化：优先 ONNX，避免完整 PyTorch 依赖）。

三个后端
--------
``onnx``
    生产首选。``onnxruntime`` + ``tokenizers``，依赖约 200MB，
    不引入 PyTorch。模型目录需包含 ``model.onnx`` 与 ``tokenizer.json``。
``sentence-transformers``
    兼容性最好但体积大（~2GB），适合已经装了 torch 的环境。
``hashing``
    纯 Python 兜底后端，**零依赖、离线可用**。它基于字符 n-gram 的
    哈希投影 + 次线性 TF 加权，属于词法相似度而非语义相似度——
    仅用于开发调试与 CI；生产环境请配置真实模型。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from .cancellation import CancellationToken, get_cancellation_token
from .config import AppConfig

logger = logging.getLogger(__name__)


class EmbedderError(RuntimeError):
    """嵌入后端不可用。"""


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class Embedder(ABC):
    """嵌入后端统一接口。"""

    name: str = "base"
    dimension: int = 0
    #: 该后端建议的最小余弦相似度。不同模型的相似度分布差异很大，
    #: 用一个全局魔数会在切换后端时悄悄改变召回行为。
    recommended_min_score: float = 0.0

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量编码文档。"""

    def embed_query(self, text: str) -> list[float]:
        """编码查询。默认与文档同构；子类可加查询前缀。"""
        vectors = self.embed([text])
        return vectors[0] if vectors else [0.0] * self.dimension

    @property
    def backend_id(self) -> str:
        return f"{self.name}:{self.dimension}"

    def health(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "model": self.name,
            "dimension": self.dimension,
            "recommended_min_score": self.recommended_min_score,
        }


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 1e-12:
        return vector
    return [v / norm for v in vector]


# ---------------------------------------------------------------------------
# 兜底后端：哈希嵌入
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class HashingEmbedder(Embedder):
    """无依赖的词法嵌入（开发/CI 兜底）。

    特征：CJK 单字 + 二元组、拉丁词、词二元组。
    用带符号的哈希把特征投影到固定维度，再做 L2 归一化。
    """

    name = "hashing"
    #: 实测哈希后端相关查询 ≈0.35-0.43、无关查询 ≤0.07，0.25 是安全分界
    recommended_min_score = 0.25

    def __init__(self, dimension: int = 512) -> None:
        self.dimension = max(64, int(dimension))

    def _features(self, text: str) -> Counter[str]:
        text = unicodedata.normalize("NFKC", text or "").lower()
        counts: Counter[str] = Counter()

        # 拉丁词
        words = _WORD_RE.findall(text)
        counts.update(words)
        counts.update(f"{a}_{b}" for a, b in zip(words, words[1:]))

        # CJK 单字与二元组
        cjk = _CJK_RE.findall(text)
        counts.update(cjk)
        counts.update(f"{a}{b}" for a, b in zip(cjk, cjk[1:]))
        return counts

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % self.dimension
        sign = 1.0 if (value >> 63) & 1 else -1.0
        return index, sign

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for feature, count in self._features(text).items():
                index, sign = self._bucket(feature)
                vector[index] += sign * (1.0 + math.log(count))
            out.append(_l2_normalize(vector))
        return out


# ---------------------------------------------------------------------------
# ONNX 后端
# ---------------------------------------------------------------------------

class OnnxEmbedder(Embedder):
    """ONNXRuntime 后端（推荐生产使用）。"""

    name = "onnx"
    #: bge 系列对同语种文本的相似度基线偏高（无关句对也能到 0.45），
    #: 0.35 是实测的噪音下限，再高会损失召回。
    recommended_min_score = 0.35

    def __init__(
        self,
        model_dir: str | Path,
        *,
        dimension: int = 0,
        query_prefix: str = "",
        max_length: int = 512,
        batch_size: int = 32,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.query_prefix = query_prefix
        self.max_length = max_length
        self.batch_size = max(1, batch_size)
        self.cancel = cancel_token or get_cancellation_token()

        try:
            import numpy as np  # type: ignore
            import onnxruntime as ort  # type: ignore
            from tokenizers import Tokenizer  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise EmbedderError(
                "ONNX 后端需要 onnxruntime / tokenizers / numpy，请执行：\n"
                "    pip install -r requirements-optional.txt"
            ) from exc

        self._np = np
        model_file = self._locate_model()
        tokenizer_file = self.model_dir / "tokenizer.json"
        if not tokenizer_file.is_file():
            raise EmbedderError(f"缺少分词器文件：{tokenizer_file}")

        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))
        self._tokenizer.enable_truncation(max_length=self.max_length)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = 0
        self._session = ort.InferenceSession(
            str(model_file), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._output_name = self._session.get_outputs()[0].name

        if dimension:
            self.dimension = dimension
        else:
            self.dimension = self._probe_dimension()

    def _locate_model(self) -> Path:
        for candidate in (
            self.model_dir / "model.onnx",
            self.model_dir / "model_quantized.onnx",
            self.model_dir / "onnx" / "model.onnx",
        ):
            if candidate.is_file():
                return candidate
        found = sorted(self.model_dir.glob("*.onnx")) if self.model_dir.is_dir() else []
        if found:
            return found[0]
        raise EmbedderError(
            f"在 {self.model_dir} 未找到 model.onnx。\n"
            "请先导出 ONNX 模型，例如：\n"
            "    optimum-cli export onnx --model BAAI/bge-small-zh-v1.5 <model_dir>"
        )

    def _probe_dimension(self) -> int:
        vector = self._run(["维度探测"])[0]
        return len(vector)

    def _run(self, texts: Sequence[str]) -> list[list[float]]:
        np = self._np
        encodings = self._tokenizer.encode_batch(list(texts))
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feeds: dict[str, object] = {}
        if "input_ids" in self._input_names:
            feeds["input_ids"] = ids
        if "attention_mask" in self._input_names:
            feeds["attention_mask"] = mask
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)

        outputs = self._session.run([self._output_name], feeds)
        hidden = np.asarray(outputs[0], dtype=np.float32)  # (B, T, H)

        # mean pooling（考虑 attention mask）
        mask_f = mask[..., None].astype(np.float32)
        summed = (hidden * mask_f).sum(axis=1)
        counts = np.clip(mask_f.sum(axis=1), 1e-9, None)
        pooled = summed / counts

        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        pooled = pooled / np.clip(norms, 1e-12, None)
        return pooled.tolist()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            self.cancel.raise_if_cancelled()
            batch = list(texts[start : start + self.batch_size])
            if not batch:
                continue
            out.extend(self._run(batch))
        return out

    def embed_query(self, text: str) -> list[float]:
        prefix = self.query_prefix or ""
        return self.embed([f"{prefix}{text}"])[0]

    def health(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "model_dir": str(self.model_dir),
            "dimension": self.dimension,
            "recommended_min_score": self.recommended_min_score,
        }


# ---------------------------------------------------------------------------
# SentenceTransformer 后端
# ---------------------------------------------------------------------------

class OllamaEmbedder(Embedder):
    """通过本机 Ollama 服务做嵌入。

    为什么单独做一个后端
    --------------------
    ONNX 后端要求用户事先把模型导出成 onnx；SentenceTransformer 要拖 PyTorch。
    而 Ollama 用户往往已经跑着本地模型（``nomic-embed-text`` / ``bge-m3`` 等），
    直接复用比再装一套运行时省事得多，而且是**纯 HTTP**，不引入任何新依赖。

    走 ``POST /api/embed``（新版）并回退到 ``POST /api/embeddings``（旧版），
    这样 0.x 各版本都能用。
    """

    name = "ollama"

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 60.0,
        batch_size: int = 16,
        query_prefix: str = "",
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self.model = model or "nomic-embed-text"
        self.base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")
        self.timeout = float(timeout)
        self.batch_size = max(1, int(batch_size))
        self.query_prefix = query_prefix or ""
        self.cancel = cancel_token or get_cancellation_token()
        self._batch_endpoint = "/api/embed"
        self.dimension = self._probe_dimension()

    # ---- HTTP ----

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import json
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise EmbedderError(f"Ollama 返回 HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise EmbedderError(
                f"连不上 Ollama（{self.base_url}）：{exc.reason}。"
                "请确认 `ollama serve` 正在运行。"
            ) from exc
        except OSError as exc:
            raise EmbedderError(f"请求 Ollama 失败：{exc}") from exc

    def _probe_dimension(self) -> int:
        """用一次真实请求确定维度 —— 不同模型差别很大，猜不得。"""
        payload = self._embed_vectors(["维度探测"])
        if not payload:
            raise EmbedderError(f"Ollama 模型 {self.model} 没有返回向量")
        return len(payload[0])

    def _embed_vectors(self, texts: Sequence[str]) -> list[list[float]]:
        """调 Ollama 取向量，自动适配新旧两套接口。"""
        if self._batch_endpoint == "/api/embed":
            try:
                data = self._post("/api/embed", {"model": self.model, "input": list(texts)})
                vectors = data.get("embeddings")
                if isinstance(vectors, list) and vectors:
                    return [[float(x) for x in v] for v in vectors]
                raise EmbedderError(f"Ollama /api/embed 响应缺少 embeddings：{str(data)[:150]}")
            except EmbedderError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                # 老版本 Ollama 没有 /api/embed，退回一次一个的旧接口
                self._batch_endpoint = "/api/embeddings"
                logger.info("Ollama 不支持 /api/embed，改用 /api/embeddings")

        out: list[list[float]] = []
        for text in texts:
            data = self._post(
                self._batch_endpoint, {"model": self.model, "prompt": text}
            )
            vector = data.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise EmbedderError(f"Ollama 响应缺少 embedding：{str(data)[:150]}")
            out.append([float(x) for x in vector])
        return out

    # ---- Embedder 接口 ----

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            self.cancel.raise_if_cancelled()
            batch = list(texts[start : start + self.batch_size])
            if not batch:
                continue
            out.extend(self._embed_vectors(batch))
        return out

    def embed_query(self, text: str) -> list[float]:
        prefix = self.query_prefix or ""
        vectors = self._embed_vectors([f"{prefix}{text}"])
        return vectors[0] if vectors else [0.0] * self.dimension

    def health(self) -> dict[str, object]:
        info = super().health()
        info.update({"model": self.model, "base_url": self.base_url})
        return info

    @property
    def backend_id(self) -> str:
        return f"ollama:{self.model}:{self.dimension}"


class SentenceTransformerEmbedder(Embedder):
    """sentence-transformers 后端（体积大，兼容性最好）。"""

    name = "sentence-transformers"
    recommended_min_score = 0.35

    def __init__(
        self,
        model: str = "BAAI/bge-small-zh-v1.5",
        *,
        query_prefix: str = "",
        device: str = "cpu",
        batch_size: int = 32,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise EmbedderError(
                "sentence-transformers 后端未安装，请执行：\n"
                "    pip install sentence-transformers"
            ) from exc

        self.model_name = model
        self.query_prefix = query_prefix
        self.batch_size = max(1, batch_size)
        self.cancel = cancel_token or get_cancellation_token()
        logger.info("正在加载嵌入模型 %s（设备 %s）…", model, device)
        self._model = SentenceTransformer(model, device=device)
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            self.cancel.raise_if_cancelled()
            batch = list(texts[start : start + self.batch_size])
            if not batch:
                continue
            vectors = self._model.encode(
                batch,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            out.extend(v.tolist() for v in vectors)
        return out

    def embed_query(self, text: str) -> list[float]:
        prefix = self.query_prefix or ""
        return self.embed([f"{prefix}{text}"])[0]

    def health(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "model": self.model_name,
            "dimension": self.dimension,
            "recommended_min_score": self.recommended_min_score,
        }


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def create_embedder(
    config: AppConfig, *, cancel_token: CancellationToken | None = None
) -> Embedder:
    """按配置创建嵌入后端，失败时按优先级降级并给出明确告警。"""
    cfg = config.embedding
    token = cancel_token or get_cancellation_token()
    preferred = cfg.backend
    attempts: list[str] = []

    if preferred in ("auto", "onnx", "sentence-transformers"):
        attempts.append("onnx")
    if preferred in ("auto", "sentence-transformers"):
        attempts.append("sentence-transformers")
    # ollama **不放进 auto 降级链**：它需要用户自己先跑起 ollama serve，
    # 自动去试只会在没装的人机器上白等一次超时。只有显式选择才用它。
    if preferred == "ollama":
        attempts.append("ollama")
    attempts.append("hashing")

    errors: list[str] = []
    for backend in attempts:
        try:
            if backend == "onnx":
                embedder = OnnxEmbedder(
                    config.model_path,
                    dimension=0,
                    query_prefix=cfg.query_prefix,
                    batch_size=cfg.batch_size,
                    cancel_token=token,
                )
                logger.info("嵌入后端：ONNX（%s，%d 维）", config.model_path, embedder.dimension)
                return embedder
            if backend == "ollama":
                embedder = OllamaEmbedder(
                    cfg.ollama_model,
                    base_url=cfg.ollama_url,
                    timeout=cfg.ollama_timeout,
                    batch_size=cfg.batch_size,
                    query_prefix=cfg.query_prefix,
                    cancel_token=token,
                )
                logger.info(
                    "嵌入后端：Ollama（%s @ %s，%d 维）",
                    cfg.ollama_model,
                    cfg.ollama_url,
                    embedder.dimension,
                )
                return embedder
            if backend == "sentence-transformers":
                embedder = SentenceTransformerEmbedder(
                    cfg.model,
                    query_prefix=cfg.query_prefix,
                    device=cfg.device,
                    batch_size=cfg.batch_size,
                    cancel_token=token,
                )
                logger.info("嵌入后端：SentenceTransformer（%s）", cfg.model)
                return embedder
            embedder = HashingEmbedder(dimension=cfg.dimension)
            if preferred != "hashing":
                hint = (
                    f"  * 检查 Ollama 是否在运行（{cfg.ollama_url}），"
                    f"以及模型是否已拉取：ollama pull {cfg.ollama_model}"
                    if preferred == "ollama"
                    else "  * 如需真实语义检索，请安装 requirements-optional.txt 并导出 ONNX 模型：\n"
                    f"      optimum-cli export onnx --model {cfg.model} {config.model_path}"
                )
                logger.warning(
                    "未能加载嵌入模型，已降级为 hashing 兜底后端。\n"
                    "  * 语义检索质量将显著下降（退化为词法相似度）；\n%s",
                    hint,
                )
                for err in errors:
                    logger.warning("  - 尝试失败：%s", err)
            return embedder
        except EmbedderError as exc:
            errors.append(f"{backend}: {exc}")
            logger.debug("嵌入后端 %s 不可用", backend, exc_info=True)
        except Exception as exc:  # noqa: BLE001 - 模型加载可能抛任意异常
            errors.append(f"{backend}: {exc}")
            logger.debug("嵌入后端 %s 初始化异常", backend, exc_info=True)

    raise EmbedderError("所有嵌入后端均不可用：" + "; ".join(errors))
