"""邮件正文清洗：HTML 深度清洗 + 噪音过滤 + Markdown 转换（§11.3）。

三大职责
--------
1. :func:`clean_html` —— 移除 ``<script>``/``<style>``/隐藏元素/1x1 追踪像素，
   并把 ``cid:`` 内联图片引用替换为本地相对路径。
2. :func:`html_to_markdown` / :func:`html_to_text` —— 正文格式转换。
3. :func:`strip_noise` —— 切除签名区、历史回复引用、法务免责声明，
   防止污染向量空间。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---- 可选依赖 -------------------------------------------------------------

try:
    from bs4 import BeautifulSoup, Comment, Tag  # type: ignore

    BS4_AVAILABLE = True
except Exception:  # noqa: BLE001 pragma: no cover
    BeautifulSoup = None  # type: ignore
    Comment = None  # type: ignore
    Tag = None  # type: ignore
    BS4_AVAILABLE = False

try:
    from markdownify import markdownify as _markdownify  # type: ignore

    MARKDOWNIFY_AVAILABLE = True
except Exception:  # noqa: BLE001 pragma: no cover
    _markdownify = None  # type: ignore
    MARKDOWNIFY_AVAILABLE = False


# 需要整棵移除的标签
_DROP_TAGS = (
    "script", "style", "head", "meta", "link", "title", "noscript",
    "iframe", "frame", "frameset", "object", "embed", "applet",
    "form", "input", "button", "select", "textarea",
    "svg", "canvas", "video", "audio", "base",
    # Office / 邮件客户端专有标签
    "o:smarttagtype", "xml", "v:shapetype", "v:shape", "w:worddocument",
)

_HIDDEN_STYLE_RE = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:[;\s]|$)"
    r"|max-height\s*:\s*0|font-size\s*:\s*0|mso-hide\s*:\s*all)",
    re.IGNORECASE,
)
_HIDDEN_ATTR_VALUES = {"hidden", "none", "false", "0"}


def _is_tracking_pixel(tag: Any) -> bool:
    """识别 1x1 / 0x0 追踪像素与隐形图片。"""
    if tag.name != "img":
        return False
    def _dim(name: str) -> int | None:
        raw = tag.get(name)
        if raw is None:
            return None
        m = re.search(r"\d+", str(raw))
        return int(m.group()) if m else None

    width, height = _dim("width"), _dim("height")
    if width is not None and height is not None and width <= 1 and height <= 1:
        return True
    style = str(tag.get("style") or "")
    if _HIDDEN_STYLE_RE.search(style):
        return True
    src = str(tag.get("src") or "").lower()
    if any(k in src for k in ("open.aspx", "track", "beacon", "pixel.gif", "1x1.gif",
                              "spacer.gif", "blank.gif", "transparent.gif")):
        return True
    return False


def clean_html(html: str, cid_map: dict[str, str] | None = None) -> str:
    """深度清洗 HTML，返回安全、精简的 HTML 字符串。

    :param cid_map: ``{content-id: 本地相对路径}``，用于替换 ``cid:`` 引用。
    """
    if not html:
        return ""
    if not BS4_AVAILABLE:
        logger.debug("未安装 beautifulsoup4，退化为正则清洗")
        return _clean_html_regex(html, cid_map)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001
        logger.exception("HTML 解析失败，退化为正则清洗")
        return _clean_html_regex(html, cid_map)

    # 1) 移除危险/无用标签
    for name in _DROP_TAGS:
        for tag in soup.find_all(name):
            tag.decompose()

    # 2) 移除 HTML 注释（常见于条件注释与追踪代码）
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # 3) 移除隐藏元素与追踪像素
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None:
            continue
        style = str(tag.get("style") or "")
        if style and _HIDDEN_STYLE_RE.search(style):
            tag.decompose()
            continue
        if tag.has_attr("hidden"):
            tag.decompose()
            continue
        if _is_tracking_pixel(tag):
            tag.decompose()

    # 4) 清理事件处理器与危险属性
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            low = attr.lower()
            if low.startswith("on") or low in ("srcdoc", "formaction", "xlink:href"):
                del tag.attrs[attr]

    # 5) cid: 内联图片 -> 本地相对路径
    for img in soup.find_all("img"):
        src = str(img.get("src") or "").strip()
        if not src:
            img.decompose()
            continue
        if src.lower().startswith("cid:"):
            key = src[4:].strip().strip("<>")
            local = (cid_map or {}).get(key)
            if local:
                img["src"] = local
            else:
                img.decompose()
                continue
        elif src.lower().startswith(("http://", "https://")):
            # 外部图片：保留（markdownify 会转成 ![]()），但去掉可能的 base64 巨型内联
            pass
        elif src.startswith("data:image"):
            if len(src) > 4096:
                img.decompose()
                continue

    return str(soup)


def _clean_html_regex(html: str, cid_map: dict[str, str] | None) -> str:
    """无 bs4 时的降级实现。"""
    out = re.sub(r"(?is)<(script|style|head|noscript|iframe|object|embed)\b.*?</\1>", " ", html)
    out = re.sub(r"(?s)<!--.*?-->", " ", out)
    out = re.sub(r'(?is)<img\b[^>]*?(?:width\s*=\s*["\']?[01]["\']?)[^>]*?>', " ", out)
    out = re.sub(r"(?i)\son\w+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", out)

    def _cid_sub(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        local = (cid_map or {}).get(key)
        return f'src="{local}"' if local else 'data-removed="1"'

    out = re.sub(r'(?i)src\s*=\s*["\']cid:([^"\']+)["\']', _cid_sub, out)
    return out


# ---------------------------------------------------------------------------
# HTML -> Markdown / Text
# ---------------------------------------------------------------------------

def html_to_markdown(html: str, cid_map: dict[str, str] | None = None) -> str:
    """HTML -> Markdown（含深度清洗）。"""
    if not html or not html.strip():
        return ""
    cleaned = clean_html(html, cid_map)
    if not cleaned.strip():
        return ""

    if MARKDOWNIFY_AVAILABLE:
        try:
            # 注意：markdownify 不允许同时指定 strip 与 convert。
            # 这里用 convert 白名单——未列出的标签（span/font 等）会被"脱壳"保留文字。
            md = _markdownify(
                cleaned,
                heading_style="ATX",
                bullets="-",
                convert=["p", "br", "b", "strong", "i", "em", "u", "a", "img",
                         "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                         "blockquote", "pre", "code", "table", "tr", "td", "th", "hr"],
            )
            return normalize_markdown(md)
        except Exception:  # noqa: BLE001
            logger.exception("markdownify 转换失败，退化为文本提取")

    return text_to_markdown_ish(html_to_text(cleaned))


def html_to_text(html: str) -> str:
    """HTML -> 纯文本（供 FTS 与切片使用）。"""
    if not html:
        return ""
    if BS4_AVAILABLE:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for br in soup.find_all("br"):
                br.replace_with("\n")
            for tag in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
                tag.append("\n")
            text = soup.get_text(separator="")
        except Exception:  # noqa: BLE001
            logger.exception("HTML 文本提取失败")
            text = re.sub(r"<[^>]+>", " ", html)
    else:
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", text)

    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def text_to_markdown_ish(text: str) -> str:
    """已清洗 HTML 转纯文本后的轻量 Markdown 化（保留段落）。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "\n\n".join(re.sub(r"\n+", "\n", p) for p in paragraphs)


