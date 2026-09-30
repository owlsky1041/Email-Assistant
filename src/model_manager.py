"""ONNX 嵌入模型的获取与管理。

为什么需要这个模块
------------------
运行时的 ONNX 推理**不需要 PyTorch**，但*导出* ONNX 需要。
如果让每个用户都自己装 900MB 的 torch 再导出，体验很差。

因此提供三条路径：

``model import <目录>``
    从已有目录复制（离线 / 内网环境，或从发行包解压）。
``model download --url <URL>``
    从发行包附件或内部镜像下载（zip 或裸文件均可）。
``model download --repo <HF仓库>``
    从 HuggingFace 仓库拉取（支持 ``--endpoint`` 走镜像）。

⚠️ 关于公开的 ONNX 仓库
    社区上的第三方 ONNX 导出**池化方式可能与 sentence-transformers 不一致**，
    实测 ``Xenova/bge-small-zh-v1.5`` 的相关/无关句对相似度几乎不可分
    （0.287 vs 0.283）。因此默认推荐用 `model import` 导入由本项目
    自己导出的模型，或下载发行包里附带的版本。下载第三方仓库后
    请务必用 ``scripts/calibrate_threshold.py`` 验证区分度。
"""

from __future__ import annotations

import logging
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

REQUIRED_FILES = ("model.onnx", "tokenizer.json")
OPTIONAL_FILES = (
    "config.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
)

DEFAULT_ENDPOINT = "https://huggingface.co"
MIRROR_ENDPOINT = "https://hf-mirror.com"

#: 已验证可用、但**区分度存疑**的社区导出，仅作备选
COMMUNITY_REPOS = {
    "xenova": "Xenova/bge-small-zh-v1.5",  # 池化方式与 ST 不一致，慎用
}

USER_AGENT = "email-assistant/0.1 (+model-manager)"


class ModelError(RuntimeError):
    """模型获取失败。"""


@dataclass(slots=True)
class ModelStatus:
    path: Path
    ready: bool
    files: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    size_bytes: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "ready": self.ready,
            "files": self.files,
            "missing": self.missing,
            "size_mb": round(self.size_bytes / 1024 / 1024, 1),
            "note": self.note,
        }


def check_model_dir(path: str | Path) -> ModelStatus:
    """检查模型目录是否可用。

    除了必需文件，还检测**外部数据文件**：部分社区导出把权重放在
    ``model.onnx_data`` 里，只复制 ``model.onnx`` 会在推理时才报错，
    排查成本很高，这里提前识别。
    """
    directory = Path(path)
    if not directory.is_dir():
        return ModelStatus(path=directory, ready=False, missing=list(REQUIRED_FILES),
                           note="目录不存在")

    present = sorted(p.name for p in directory.iterdir() if p.is_file())
    missing = [name for name in REQUIRED_FILES if not (directory / name).is_file()]
    size = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())

    note = ""
    ready = not missing
    if ready:
        onnx_file = directory / "model.onnx"
        # 外部数据：graph 很小但伴生 *_data 文件
        external = list(directory.glob("*.onnx_data")) + list(directory.glob("*.onnx.data"))
        if external:
            note = f"检测到外部权重文件：{[p.name for p in external]}（需一并拷贝）"
        elif onnx_file.stat().st_size < 5 * 1024 * 1024:
            ready = False
            note = (
                "model.onnx 体积异常小，可能是使用外部权重的导出。"
                "请确认同时提供了 .onnx_data 文件。"
            )
        else:
            note = "模型就绪"
    else:
        note = f"缺少必需文件：{missing}"

    return ModelStatus(
        path=directory, ready=ready, files=present, missing=missing, size_bytes=size, note=note
    )


def import_model(source: str | Path, target: str | Path, *, overwrite: bool = False) -> ModelStatus:
    """从目录或 zip 文件导入模型。"""
    src = Path(source)
    dst = Path(target)

    if not src.exists():
        raise ModelError(f"源路径不存在：{src}")

    if src.is_file():
        if src.suffix.lower() != ".zip":
            raise ModelError(f"不支持的源文件类型：{src.suffix}（只支持目录或 .zip）")
        if dst.exists() and not overwrite and any(dst.iterdir()):
            raise ModelError(f"目标目录非空：{dst}（使用 --force 覆盖）")
        dst.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            _safe_extract(zf, dst)
        logger.info("已从 zip 导入模型到 %s", dst)
        return check_model_dir(dst)

    if not src.is_dir():
        raise ModelError(f"源路径既不是目录也不是 zip：{src}")

    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in src.iterdir():
        if not item.is_file():
            continue
        if item.name in ("model.onnx", "tokenizer.json") or item.name in OPTIONAL_FILES \
                or item.suffix in (".onnx", ".data") or item.name.endswith(".onnx_data"):
            target_file = dst / item.name
            if target_file.exists() and not overwrite:
                continue
            shutil.copy2(item, target_file)
            copied += 1
    logger.info("已导入 %d 个模型文件到 %s", copied, dst)
    return check_model_dir(dst)


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """阻止 zip 路径穿越（Zip Slip）。"""
    root = destination.resolve()
    for member in archive.namelist():
        target = (destination / member).resolve()
        if not str(target).startswith(str(root)):
            raise ModelError(f"压缩包包含非法路径：{member}")
    archive.extractall(destination)
    # 压缩包若多套了一层目录，把文件提到顶层
    _flatten_single_dir(destination)


