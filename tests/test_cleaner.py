"""正文清洗测试（§11.3）：HTML 深度清洗、内联图片、噪音过滤。"""

from __future__ import annotations

import pytest

from src.cleaner import (
    clean_html,
    compose_body,
    dedupe_paragraphs,
    html_to_markdown,
    html_to_text,
    strip_noise,
    strip_noise_markdown,
)


class TestCleanHtml:
    def test_removes_script_and_style(self) -> None:
        html = "<html><head><style>p{color:red}</style></head><body><script>alert(1)</script><p>正文</p></body></html>"
        result = clean_html(html)
        assert "alert" not in result
        assert "color:red" not in result
        assert "正文" in result

    def test_removes_tracking_pixel_by_dimension(self) -> None:
        html = '<p>你好</p><img src="http://x.com/a.gif" width="1" height="1">'
        assert "<img" not in clean_html(html)

    def test_removes_hidden_elements(self) -> None:
        html = '<div style="display:none">隐藏内容</div><p>可见</p>'
        result = clean_html(html)
        assert "隐藏内容" not in result
        assert "可见" in result

    def test_removes_hidden_attribute(self) -> None:
        assert "秘密" not in clean_html('<span hidden>秘密</span><p>公开</p>')

    def test_removes_html_comments(self) -> None:
        assert "跟踪注释" not in clean_html("<p>正文</p><!-- 跟踪注释 -->")

    def test_removes_event_handlers(self) -> None:
        html = '<a href="http://x.com" onclick="steal()">链接</a>'
        result = clean_html(html)
        assert "onclick" not in result
        assert "链接" in result

    def test_cid_replaced_with_local_path(self) -> None:
        html = '<img src="cid:logo@corp" width="200" height="60">'
        result = clean_html(html, {"logo@corp": "attachments/logo.png"})
        assert "attachments/logo.png" in result
        assert "cid:" not in result

    def test_unresolved_cid_image_removed(self) -> None:
        html = '<p>文字</p><img src="cid:unknown@corp">'
        result = clean_html(html, {})
        assert "<img" not in result
        assert "文字" in result

    def test_removes_dangerous_tags(self) -> None:
        html = '<iframe src="http://evil"></iframe><object></object><embed><p>安全</p>'
        result = clean_html(html)
        for tag in ("iframe", "object", "embed"):
            assert f"<{tag}" not in result
        assert "安全" in result


class TestHtmlToMarkdown:
    def test_basic_conversion(self) -> None:
        md = html_to_markdown("<p>第一段</p><p>第二段</p>")
        assert "第一段" in md and "第二段" in md

    def test_bold_and_links(self) -> None:
        md = html_to_markdown('<p><b>重点</b> 见 <a href="http://x.com">链接</a></p>')
        assert "**重点**" in md
        assert "http://x.com" in md

    def test_lists(self) -> None:
        md = html_to_markdown("<ul><li>甲</li><li>乙</li></ul>")
        assert "- 甲" in md and "- 乙" in md

    def test_empty_input(self) -> None:
        assert html_to_markdown("") == ""
        assert html_to_markdown("<html></html>").strip() == ""


class TestHtmlToText:
    def test_strips_tags(self) -> None:
        assert "<p>" not in html_to_text("<p>内容</p>")

    def test_br_becomes_newline(self) -> None:
        assert "\n" in html_to_text("第一行<br>第二行")


