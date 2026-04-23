"""Tests for md_to_telegram_html() and the parse_mode='HTML' path in send_long_message."""

from unittest.mock import AsyncMock, MagicMock


class TestMdToTelegramHtml:
    def html(self, md: str) -> str:
        from shared import md_to_telegram_html

        return md_to_telegram_html(md)

    def test_bold(self):
        assert "<b>hi</b>" in self.html("**hi**")

    def test_italic(self):
        assert "<i>hi</i>" in self.html("*hi*")

    def test_inline_code(self):
        assert "<code>x</code>" in self.html("`x`")

    def test_fenced_code_block(self):
        result = self.html("```python\nprint('hi')\n```")
        assert "<pre><code>" in result
        assert "print(&#x27;hi&#x27;)" in result or "print('hi')" in result
        assert "</code></pre>" in result

    def test_heading_becomes_bold(self):
        result = self.html("# Title")
        assert "<b>Title</b>" in result

    def test_h2_also_bold(self):
        result = self.html("## Section")
        assert "<b>Section</b>" in result

    def test_bullet_list(self):
        result = self.html("- apple\n- banana")
        assert "apple" in result
        assert "banana" in result
        assert "•" in result

    def test_link(self):
        result = self.html("[click](https://example.com)")
        assert '<a href="https://example.com">click</a>' in result

    def test_escapes_angle_brackets(self):
        result = self.html("a < b and c > d")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "<" not in result.replace("</", "").replace("<b", "").replace("<i", "").replace("<code", "").replace(
            "<pre", ""
        ).replace("<a ", "")

    def test_escapes_ampersand(self):
        result = self.html("Tom & Jerry")
        assert "&amp;" in result

    def test_hr_becomes_unicode_line(self):
        result = self.html("---")
        assert "──" in result

    def test_plain_text_passthrough(self):
        result = self.html("just plain text here")
        assert "just plain text here" in result

    def test_table_renders_as_pre(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = self.html(md)
        assert "<pre>" in result
        assert "A" in result
        assert "B" in result

    def test_strikethrough(self):
        result = self.html("~~deleted~~")
        assert "<s>deleted</s>" in result

    def test_nested_bold_italic(self):
        result = self.html("**_both_**")
        assert "<b>" in result
        assert "<i>" in result

    def test_empty_string(self):
        assert self.html("") == ""

    def test_code_block_escapes_html_chars(self):
        result = self.html("```\n<script>alert(1)</script>\n```")
        assert "&lt;script&gt;" in result
        assert "<script>" not in result


class TestSendLongMessageParseMode:
    async def test_plain_send_no_parse_mode(self):
        from shared import send_long_message

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_long_message(123, "hello world", bot)
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["parse_mode"] is None

    async def test_html_parse_mode_passed_to_telegram(self):
        from shared import send_long_message

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_long_message(123, "**bold**", bot, parse_mode="HTML")
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["parse_mode"] == "HTML"
        # Text should be the converted HTML, not raw markdown
        assert "**" not in kwargs["text"]
        assert "<b>bold</b>" in kwargs["text"]

    async def test_html_escapes_dangerous_chars(self):
        from shared import send_long_message

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_long_message(123, "a < b", bot, parse_mode="HTML")
        text_sent = bot.send_message.call_args.kwargs["text"]
        assert "&lt;" in text_sent

    async def test_empty_text_not_sent(self):
        from shared import send_long_message

        bot = MagicMock()
        bot.send_message = AsyncMock()
        await send_long_message(123, "", bot, parse_mode="HTML")
        bot.send_message.assert_not_called()