def normalize_markdown(md: str) -> str:
    """统一 Markdown 空白与空链接。"""
    if not md:
        return ""
    out = md.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{4,}", "\n\n\n", out)
    out = re.sub(r"!\[\]\(\s*\)", "", out)  # 去掉无地址的图片
    out = re.sub(r"\[\]\(\s*\)", "", out)  # 去掉无文本无地址的链接
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ---------------------------------------------------------------------------
# 噪音过滤（§11.3）
# ---------------------------------------------------------------------------

# RFC 3676 签名分隔符
_SIG_SEP_RE = re.compile(r"^\s*--\s*$")
# 中文回复引用头：在 2024年1月1日 10:00，张三 <a@b.com> 写道：
_QUOTE_HEADER_CN = re.compile(
    r"^\s*(在\s*.{0,80}?\s*(写道|寫道)\s*[:：]?\s*)\s*$"
)
_QUOTE_HEADER_EN = re.compile(
    r"^\s*(On\s+.{0,80}?\s+wrote\s*[:：]?\s*)\s*$", re.IGNORECASE
)
# 中文邮件客户端引用头（Outlook/Foxmail）
_QUOTE_HEADER_ZH_CN = re.compile(
    r"^\s*-{2,}\s*(原始邮件|原始郵件|转发邮件|轉發郵件|Original Message)\s*-{2,}\s*$",
    re.IGNORECASE,
)
_QUOTE_FIELD_RE = re.compile(
    r"^\s*(发件人|寄件者|发送时间|發送時間|发送日期|收件人|收件者|抄送|主题|主旨|日期|"
    r"From|Sent|To|Cc|Subject|Date)\s*[:：]",
    re.IGNORECASE,
)
# 移动端签名
_MOBILE_SIG_RE = re.compile(
    r"^\s*(发自我的?\s*(iPhone|iPad|华为|小米|安卓|Android)|"
    r"Sent from my\s+\w+|Get Outlook for \w+|"
    r"此邮件由.{0,20}发送|从我的\s*\w+\s*发送)\s*$",
    re.IGNORECASE,
)
# 法务免责声明
_LEGAL_MARKERS = (
    "本邮件（含附件）可能包含保密信息",
    "本邮件及附件可能包含保密信息",
    "本邮件包含保密信息",
    "此邮件可能包含保密信息",
    "本邮件和任何附件均属机密",
    "本电子邮件及其附件可能含有保密信息",
    "如果您不是指定的收件人",
    "如果您不是本邮件的指定收件人",
    "请立即通知发件人并删除本邮件",
    "未经授权，禁止使用",
    "请考虑环境保护再打印",
    "请在打印前考虑是否需要",
    "this email and any files transmitted with it are confidential",
    "this message and any attachments are confidential",
    "if you are not the intended recipient",
    "please consider the environment before printing",
    "privileged and confidential",
)
# 分隔线（单独成行的 --- 或 ===，3 个以上）
_SEPARATOR_RE = re.compile(r"^\s*[-=_*]{3,}\s*$")
# 引用行
_QUOTED_LINE_RE = re.compile(r"^\s*>{1,}")


