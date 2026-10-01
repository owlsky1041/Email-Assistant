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
from dataclasses import dataclass
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

    # 3.5) 排版表格脱壳
    _unwrap_layout_tables(soup)

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


def _unwrap_layout_tables(soup: Any) -> None:
    """把**纯排版用途**的表格脱壳，只保留里面的文字。

    HTML 邮件几乎都用嵌套表格排版（一层层单元格，每格一行字）。
    直接转 Markdown 会变成满屏 ``| --- |``：实测某封真实邮件的切片
    1022 个字符里有 243 个是 ``|``，这些脚手架会严重稀释向量语义。

    **判据（基于真实邮件实测调整）**

    只有同时满足下面三条才认定为"数据表"并保留表格结构：

      * 最宽行 ≥ 3 列
      * 至少 2 行
      * 空格子占比 < 0.5

    其余一律脱壳。特别注意**不能用 ``<th>`` 作为判据**：实测该邮件的
    排版表几乎都带 ``<th>``（模板用它规避默认样式），按 ``<th>`` 判断
    恰好会保护住最该拆的那批表。

    从外到内反复处理：外层脱壳后内层仍在文档中，下一轮继续。
    """
    if soup is None:
        return
    for _ in range(200):  # 嵌套层数上限，防止异常结构导致死循环
        target = None
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            cells = table.find_all(["td", "th"])
            if not rows or not cells:
                target = table
                break
            widest = max(
                (len(tr.find_all(["td", "th"], recursive=False)) for tr in rows),
                default=0,
            )
            empty_ratio = sum(1 for c in cells if not c.get_text(strip=True)) / len(cells)
            is_data_table = widest >= 3 and len(rows) >= 2 and empty_ratio < 0.5
            if not is_data_table:
                target = table
                break
        if target is None:
            return
        for tag in target.find_all(["tr", "td", "th", "tbody", "thead", "tfoot"]):
            tag.unwrap()
        target.unwrap()


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

    # 丢掉"纯脚手架"表格行：一行里除了 | 和 - 之外没有任何文字。
    # HTML 邮件的排版表格会残留大量这类行，它们不含任何语义，
    # 却会占掉大段 token 并稀释向量。
    kept: list[str] = []
    for line in out.split("\n"):
        stripped = line.strip()
        if stripped and "|" in stripped and not re.search(r"[^\s|\-:]", stripped):
            continue
        kept.append(line)
    out = "\n".join(kept)

    # 压缩只剩分隔符的表格骨架：连续多个 `| | |` 之间没有内容时合并
    out = re.sub(r"(?:\|[ \t]*)++(?=\n|$)", "|", out)
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


def _find_noise_cut(
    lines: list[str],
    *,
    tail_ratio: float = 0.3,
    strip_signature: bool = True,
    strip_quoted_history: bool = False,
    strip_legal: bool = True,
    min_tail_lines: int = 8,
) -> int | None:
    """返回应当截断的行号（保留 ``[:cut]``）。

    两个关键取舍
    ------------
    1. **默认保留引用/转发历史**（``strip_quoted_history=False``）。
       转发邮件里的历史内容往往是知识库最有价值的部分，
       早期实现把它当噪音删掉，属于数据丢失。
    2. **只在邮件末尾 ``tail_ratio`` 区域内寻找标记**（默认最后 30%）。
       早期把搜索范围放到后 85%，一旦签名或免责声明出现在转发历史
       中间，就会把它之后的全部内容一并截掉。
    """
    n = len(lines)
    if n == 0:
        return None
    # 比例窗口 + 绝对保底：短邮件（如 5 行）按比例算只剩一两行，
    # 签名根本落不进窗口。保底确保至少搜索末尾 min_tail_lines 行。
    floor = max(0, min(int(n * (1.0 - tail_ratio)), n - min_tail_lines))
    candidates: list[int] = []

    for i in range(floor, n):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            continue

        if strip_signature and _SIG_SEP_RE.match(line):
            candidates.append(i)
            continue
        if strip_signature and _MOBILE_SIG_RE.match(line):
            candidates.append(i)
            continue

        if strip_quoted_history:
            if _QUOTE_HEADER_CN.match(line) or _QUOTE_HEADER_EN.match(line):
                candidates.append(i)
                continue
            if _QUOTE_HEADER_ZH_CN.match(line):
                candidates.append(i)
                continue

        if strip_legal:
            lowered = stripped.lower()
            for marker in _LEGAL_MARKERS:
                if marker in lowered or marker in stripped:
                    candidates.append(i)
                    break
            else:
                if strip_quoted_history and _QUOTE_FIELD_RE.match(line):
                    # 「发件人:/From:」引用块：需连续出现至少 2 个引用字段才认定
                    window = [l.strip() for l in lines[i : i + 6] if l.strip()]
                    hits = sum(1 for l in window if _QUOTE_FIELD_RE.match(l))
                    if hits >= 2:
                        candidates.append(i)

    if not candidates:
        return None
    cut = min(candidates)
    return cut if cut > 0 else None