class TestStripNoise:
    def test_cuts_rfc3676_signature(self) -> None:
        text = "正文内容\n\n-- \n张三\n销售部\n电话123"
        assert "销售部" not in strip_noise(text)
        assert "正文内容" in strip_noise(text)

    def test_cuts_chinese_quote_header(self) -> None:
        text = "好的，收到。\n\n在 2024年3月1日 10:00，李四 <li@x.com> 写道：\n> 原始内容"
        result = strip_noise(text)
        assert "好的，收到。" in result
        assert "原始内容" not in result

    def test_cuts_english_quote_header(self) -> None:
        text = "Thanks.\n\nOn Mon, Mar 1, 2024 at 10:00 AM Alice <a@x.com> wrote:\n> old"
        result = strip_noise(text)
        assert "Thanks." in result
        assert "old" not in result

    def test_cuts_outlook_original_message_marker(self) -> None:
        text = "答复见上。\n\n------------------ 原始邮件 ------------------\n发件人: 王五"
        result = strip_noise(text)
        assert "答复见上。" in result
        assert "王五" not in result

    def test_cuts_legal_disclaimer(self) -> None:
        text = "项目已上线。\n本邮件（含附件）可能包含保密信息，如果您不是指定的收件人请删除。"
        assert "保密信息" not in strip_noise(text)

    def test_removes_quoted_lines(self) -> None:
        assert "> 引用" not in strip_noise("正文\n> 引用\n更多正文")

    def test_cuts_mobile_signature(self) -> None:
        assert "iPhone" not in strip_noise("已收到。\n\n发自我的 iPhone")

    def test_preserves_legitimate_from_field_near_top(self) -> None:
        """正文开头的「发件人:」不应误伤（只在后 85% 区域找噪音标记）。"""
        text = "发件人: 这是正文里正常提到的内容，请勿删除。\n后面还有很多正文。" * 6
        result = strip_noise(text)
        assert len(result) > 100

    def test_empty_input(self) -> None:
        assert strip_noise("") == ""
        assert strip_noise("   ") == ""

    def test_no_noise_marker_keeps_everything(self) -> None:
        text = "第一段。\n\n第二段。\n\n第三段。"
        assert strip_noise(text) == text


class TestStripNoiseMarkdown:
    def test_removes_markdown_blockquote_blocks(self) -> None:
        md = "我的回复\n\n> 对方之前说的内容\n\n结束语"
        result = strip_noise_markdown(md)
        assert "对方之前说的内容" not in result
        assert "我的回复" in result


class TestComposeBody:
    def test_prefers_html_over_plain(self) -> None:
        md, plain = compose_body("纯文本版", "<p>HTML 版本</p>")
        assert "HTML 版本" in md

    def test_falls_back_to_plain(self) -> None:
        md, plain = compose_body("纯文本内容", "")
        assert "纯文本内容" in md and "纯文本内容" in plain

    def test_strips_noise_from_both(self) -> None:
        html = "<p>正文</p><p>-- </p><p>签名部门</p>"
        md, _ = compose_body("", html)
        assert "正文" in md
        assert "签名部门" not in md

    def test_plain_is_derived_from_markdown(self) -> None:
        _, plain = compose_body("", "<p><b>加粗</b>文字</p>")
        assert "**" not in plain
        assert "加粗文字" in plain

    def test_empty_both(self) -> None:
        md, plain = compose_body("", "")
        assert md == "" and plain == ""

    def test_full_pipeline_chinese_business_email(self) -> None:
        """真实场景回归：一份带追踪像素、内联图、签名、免责声明的中文商务邮件。"""
        html = """
        <html><head><style>.x{}</style></head><body>
        <script>track()</script>
        <p>张经理，您好：</p>
        <p>附件是本月对账单，请查收。</p>
        <img src="cid:sign@corp" width="180" height="50">
        <img src="https://mail.corp.com/open.aspx?id=9" width="1" height="1">
        <p>-- </p>
        <p>李四 | 财务部 | 分机 8021</p>
        <div>在 2024年2月28日 09:15，张经理 &lt;zhang@corp.com&gt; 写道：</div>
        <blockquote><p>请把对账单发我一份。</p></blockquote>
        <p>本邮件（含附件）可能包含保密信息，如果您不是指定的收件人请立即通知发件人并删除本邮件。</p>
        </body></html>
        """
        md, plain = compose_body("", html, {"sign@corp": "attachments/sign.png"})
        assert "张经理，您好" in md
        assert "本月对账单" in md
        assert "attachments/sign.png" in md
        assert "open.aspx" not in md
        assert "track()" not in md
        assert "李四" not in md
        assert "保密信息" not in md
        assert "请把对账单发我一份" not in md
        assert "**" not in plain


