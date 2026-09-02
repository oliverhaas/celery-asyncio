"""Tests for the shared transport base."""

import base64
import logging

from kombu.transport.base import Envelope, decode_envelope
from kombu.utils.json import dumps as json_dumps


class test_decode_envelope:
    def test_text_body(self):
        data = json_dumps(
            {
                "body": "hello",
                "content-type": "text/plain",
                "content-encoding": "utf-8",
                "properties": {"delivery_mode": 2},
                "headers": {"lang": "py"},
            },
        ).encode()

        assert decode_envelope(data, "q") == Envelope(
            b"hello",
            "text/plain",
            "utf-8",
            {"delivery_mode": 2},
            {"lang": "py"},
        )

    def test_base64_body(self):
        data = json_dumps(
            {
                "body": base64.b64encode(b"\x00\xff").decode(),
                "content-type": "application/data",
                "content-encoding": "binary",
                "headers": {"body_encoding": "base64"},
            },
        ).encode()

        assert decode_envelope(data, "q").body == b"\x00\xff"

    def test_structured_body_is_reserialized(self):
        data = json_dumps({"body": [1, 2], "content-type": "application/json"}).encode()

        assert decode_envelope(data, "q").body == b"[1, 2]"

    def test_binary_content_encoding_keeps_the_string_bytes(self):
        data = json_dumps({"body": "hello", "content-encoding": "binary"}).encode()

        assert decode_envelope(data, "q").body == b"hello"

    def test_payload_that_is_not_json(self):
        assert decode_envelope(b"not json at all", "q") == Envelope(
            b"not json at all",
            "application/data",
            "binary",
            {},
            {},
        )

    def test_json_that_is_not_an_object(self, caplog):
        with caplog.at_level(logging.ERROR, logger="kombu.transport.base"):
            envelope = decode_envelope(b"[1, 2, 3]", "myqueue")

        assert envelope == Envelope(b"[1, 2, 3]", "application/data", "binary", {}, {})
        assert "myqueue" in caplog.text
        assert "JSON list" in caplog.text

    def test_body_that_cannot_be_decoded(self, caplog):
        data = json_dumps({"body": "hello", "content-encoding": "no-such-codec"}).encode()

        with caplog.at_level(logging.ERROR, logger="kombu.transport.base"):
            envelope = decode_envelope(data, "myqueue")

        assert envelope == Envelope(data, "application/data", "binary", {}, {})
        assert "myqueue" in caplog.text

    def test_body_defaults_to_the_whole_payload(self):
        data = json_dumps({"content-type": "application/json"}).encode()

        assert decode_envelope(data, "q").body == data