def strip_noise(
    text: str,
    *,
    strip_signature: bool = True,
    strip_quoted_history: bool = False,
    strip_legal: bool = True,
    tail_ratio: float = 0.3,
) -> str:
    """切除签名与免责声明。

    **默认不动引用/转发历史** —— 转发邮件中的历史内容属于用户资产，
    删掉就无法恢复。需要时可显式开启 ``strip_quoted_history``。
    """
    if not text or not text.strip():
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cut = _find_noise_cut(
        lines,
        tail_ratio=tail_ratio,
        strip_signature=strip_signature,
        strip_quoted_history=strip_quoted_history,
        strip_legal=strip_legal,
    )
    if cut is not None:
        lines = lines[:cut]

    # 移除单独成行的引用行与分隔线（仅当显式要求剔除引用历史时）
    cleaned: list[str] = []
    for line in lines:
        if strip_quoted_history and _QUOTED_LINE_RE.match(line):
            continue
        if _SEPARATOR_RE.match(line) and len(line.strip()) >= 3:
            continue
        cleaned.append(line.rstrip())

    out = "\n".join(cleaned)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()




#: 转发/回复头部的字段标记，形如 ``**发件人：**`` / ``**From:**``。
#: 注意真实邮件里这些字段常常**全部挤在同一行**，其值与正文连在一起，
#: 因此不能按行删除，必须逐个字段定位。
_QUOTE_FIELD_MARK = re.compile(
    r"\*\*\s*"
    r"(发件人|发送时间|发送日期|收件人|抄送|密送|主题|日期|"
    r"From|Sent|To|Cc|Bcc|Subject|Date)\s*[:：]\s*\*\*\s*",
    re.IGNORECASE,
)

#: 纯文本形式的字段行（无 ``**`` 包裹）
_QUOTE_META_LINE = re.compile(
    r"^\s*>?\s*(?:\*\*)?\s*"
    r"(发件人|发送时间|发送日期|收件人|抄送|密送|主题|日期|"
    r"From|Sent|To|Cc|Bcc|Subject|Date)\s*[:：]",
    re.IGNORECASE,
)


#: 转发/回复前缀，例如 ``转发:`` ``回复:`` ``Re:`` ``Fwd:``
_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:(?:转发|回复|答复|回覆|转|回)\s*[:：]|(?:re|fwd?|fw|aw|sv)\s*[:：])\s*",
    re.IGNORECASE,
)


def _strip_reply_prefixes(text: str) -> str:
    """剥掉主题上的转发/回复前缀（可叠加，如 ``Re: Fwd: xxx``）。"""
    prev = None
    while prev != text:
        prev = text
        text = _REPLY_PREFIX_RE.sub("", text).strip()
    return text


def _cut_after_subject(tail: str, subject: str) -> str:
    """从 ``tail`` 中切掉主题值，返回其后的内容（即正文本体）。

    正文里 ``主题：`` 的值通常**不带** ``转发:`` 前缀，而邮件头的 Subject
    带前缀，所以要先剥离前缀再匹配；仍匹配不上时用主题尾部若干字符兜底定位。
    """
    if not tail or not subject:
        return ""
    bare = _strip_reply_prefixes(subject)
    for candidate in (bare, subject):
        if not candidate:
            continue
        idx = tail.find(candidate)
        if idx >= 0:
            return tail[idx + len(candidate):].strip()
    # 兜底：主题可能被截断或含不可见字符，用尾部片段定位
    for size in (30, 20, 12):
        if len(bare) >= size:
            idx = tail.find(bare[-size:])
            if idx >= 0:
                return tail[idx + size:].strip()
    return ""


def _strip_quote_meta_lines(md: str, subject: str = "") -> str:
    """剔除转发头部的元数据字段，**保留其后的实际正文**。

    用于"安全阀"：常规噪音过滤把正文删得所剩无几时退化为这种保守清理。

    难点：真实邮件里 ``**发件人：**值**发送时间：**值...**主题：**值正文``
    全部挤在一行，最后一个字段（主题）的值直接连着正文，没有分隔符。
    这里用**邮件头里已知的主题**做锚点，精确切掉主题值，剩下的就是正文。
    """
    subject = (subject or "").strip()
    out: list[str] = []
    for line in md.split("\n"):
        if _QUOTE_FIELD_MARK.search(line):
            # re.split 带捕获组： [前缀, 字段名1, 值1, 字段名2, 值2, ..., 最后一个值]
            parts = _QUOTE_FIELD_MARK.split(line)
            prefix = parts[0]
            tail = parts[-1] if len(parts) >= 3 else ""
            line = f"{prefix} {_cut_after_subject(tail, subject)}".strip()
        # 去掉引用前缀但保留内容
        line = re.sub(r"^\s*>\s?", "", line)
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return normalize_markdown(text)