class TestDedupeParagraphs:
    def test_removes_repeated_paragraphs(self) -> None:
        text = "段落一\n\n段落二\n\n段落一"
        assert dedupe_paragraphs(text).count("段落一") == 1

    def test_keeps_distinct(self) -> None:
        text = "甲\n\n乙\n\n丙"
        assert dedupe_paragraphs(text) == text


class TestLayoutTableUnwrapping:
    """HTML 邮件几乎都用嵌套表格排版，必须脱壳，否则正文全是 `| --- |`。

    实测背景：某封真实邮件的切片 1022 字符里有 243 个 `|`，
    全是排版脚手架，会严重稀释向量语义。
    """

    def test_single_cell_table_unwrapped(self) -> None:
        html = "<table><tr><td>正文内容</td></tr></table>"
        md = html_to_markdown(html)
        assert "正文内容" in md
        assert "|" not in md

    def test_nested_layout_tables_unwrapped(self) -> None:
        """多层嵌套的单格表格是典型邮件排版结构。"""
        html = (
            "<table><tr><td>"
            "<table><tr><td>"
            "<table><tr><td>深层内容</td></tr></table>"
            "</td></tr></table>"
            "</td></tr></table>"
        )
        md = html_to_markdown(html)
        assert "深层内容" in md
        assert "|" not in md

    def test_th_alone_does_not_make_it_a_data_table(self) -> None:
        """回归：邮件模板常拿 <th> 当排版单元格。

        早期判据把"有 <th>"当作数据表标志，恰好保护住了最该拆的排版表。
        """
        html = (
            "<table><tr><th></th><th></th></tr>"
            "<tr><th>Welcome aboard 张三</th><th></th></tr></table>"
        )
        md = html_to_markdown(html)
        assert "Welcome aboard 张三" in md
        assert "|" not in md

    def test_sparse_wide_table_unwrapped(self) -> None:
        """宽但几乎全是空格的表格也是排版表。"""
        html = (
            "<table>"
            "<tr><td></td><td></td><td></td><td></td><td></td></tr>"
            "<tr><td></td><td>唯一内容</td><td></td><td></td><td></td></tr>"
            "</table>"
        )
        md = html_to_markdown(html)
        assert "唯一内容" in md
        assert "|" not in md

    def test_real_data_table_preserved(self) -> None:
        """真实数据表必须保留表格结构 —— 不能为了去噪把有用信息也拆了。"""
        html = (
            "<table>"
            "<tr><th>项目</th><th>数量</th><th>金额</th></tr>"
            "<tr><td>螺栓</td><td>120</td><td>340.00</td></tr>"
            "<tr><td>法兰</td><td>8</td><td>1200.00</td></tr>"
            "</table>"
        )
        md = html_to_markdown(html)
        assert "|" in md, "三列两行的真实数据表应当保留表格结构"
        assert "螺栓" in md and "120" in md and "340.00" in md
        assert "项目" in md and "金额" in md

    def test_two_column_data_table_unwrapped(self) -> None:
        """两列表格在邮件里基本都是排版，按判据脱壳。"""
        html = (
            "<table>"
            "<tr><td>标签</td><td>值</td></tr>"
            "<tr><td>姓名</td><td>张三</td></tr>"
            "</table>"
        )
        md = html_to_markdown(html)
        assert "标签" in md and "张三" in md
        assert "|" not in md

    def test_scaffolding_lines_removed_by_normalize(self) -> None:
        """残留的纯脚手架行（只有 | 和 -）应被清掉。"""
        from src.cleaner import normalize_markdown

        raw = "正文\n\n|  |  |  |\n| --- | --- | --- |\n\n更多正文"
        out = normalize_markdown(raw)
        assert "---" not in out
        assert "正文" in out and "更多正文" in out

    def test_empty_table_removed(self) -> None:
        md = html_to_markdown("<table><tr><td></td></tr></table><p>有内容</p>")
        assert "有内容" in md
        assert "|" not in md

    def test_deeply_nested_terminates(self) -> None:
        """异常深的结构不能导致死循环。"""
        html = "<table><tr><td>" * 300 + "内层" + "</td></tr></table>" * 300
        md = html_to_markdown(html)
        assert "内层" in md


