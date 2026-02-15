"""Tests for voice/audio transcription in bot.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTranscribeVoice:
    """Tests for _transcribe_voice()."""

    @pytest.fixture
    def mock_openai(self):
        """Patch openai_client in bot module."""
        mock_client = MagicMock()
        transcript = MagicMock()
        transcript.text = "Hello world"
        mock_client.audio.transcriptions.create.return_value = transcript
        with patch("bot.openai_client", mock_client):
            yield mock_client

    @pytest.mark.asyncio
    async def test_transcribe_voice_ogg(self, mock_openai, mock_telegram_bot):
        from bot import _transcribe_voice

        file_obj = MagicMock()
        mock_telegram_bot.get_file.return_value = AsyncMock(download_as_bytearray=AsyncMock(return_value=b"fake-ogg"))

        with patch("bot._download_telegram_file", new_callable=AsyncMock, return_value=b"fake-ogg"):
            result = await _transcribe_voice(file_obj, mock_telegram_bot, "voice.ogg")

        assert result == "Hello world"
        call_kwargs = mock_openai.audio.transcriptions.create.call_args
        assert call_kwargs[1]["model"] == "whisper-1"
        assert call_kwargs[1]["file"].name == "voice.ogg"

    @pytest.mark.asyncio
    async def test_transcribe_voice_custom_filename(self, mock_openai, mock_telegram_bot):
        from bot import _transcribe_voice

        file_obj = MagicMock()
        with patch("bot._download_telegram_file", new_callable=AsyncMock, return_value=b"fake-mp3"):
            result = await _transcribe_voice(file_obj, mock_telegram_bot, "recording.mp3")

        assert result == "Hello world"
        call_kwargs = mock_openai.audio.transcriptions.create.call_args
        assert call_kwargs[1]["file"].name == "recording.mp3"


class TestBuildUserContentVoice:
    """Tests for voice handling in _build_user_content()."""

    def _make_update(self, voice=None, audio=None):
        """Create a mock Update with voice/audio."""
        msg = MagicMock()
        msg.text = None
        msg.caption = None
        msg.photo = []
        msg.sticker = None
        msg.document = None
        msg.voice = voice
        msg.audio = audio
        msg.video = None
        msg.video_note = None
        msg.location = None
        msg.contact = None
        update = MagicMock()
        update.message = msg
        return update

    @pytest.mark.asyncio
    async def test_voice_with_openai_configured(self, mock_telegram_bot):
        from bot import _build_user_content

        voice = MagicMock()
        update = self._make_update(voice=voice)

        mock_client = MagicMock()
        transcript = MagicMock()
        transcript.text = "transcribed text"
        mock_client.audio.transcriptions.create.return_value = transcript

        with (
            patch("bot.openai_client", mock_client),
            patch("bot._download_telegram_file", new_callable=AsyncMock, return_value=b"ogg-data"),
        ):
            result = await _build_user_content(update, mock_telegram_bot)

        assert "[Voice transcription]: transcribed text" in result

    @pytest.mark.asyncio
    async def test_voice_without_openai(self, mock_telegram_bot):
        from bot import _build_user_content

        voice = MagicMock()
        update = self._make_update(voice=voice)

        with patch("bot.openai_client", None):
            result = await _build_user_content(update, mock_telegram_bot)

        assert "not configured" in result

    @pytest.mark.asyncio
    async def test_voice_transcription_error(self, mock_telegram_bot):
        from bot import _build_user_content

        voice = MagicMock()
        update = self._make_update(voice=voice)

        mock_client = MagicMock()
        mock_client.audio.transcriptions.create.side_effect = RuntimeError("API down")

        with (
            patch("bot.openai_client", mock_client),
            patch("bot._download_telegram_file", new_callable=AsyncMock, return_value=b"ogg-data"),
        ):
            result = await _build_user_content(update, mock_telegram_bot)

        assert "transcription failed" in result

    @pytest.mark.asyncio
    async def test_audio_with_openai_configured(self, mock_telegram_bot):
        from bot import _build_user_content

        audio = MagicMock()
        audio.file_name = "song.mp3"
        audio.title = "My Song"
        update = self._make_update(audio=audio)

        mock_client = MagicMock()
        transcript = MagicMock()
        transcript.text = "lyrics here"
        mock_client.audio.transcriptions.create.return_value = transcript

        with (
            patch("bot.openai_client", mock_client),
            patch("bot._download_telegram_file", new_callable=AsyncMock, return_value=b"mp3-data"),
        ):
            result = await _build_user_content(update, mock_telegram_bot)

        assert "[Audio transcription of song.mp3]: lyrics here" in result

    @pytest.mark.asyncio
    async def test_audio_without_openai(self, mock_telegram_bot):
        from bot import _build_user_content

        audio = MagicMock()
        audio.file_name = "song.mp3"
        audio.title = "My Song"
        update = self._make_update(audio=audio)

        with patch("bot.openai_client", None):
            result = await _build_user_content(update, mock_telegram_bot)

        assert "not configured" in result