def _flatten_single_dir(directory: Path) -> None:
    entries = list(directory.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return
    inner = entries[0]
    if not any((inner / name).exists() for name in REQUIRED_FILES):
        return
    for item in inner.iterdir():
        shutil.move(str(item), str(directory / item.name))
    inner.rmdir()


def download_model(
    target: str | Path,
    *,
    url: str | None = None,
    repo: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    variant: str = "model.onnx",
    overwrite: bool = False,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> ModelStatus:
    """下载模型。

    :param url: 直接下载地址（zip 或裸 model.onnx）。给了 zip 会自动解压。
    :param repo: HuggingFace 仓库名，例如 ``onnx-community/xxx-ONNX``。
    :param endpoint: 端点，国内建议 ``https://hf-mirror.com``。
    """
    dst = Path(target)
    dst.mkdir(parents=True, exist_ok=True)

    if url:
        return _download_from_url(url, dst, overwrite=overwrite, on_progress=on_progress)

    if not repo:
        raise ModelError("必须提供 --url 或 --repo 之一")

    base = f"{endpoint.rstrip('/')}/{repo}/resolve/main/"
    plan = [f"onnx/{variant}", "tokenizer.json", *OPTIONAL_FILES]
    for remote in plan:
        local = dst / Path(remote).name
        if local.exists() and not overwrite and local.stat().st_size > 0:
            logger.info("已存在，跳过：%s", local.name)
            continue
        try:
            _fetch(base + remote, local, on_progress)
        except urllib.error.HTTPError as exc:
            if remote in OPTIONAL_FILES or exc.code == 404:
                logger.debug("可选文件不可用：%s（%s）", remote, exc.code)
                continue
            raise ModelError(
                f"下载失败 {remote}：HTTP {exc.code}。"
                f"若在中国大陆，可加 --endpoint {MIRROR_ENDPOINT}"
            ) from exc

    status = check_model_dir(dst)
    if status.ready and repo in COMMUNITY_REPOS.values():
        status.note += (
            "  ⚠️ 这是社区导出，池化方式可能与 sentence-transformers 不一致，"
            "请用 scripts/calibrate_threshold.py 验证区分度后再用于生产。"
        )
    return status


def _download_from_url(
    url: str,
    dst: Path,
    *,
    overwrite: bool,
    on_progress: Callable[[str, int, int], None] | None,
) -> ModelStatus:
    filename = url.split("?")[0].rstrip("/").split("/")[-1] or "model.onnx"
    is_zip = filename.lower().endswith(".zip")
    local = dst / filename

    if local.exists() and not overwrite and local.stat().st_size > 0:
        logger.info("已存在，跳过下载：%s", local)
    else:
        try:
            _fetch(url, local, on_progress)
        except urllib.error.HTTPError as exc:
            raise ModelError(f"下载失败：HTTP {exc.code} {url}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"网络不可达：{exc.reason}") from exc

    if is_zip:
        with zipfile.ZipFile(local) as zf:
            _safe_extract(zf, dst)
        local.unlink(missing_ok=True)
    return check_model_dir(dst)


def _fetch(
    url: str,
    destination: Path,
    on_progress: Callable[[str, int, int], None] | None = None,
    *,
    timeout: int = 120,
) -> None:
    """带 .tmp 原子写入的下载（断网不会留下半个文件）。"""
    tmp = destination.with_name(destination.name + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            with open(tmp, "wb") as fh:
                while True:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    fh.write(block)
                    received += len(block)
                    if on_progress:
                        on_progress(destination.name, received, total)
        if total and tmp.stat().st_size != total:
            raise ModelError(
                f"下载不完整：期望 {total} 字节，实际 {tmp.stat().st_size} 字节"
            )
        tmp.replace(destination)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def resolve_endpoint(explicit: str | None = None) -> str:
    """确定 HF 端点。支持 ``HF_ENDPOINT`` 环境变量。"""
    import os

    if explicit:
        return explicit
    from_env = os.environ.get("HF_ENDPOINT")
    if from_env:
        return from_env.rstrip("/")
    return DEFAULT_ENDPOINT