def _body_mostly_lost(original: str, cleaned: str) -> bool:
    """判断噪音过滤是否"删过头"。

    正文被删到只剩零头（或彻底为空）时，几乎一定是过滤规则误伤，
    而不是真的全是签名/引用。此时宁可保留噪音，也不能丢内容。
    """
    src = (original or "").strip()
    dst = (cleaned or "").strip()
    if len(src) < 40:
        return False          # 短邮件本来就没什么可删
    if not dst:
        return True
    return len(dst) < max(30, len(src) * 0.25)



def strip_noise_markdown(
    md: str,
    *,
    strip_signature: bool = True,
    strip_quoted_history: bool = False,
    strip_legal: bool = True,
    tail_ratio: float = 0.3,
) -> str:
    """Markdown 版噪音过滤。

    默认**保留引用块**：转发邮件的历史内容常整体位于引用块内，
    早期"丢弃所有以 > 开头的块"会把整封正文删光。
    """
    if not md:
        return ""
    text = md
    if strip_quoted_history:
        blocks = re.split(r"\n\s*\n", md)
        kept = []
        for block in blocks:
            lines = [ln for ln in block.split("\n") if ln.strip()]
            if lines and all(ln.lstrip().startswith(">") for ln in lines):
                continue
            kept.append(block)
        text = "\n\n".join(kept)
    return normalize_markdown(
        strip_noise(
            text,
            strip_signature=strip_signature,
            strip_quoted_history=strip_quoted_history,
            strip_legal=strip_legal,
            tail_ratio=tail_ratio,
        )
    )


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


@dataclass(slots=True)
class CleanPolicy:
    """清洗策略（与 config.CleanConfig 对应，避免 cleaner 依赖配置模块）。"""

    strip_signature: bool = True
    strip_quoted_history: bool = False
    strip_legal_disclaimer: bool = True
    noise_tail_ratio: float = 0.3


DEFAULT_POLICY = CleanPolicy()


def compose_body(
    text_plain: str,
    html: str,
    cid_map: dict[str, str] | None = None,
    subject: str = "",
    policy: "CleanPolicy | None" = None,
) -> tuple[str, str]:
    """生成 ``(markdown, plain_text)`` 正文对。

    优先使用 ``text/html``（结构更完整），退化到 ``text/plain``。
    两种来源都会经过噪音过滤。
    """
    policy = policy or DEFAULT_POLICY
    noise_kwargs = {
        "strip_signature": policy.strip_signature,
        "strip_quoted_history": policy.strip_quoted_history,
        "strip_legal": policy.strip_legal_disclaimer,
        "tail_ratio": policy.noise_tail_ratio,
    }

    markdown = ""
    if html and html.strip():
        markdown = html_to_markdown(html, cid_map)
        markdown = strip_noise_markdown(markdown, **noise_kwargs)

    plain_source = text_plain or ""
    if not markdown.strip():
        # 没有可用 HTML，用 text/plain
        markdown = text_to_markdown_ish(strip_noise(plain_source, **noise_kwargs))
    elif plain_source.strip():
        # 有 HTML 时，也检查 plain 是否包含 HTML 里没有的尾巴（少见），不做合并以免重复
        pass

    # 安全阀：噪音过滤不得把正文删光。
    # 转发邮件的正文常常整体位于引用块内，激进过滤会把它整封删掉。
    candidate = html_to_markdown(html, cid_map) if (html and html.strip()) else ""
    if not candidate.strip():
        candidate = text_to_markdown_ish(text_plain or "")
    if _body_mostly_lost(candidate, markdown):
        salvaged = _strip_quote_meta_lines(candidate, subject)
        if salvaged.strip():
            logger.debug("噪音过滤删除过多内容，已退化为保守清理")
            markdown = salvaged

    markdown = dedupe_paragraphs(normalize_markdown(markdown))
    plain = _markdown_to_plain(markdown)
    return markdown, plain


#: Markdown 反斜杠转义，例如 ``\_`` ``\*`` ``\[``
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+.!~>-])")


def _markdown_to_plain(markdown: str) -> str:
    """把 Markdown 正文降级为纯文本（用于 FTS / 向量切片）。

    要点：必须先「寄存」反斜杠转义再清理强调符号。
    否则 ``zj\\_zhangzhongxu@rong-sheng.com`` 会因为 ``_`` 被删掉而
    退化成 ``zj\\zhangzhongxu@rong-sheng.com``——邮箱地址、文件名里的
    下划线被破坏，检索也就搜不到了。
    """
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", markdown)  # 去掉图片语法
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接保留文字
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    # 引用层级标记：转发历史层层嵌套后会变成 "> > > > 正文"，
    # 保留下来只会污染检索片段，正文本身不受影响。
    text = re.sub(r"^(?:[ \t]*>)+[ \t]?", "", text, flags=re.MULTILINE)

    stash: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        stash.append(match.group(1))
        return f"\x00{len(stash) - 1}\x00"

    text = _MD_ESCAPE_RE.sub(_stash, text)
    text = re.sub(r"[*_`]{1,3}", "", text)  # 清理强调/代码符号
    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