def _find_noise_cut(lines: list[str], min_keep_ratio: float = 0.15) -> int | None:
    """返回应当截断的行号（保留 ``[:cut]``）。

    只在邮件后 85% 区域寻找噪音标记，避免误伤正文开头提到的「发件人:」等内容。
    """
    n = len(lines)
    if n == 0:
        return None
    floor = max(int(n * min_keep_ratio), 0)
    candidates: list[int] = []

    for i in range(floor, n):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            continue

        if _SIG_SEP_RE.match(line):
            candidates.append(i)
            continue
        if _MOBILE_SIG_RE.match(line):
            candidates.append(i)
            continue
        if _QUOTE_HEADER_CN.match(line) or _QUOTE_HEADER_EN.match(line):
            candidates.append(i)
            continue
        if _QUOTE_HEADER_ZH_CN.match(line):
            candidates.append(i)
            continue

        lowered = stripped.lower()
        for marker in _LEGAL_MARKERS:
            if marker in lowered or marker in stripped:
                candidates.append(i)
                break
        else:
            # 「发件人:/From:」开头的引用块：需连续出现至少 2 个引用字段才认定
            if _QUOTE_FIELD_RE.match(line):
                window = [l.strip() for l in lines[i : i + 6] if l.strip()]
                hits = sum(1 for l in window if _QUOTE_FIELD_RE.match(l))
                if hits >= 2:
                    candidates.append(i)

    if not candidates:
        return None
    cut = min(candidates)
    return cut if cut > 0 else None


def strip_noise(text: str) -> str:
    """切除签名、历史引用与免责声明。

    保守策略：只有确认找到噪音标记才截断，否则原样返回。
    """
    if not text or not text.strip():
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cut = _find_noise_cut(lines)
    if cut is not None:
        lines = lines[:cut]

    # 移除单独成行的引用行与分隔线（保守：仅当整行就是引用/分隔线）
    cleaned: list[str] = []
    for line in lines:
        if _QUOTED_LINE_RE.match(line):
            continue
        if _SEPARATOR_RE.match(line) and len(line.strip()) >= 3:
            continue
        cleaned.append(line.rstrip())

    out = "\n".join(cleaned)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def strip_noise_markdown(md: str) -> str:
    """Markdown 版噪音过滤：在纯文本规则之外，额外处理引用块。"""
    if not md:
        return ""
    # Markdown 引用块 `> ...` 整块删除（历史回复）
    blocks = re.split(r"\n\s*\n", md)
    kept = [b for b in blocks if not b.lstrip().startswith(">")]
    return normalize_markdown(strip_noise("\n\n".join(kept)))


def dedupe_paragraphs(text: str) -> str:
    """去除连续重复段落（常见于邮件客户端重复渲染同一段内容）。"""
    if not text:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        key = re.sub(r"\s+", "", para).strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(para.strip())
    return "\n\n".join(out)


def compose_body(
    text_plain: str, html: str, cid_map: dict[str, str] | None = None
) -> tuple[str, str]:
    """生成 ``(markdown, plain_text)`` 正文对。

    优先使用 ``text/html``（结构更完整），退化到 ``text/plain``。
    两种来源都会经过噪音过滤。
    """
    markdown = ""
    if html and html.strip():
        markdown = html_to_markdown(html, cid_map)
        markdown = strip_noise_markdown(markdown)

    plain_source = text_plain or ""
    if not markdown.strip():
        # 没有可用 HTML，用 text/plain
        markdown = text_to_markdown_ish(strip_noise(plain_source))
    elif plain_source.strip():
        # 有 HTML 时，也检查 plain 是否包含 HTML 里没有的尾巴（少见），不做合并以免重复
        pass

    markdown = dedupe_paragraphs(normalize_markdown(markdown))
    plain = normalize_markdown(markdown)
    plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", plain)  # 去掉图片语法
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)  # 链接保留文字
    plain = re.sub(r"^#{1,6}\s*", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"[*_`]{1,3}", "", plain)
    plain = re.sub(r"[ \t]+", " ", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    return markdown, plain