class TestForwardedMailBodyRecovery:
    """回归：转发邮件的正文曾被整个删光。

    真实案例：一封转发邮件的 HTML 里，``**发件人：**值**发送时间：**值
    ...**主题：**值正文`` 全部挤在**同一行**，最后一个字段（主题）的值
    直接连着正文、没有分隔符。早期的规则"丢弃所有以 > 开头的块"
    导致 615 字符正文被删成 0，归档结果只剩「(此邮件无正文内容)」。
    """

    SUBJECT = "转发: XCL-ED2-GYGC-178 宁波中金石化85000空分装置初步技术附件审查意见"

    def _html(self) -> str:
        return (
            "<blockquote>"
            "<div><b>发件人：</b> 柴平海</div>"
            "<div><b>发送时间：</b> 2022-09-01 15:56</div>"
            "<div><b>收件人：</b> 张海峰</div>"
            "<div><b>主题：</b> XCL-ED2-GYGC-178 宁波中金石化85000空分装置初步技术附件审查意见"
            "各位领导好，附件为审查意见，请查收。</div>"
            "<div>宁波中金石化有限公司 轻烃事业部</div>"
            "</blockquote>"
        )

    def test_body_recovered_from_single_line_metadata(self) -> None:
        md, plain = compose_body("", self._html(), subject=self.SUBJECT)
        assert "各位领导好" in plain, "转发邮件的实际正文不能丢"
        assert "请查收" in plain
        assert md.strip() != ""

    def test_metadata_fields_removed(self) -> None:
        _, plain = compose_body("", self._html(), subject=self.SUBJECT)
        for field in ("发件人：", "发送时间：", "收件人："):
            assert field not in plain, f"元数据字段 {field} 应当被剔除"

    def test_subject_prefix_variants(self) -> None:
        """正文里的主题值不带「转发:」前缀，必须能匹配上。"""
        from src.cleaner import _cut_after_subject, _strip_reply_prefixes

        assert _strip_reply_prefixes("转发: 测试") == "测试"
        assert _strip_reply_prefixes("Re: Fwd: 测试") == "测试"
        assert _strip_reply_prefixes("回复：测试") == "测试"
        assert _strip_reply_prefixes("普通主题") == "普通主题"
        assert _cut_after_subject("测试主题正文内容", "转发: 测试主题").startswith("正文内容")

    def test_normal_email_unaffected(self) -> None:
        """常规邮件不能因为安全阀而保留噪音。"""
        html = (
            "<p>正事如下：</p><p>请于本周五前反馈。</p>"
            "<p>-- </p><p>张三 | 销售部</p>"
        )
        _, plain = compose_body("", html, subject="周会安排")
        assert "请于本周五前反馈" in plain
        assert "张三" not in plain, "签名仍应被剔除"

    def test_safety_valve_keeps_content_when_over_filtered(self) -> None:
        """通用安全阀：过滤把正文删到只剩零头时必须回退。"""
        from src.cleaner import _body_mostly_lost

        long_text = "这是一段足够长的正文内容。" * 10
        assert _body_mostly_lost(long_text, "") is True
        assert _body_mostly_lost(long_text, "短") is True
        assert _body_mostly_lost(long_text, long_text) is False
        # 短邮件不触发（本来就没什么可删）
        assert _body_mostly_lost("短", "") is False

    def test_plain_text_forward_also_recovers(self) -> None:
        """纯文本形式的转发邮件同样要能恢复正文。"""
        plain_src = (
            "\n\n发件人： 柴平海\n发送时间： 2022-09-01 15:56\n"
            "收件人： 张海峰\n主题： 某技术附件审查意见\n"
            "各位领导好，附件为审查意见，请查收。\n"
            "宁波中金石化有限公司 轻烃事业部\n"
        )
        md, plain = compose_body(plain_src, "", subject="转发: 某技术附件审查意见")
        assert "各位领导好" in plain
        assert "请查收" in plain
