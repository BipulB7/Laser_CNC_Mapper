"""Direct-serial ILD1320 decoding (no hardware)."""

import time

from bowmapper.config import Config
from bowmapper.events import EventBus
from bowmapper.ild_serial import FrameDecoder, looks_like_stream, to_mm
from bowmapper.laser import SENTINEL, SerialLaser


def enc(x: int, first: bool = True) -> bytes:
    return bytes([x & 0x3F, 0x40 | ((x >> 6) & 0x3F), (0x80 if first else 0xC0) | ((x >> 12) & 0x3F)])


def test_conversion_matches_manual():
    # optoNCDT 1320 manual 7.5.2.1: 643 = SMR, 64887 = EMR (MR 25 mm)
    assert abs(to_mm(643, 25.0)) < 0.001
    assert abs(to_mm(64887, 25.0) - 25.0) < 0.005
    assert abs(to_mm(32765, 25.0) - 12.5) < 0.005
    assert to_mm(262076, 25.0) is None


def test_decoder_roundtrip_and_resync():
    vals = [643, 32765, 64887, 262078, 1, 65520]
    data = b"".join(enc(v) for v in vals)
    assert FrameDecoder().feed(data) == vals
    d = FrameDecoder()                                   # start mid-frame, split across reads
    assert d.feed(data[2:7]) == [32765]
    assert d.feed(data[7:]) == vals[2:]
    assert d.bad == 1


def test_additional_values_skipped():
    data = enc(1000) + enc(5, first=False) + enc(2000) + enc(6, first=False)
    assert FrameDecoder().feed(data) == [1000, 2000]


def test_stream_detection_rejects_garbage():
    good = b"".join(enc(30000 + i) for i in range(50))
    assert looks_like_stream(good)[0]
    assert not looks_like_stream(bytes(range(256)) * 2)[0]
    assert not looks_like_stream(b"GETINFO\r\n->" * 20)[0]


class FakeSerial:
    def __init__(self, data: bytes):
        self.data = data
        self.in_waiting = len(data)

    def read(self, n):
        out, self.data = self.data[:n], self.data[n:]
        self.in_waiting = len(self.data)
        return out

    def close(self):
        pass


def test_serial_laser_read(tmp_path):
    from bowmapper.ild_serial import FrameDecoder as FD
    las = SerialLaser(Config(tmp_path / "c.json"), EventBus())
    las.ser = FakeSerial(enc(32765) + enc(262076) + enc(64887))
    las.dec = FD()
    las._mr = 25.0
    las._last_data = time.monotonic()
    v = las._read()
    assert abs(v[0] - 12.5) < 0.005 and v[1] == SENTINEL and abs(v[2] - 25.0) < 0.005
    assert "no peak" in las.last_state
    assert not las.valid(v[1])
