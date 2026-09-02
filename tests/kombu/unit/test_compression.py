import base64
import zlib

import pytest

from kombu import Connection, compression
from kombu.messaging import Producer
from kombu.utils.json import loads as json_loads


class test_compression:
    def test_encoders__gzip(self):
        assert "application/x-gzip" in compression.encoders()

    def test_encoders__bz2(self):
        pytest.importorskip("bz2")
        assert "application/x-bz2" in compression.encoders()

    def test_encoders__brotli(self):
        pytest.importorskip("brotli")

        assert "application/x-brotli" in compression.encoders()

    def test_encoders__lzma(self):
        pytest.importorskip("lzma")

        assert "application/x-lzma" in compression.encoders()

    def test_encoders__zstd(self):
        pytest.importorskip("compression.zstd")

        assert "application/zstd" in compression.encoders()

    def test_compress__decompress__zlib(self):
        text = b"The Quick Brown Fox Jumps Over The Lazy Dog"
        c, ctype = compression.compress(text, "zlib")
        assert text != c
        d = compression.decompress(c, ctype)
        assert d == text

    def test_compress__decompress__bzip2(self):
        text = b"The Brown Quick Fox Over The Lazy Dog Jumps"
        c, ctype = compression.compress(text, "bzip2")
        assert text != c
        d = compression.decompress(c, ctype)
        assert d == text

    def test_compress__decompress__brotli(self):
        pytest.importorskip("brotli")

        text = b"The Brown Quick Fox Over The Lazy Dog Jumps"
        c, ctype = compression.compress(text, "brotli")
        assert text != c
        d = compression.decompress(c, ctype)
        assert d == text

    def test_compress__decompress__lzma(self):
        pytest.importorskip("lzma")

        text = b"The Brown Quick Fox Over The Lazy Dog Jumps"
        c, ctype = compression.compress(text, "lzma")
        assert text != c
        d = compression.decompress(c, ctype)
        assert d == text

    def test_compress__decompress__zstd(self):
        pytest.importorskip("compression.zstd")

        text = b"The Brown Quick Fox Over The Lazy Dog Jumps"
        c, ctype = compression.compress(text, "zstd")
        assert text != c
        d = compression.decompress(c, ctype)
        assert d == text


class test_publish_compression:
    """Producer.publish used to accept `compression` and drop it."""

    async def test_publish_compresses_the_body(self):
        payload = {"hello": "world" * 20}
        async with Connection("memory://") as conn:
            channel = await conn.default_channel()
            envelopes = []
            publish = channel.publish

            async def capture(message, **kwargs):
                envelopes.append(json_loads(message))
                await publish(message=message, **kwargs)

            channel.publish = capture
            headers = {"x": 1}
            producer = Producer(conn, compression="zlib")
            await producer.publish(payload, routing_key="compressed_q", headers=headers)

            envelope = envelopes[0]
            assert envelope["headers"]["compression"] == "application/x-gzip"
            assert envelope["headers"]["body_encoding"] == "base64"
            assert headers == {"x": 1}

            wire = base64.b64decode(envelope["body"])
            assert json_loads(zlib.decompress(wire)) == payload
            assert len(wire) < len(zlib.decompress(wire))

            message = await channel.get("compressed_q", no_ack=True)
            assert message.decode() == payload

    async def test_publish_without_compression_keeps_the_body_readable(self):
        async with Connection("memory://") as conn:
            channel = await conn.default_channel()
            envelopes = []
            publish = channel.publish

            async def capture(message, **kwargs):
                envelopes.append(json_loads(message))
                await publish(message=message, **kwargs)

            channel.publish = capture
            await Producer(conn).publish({"hello": "world"}, routing_key="plain_q")

            envelope = envelopes[0]
            assert envelope["headers"] == {}
            assert envelope["body"] == '{"hello": "world"}'
