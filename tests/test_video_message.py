"""QQ 收到视频：入库、抽关键帧、把画面与声音交给模型。

走 OpenAI 兼容网关时 content part 只认 text / image_url（video_url、inline_data
都会被 400 拒掉），所以视频不能整段发出去，必须拆成「带时间戳的关键帧 +
音轨转写」。这里锁住这条链路的三个关键行为。
"""
import asyncio
import unittest
from unittest.mock import patch

from test_core import Fixture
from core import config
from channels.qq_media import MessageParser

# 只要能过 sniff_media 的最小 mp4 头：ftyp 盒 + isom 分支。
MP4_HEAD = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 128
FRAME = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def make_event(segments):
    return {
        "message_id": "9001", "user_id": "123", "self_id": "999",
        "message_type": "private", "sender": {"user_id": "123", "nickname": "主人"},
        "message": segments,
    }


def make_rpc(path):
    async def rpc(action, params):
        if action in ("get_file", "get_video"):
            return {"status": "ok", "retcode": 0, "data": {"file": path}}
        return {"status": "ok", "retcode": 0, "data": {}}

    return rpc


VIDEO_SEGMENT = {"type": "video", "data": {"file": "video.mp4"}}


class VideoMessageTests(Fixture):
    def setUp(self):
        super().setUp()
        config.save_config({"channels": {"napcat": {
            "max_images": 9, "max_videos": 2, "video_frames": 3, "video_frame_width": 512,
        }}})

    def _parse(self, segments, analysis, rpc_path="C:/fake/video.mp4", media=None):
        media = media if media is not None else (MP4_HEAD, "video/mp4")
        with patch("channels.qq_media.first_media", return_value=media), \
                patch("channels.qq_media.video_probe.analyze", return_value=analysis):
            return asyncio.run(MessageParser(make_rpc(rpc_path), make_event(segments)).parse())

    def test_video_is_stored_and_frames_go_into_images(self):
        parsed = self._parse([VIDEO_SEGMENT], {
            "duration": 12.0,
            "frames": [(1.0, FRAME), (5.5, FRAME)],
            "audio": None,
            "error": "",
        })
        self.assertEqual(len(parsed["videos"]), 1)
        self.assertTrue(parsed["videos"][0]["media_ref"].startswith("vid_"))
        self.assertEqual(parsed["videos"][0]["mime"], "video/mp4")
        # 帧走既有的图片通道，模型才看得见画面
        self.assertEqual(len(parsed["images"]), 2)
        self.assertTrue(parsed["images"][0].startswith("data:image/jpeg;base64,"))
        self.assertIn("[视频 1，时长 12.0 秒", parsed["text"])
        self.assertIn("第1帧 00:01.0", parsed["text"])
        self.assertIn("第2帧 00:05.5", parsed["text"])
        self.assertIn("media_ref=vid_", parsed["text"])
        # 旧行为是把视频段兜底成一句 [video消息]，模型等于什么都没看到
        self.assertNotIn("[video消息]", parsed["text"])

    def test_audio_track_is_transcribed_into_text(self):
        analysis = {
            "duration": 5.0, "frames": [],
            "audio": b"RIFF" + b"\x00" * 2048, "error": "",
        }
        with patch("core.stt.transcribe", return_value={"ok": True, "text": "旁白在讲自行车"}) as stt:
            parsed = self._parse([VIDEO_SEGMENT], analysis)
        stt.assert_called_once()
        self.assertIn("[视频 1 音轨转写] 旁白在讲自行车", parsed["text"])
        self.assertEqual(parsed["errors"], [])

    def test_stt_failure_keeps_the_frames(self):
        analysis = {"duration": 5.0, "frames": [(0.4, FRAME)],
                    "audio": b"RIFF" + b"\x00" * 2048, "error": ""}
        with patch("core.stt.transcribe", side_effect=ValueError("STT 失败：cloud: 502")):
            parsed = self._parse([VIDEO_SEGMENT], analysis)
        self.assertEqual(len(parsed["images"]), 1)
        self.assertIn("[视频 1 音轨未能转写]", parsed["text"])
        self.assertTrue(any("音轨转写失败" in item for item in parsed["errors"]))

    def test_frame_count_respects_remaining_image_slots(self):
        config.save_config({"channels": {"napcat": {"max_images": 4, "video_frames": 6}}})
        with patch("channels.qq_media.first_media", return_value=(MP4_HEAD, "video/mp4")), \
                patch("channels.qq_media.video_probe.analyze",
                      return_value={"duration": 3.0, "frames": [], "audio": None, "error": ""}) as analyze:
            asyncio.run(MessageParser(make_rpc("C:/fake/v.mp4"), make_event([VIDEO_SEGMENT])).parse())
        # 9 张图的预算里视频只能占剩余部分，不能把同一条消息里的图片挤掉
        self.assertEqual(analyze.call_args.args[1], 4)

    def test_missing_ffmpeg_degrades_to_reference_only(self):
        parsed = self._parse([VIDEO_SEGMENT], {
            "duration": 0.0, "frames": [], "audio": None,
            "error": "未找到 ffmpeg，无法解析视频画面",
        })
        self.assertEqual(len(parsed["videos"]), 1)
        self.assertIn("画面无法解析", parsed["text"])
        self.assertEqual(parsed["images"], [])


class QuotedVideoTests(Fixture):
    """引用一条含视频的消息：只借图片，不能把视频塞进图片通道。"""

    def test_quoted_video_does_not_break_cached_quote(self):
        from core.media_store import MediaStore

        store = MediaStore()
        store.add(MP4_HEAD, "qq:private:123", origin="received", message_id="7001")
        parser = MessageParser(make_rpc("x"), make_event([{"type": "text", "data": {"text": "看这个"}}]))
        # preview 对视频返回 None，旧实现会拿 None 去 base64 编出 TypeError
        self.assertFalse(asyncio.run(parser.cached_quote("7001")))
        self.assertEqual(parser.images, [])


if __name__ == "__main__":
    unittest.main()
