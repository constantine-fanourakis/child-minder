"""Tests for the X11 screen capture, against a fake X server on a Unix socket"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

from childminder import x11

ROOT = 0x123
VISUAL = 0x21
RGB_MASKS = (0xFF0000, 0x00FF00, 0x0000FF)
PIXELS = [[(255, 0, 0), (0, 255, 0), (0, 0, 255)],
          [(1, 2, 3), (250, 128, 7), (0, 0, 0)]]


def auth_entry(family: int, address: bytes, number: bytes, cookie: bytes,
               name: bytes = x11.COOKIE_NAME) -> bytes:
    return struct.pack(">H", family) + b"".join(
        struct.pack(">H", len(f)) + f for f in (address, number, name, cookie))


def setup_reply(width: int, height: int, depth: int = 24, bits_per_pixel: int = 32,
                msb_first: bool = False, masks=RGB_MASKS) -> bytes:
    vendor = b"Fake"
    body = (struct.pack("<IIII", 1, 0, 0, 0)
            + struct.pack("<HHBBBBBBBB4x", len(vendor), 65535, 1, 2, int(msb_first),
                          0, 32, 32, 8, 255)
            + vendor
            + struct.pack("<BBB5x", 1, 1, 32)
            + struct.pack("<BBB5x", depth, bits_per_pixel, 32)
            + struct.pack("<IIIIIHHHHHHIBBBB", ROOT, 0, 0, 0, 0, width, height,
                          300, 200, 1, 1, VISUAL, 0, 0, depth, 1)
            + struct.pack("<BxH4x", depth, 1)
            + struct.pack("<IBBHIII4x", VISUAL, 4, 8, 256, *masks))
    return struct.pack("<BBHHH", 1, 0, 11, 0, len(body) // 4) + body


def image_data(bits_per_pixel: int = 32, msb_first: bool = False) -> bytes:
    """ZPixmap rows for PIXELS, each padded to 32 bits"""
    rows = []
    for row in PIXELS:
        data = b""
        for r, g, b in row:
            value = r << 16 | g << 8 | b
            size = bits_per_pixel // 8
            data += value.to_bytes(size, "big" if msb_first else "little")
        rows.append(data + b"\0" * x11.pad4(len(data)))
    return b"".join(rows)


def decode_png(png: bytes):
    """Return (width, height, rows of RGB tuples) of an unfiltered 8-bit RGB PNG"""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset, idat = 8, b""
    while offset < len(png):
        (length,) = struct.unpack_from(">I", png, offset)
        kind = png[offset + 4:offset + 8]
        data = png[offset + 8:offset + 8 + length]
        (crc,) = struct.unpack_from(">I", png, offset + 8 + length)
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        if kind == b"IHDR":
            width, height, bit_depth, colour_type = struct.unpack(">IIBB", data[:10])
            assert (bit_depth, colour_type) == (8, 2)
        elif kind == b"IDAT":
            idat += data
        offset += 12 + length
    raw = zlib.decompress(idat)
    rows = []
    for y in range(height):
        line = raw[y * (width * 3 + 1):(y + 1) * (width * 3 + 1)]
        assert line[0] == 0
        rows.append([tuple(line[1 + x * 3:4 + x * 3]) for x in range(width)])
    return width, height, rows


class FakeXServer:
    """Answers one connection: the setup, then one GetImage request"""

    def __init__(self, directory: Path, number: int, setup: bytes, image: bytes = b"",
                 image_reply=None):
        self.setup = setup
        self.image = image
        self.image_reply = image_reply
        self.received = {}
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(directory / f"X{number}"))
        self.listener.listen()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def read(self, conn, count):
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def serve(self):
        conn, _ = self.listener.accept()
        with conn:
            try:
                order, major, _, name_length, data_length = struct.unpack(
                    "<BxHHHHxx", self.read(conn, 12))
                name = self.read(conn, name_length + x11.pad4(name_length))[:name_length]
                cookie = self.read(conn, data_length + x11.pad4(data_length))[:data_length]
                self.received.update(order=order, major=major, name=name, cookie=cookie)
                conn.sendall(self.setup)
                if self.setup[0] != 1:
                    return
                self.received["get_image"] = struct.unpack("<BBHIhhHHI", self.read(conn, 20))
                if self.image_reply is not None:
                    conn.sendall(self.image_reply)
                else:
                    conn.sendall(struct.pack("<BBHII20x", 1, 24, 1, len(self.image) // 4,
                                             VISUAL) + self.image)
            except EOFError:
                pass

    def close(self):
        self.thread.join(5)
        self.listener.close()


class CaptureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.auth = self.dir / "Xauthority"
        self.auth.write_bytes(auth_entry(x11.FAMILY_LOCAL, socket.gethostname().encode(),
                                         b"7", b"cookie-bytes"))

    def serve(self, *args, **kwargs) -> FakeXServer:
        server = FakeXServer(self.dir, 7, *args, **kwargs)
        self.addCleanup(server.close)
        return server

    def capture(self, display=":7"):
        return x11.capture(display, str(self.auth), socket_dir=str(self.dir))

    def test_capture_authenticates_and_converts_the_root_window(self):
        server = self.serve(setup_reply(3, 2), image_data())
        width, height, rows = decode_png(self.capture())
        self.assertEqual((width, height, rows), (3, 2, PIXELS))
        self.assertEqual(server.received["order"], 0x6C)
        self.assertEqual(server.received["major"], 11)
        self.assertEqual(server.received["name"], x11.COOKIE_NAME)
        self.assertEqual(server.received["cookie"], b"cookie-bytes")
        opcode, image_format, length, drawable, x, y, w, h, planes = \
            server.received["get_image"]
        self.assertEqual((opcode, image_format, length, drawable, w, h),
                         (x11.GET_IMAGE, x11.Z_PIXMAP, 5, ROOT, 3, 2))

    def test_most_significant_byte_first_images(self):
        self.serve(setup_reply(3, 2, msb_first=True), image_data(msb_first=True))
        self.assertEqual(decode_png(self.capture())[2], PIXELS)

    def test_24_bit_pixels_with_padded_rows(self):
        self.serve(setup_reply(3, 2, bits_per_pixel=24), image_data(bits_per_pixel=24))
        self.assertEqual(decode_png(self.capture())[2], PIXELS)

    def test_screen_suffix_is_accepted(self):
        self.serve(setup_reply(3, 2), image_data())
        self.assertEqual(decode_png(self.capture(":7.0"))[0], 3)

    def test_without_a_cookie_no_authorization_is_sent(self):
        self.auth.write_bytes(b"")
        server = self.serve(setup_reply(3, 2), image_data())
        self.capture()
        self.assertEqual((server.received["name"], server.received["cookie"]), (b"", b""))

    def test_refused_connection_reports_the_server_reason(self):
        reason = b"No protocol specified"
        refusal = (struct.pack("<BBHHH", 0, len(reason), 11, 0, 6)
                   + reason + b"\0" * (24 - len(reason)))
        self.serve(refusal)
        with self.assertRaisesRegex(x11.X11Error, "refused the connection: No protocol"):
            self.capture()

    def test_unsupported_pixel_format_is_refused_before_reading_pixels(self):
        server = self.serve(setup_reply(3, 2, depth=16, bits_per_pixel=16,
                                        masks=(0xF800, 0x07E0, 0x001F)))
        with self.assertRaisesRegex(x11.X11Error, "Unsupported pixel size"):
            self.capture()
        self.assertNotIn("get_image", server.received)

    def test_unsupported_colour_masks_are_refused(self):
        self.serve(setup_reply(3, 2, depth=30, masks=(0x3FF00000, 0xFFC00, 0x3FF)))
        with self.assertRaisesRegex(x11.X11Error, "Unsupported colour mask"):
            self.capture()

    def test_get_image_error_is_reported(self):
        self.serve(setup_reply(3, 2), image_reply=struct.pack("<BBH28x", 0, 8, 1))
        with self.assertRaisesRegex(x11.X11Error, "GetImage failed: BadMatch"):
            self.capture()

    def test_unnamed_get_image_error_gives_its_code(self):
        self.serve(setup_reply(3, 2), image_reply=struct.pack("<BBH28x", 0, 17, 1))
        with self.assertRaisesRegex(x11.X11Error, "X error 17"):
            self.capture()

    def test_short_image_is_refused(self):
        self.serve(setup_reply(3, 2), image_data()[:-8])
        with self.assertRaisesRegex(x11.X11Error, "less image data"):
            self.capture()

    def test_missing_display_is_reported(self):
        with self.assertRaisesRegex(x11.X11Error, "Cannot connect to X display :8"):
            x11.capture(":8", str(self.auth), socket_dir=str(self.dir))


class AuthorityFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "Xauthority"

    def cookie(self, *entries, number=0, hostname="box"):
        self.path.write_bytes(b"".join(entries))
        return x11.read_cookie(str(self.path), number, hostname)

    def test_local_entry_for_this_host_is_preferred(self):
        self.assertEqual(self.cookie(
            auth_entry(x11.FAMILY_WILD, b"", b"0", b"wild"),
            auth_entry(x11.FAMILY_LOCAL, b"other", b"0", b"other-host"),
            auth_entry(x11.FAMILY_LOCAL, b"box", b"0", b"local")), b"local")

    def test_wild_entry_is_used_without_a_local_one(self):
        self.assertEqual(self.cookie(auth_entry(x11.FAMILY_WILD, b"", b"0", b"wild")), b"wild")

    def test_other_displays_and_protocols_are_ignored(self):
        self.assertIsNone(self.cookie(
            auth_entry(x11.FAMILY_LOCAL, b"box", b"1", b"display-1"),
            auth_entry(x11.FAMILY_LOCAL, b"box", b"0", b"x", name=b"XDM-AUTHORIZATION-1")))

    def test_truncated_file_keeps_earlier_entries(self):
        good = auth_entry(x11.FAMILY_LOCAL, b"box", b"0", b"local")
        self.assertEqual(self.cookie(good, b"\x01\x00\x00\x05ab"), b"local")

    def test_missing_file_has_no_cookie(self):
        self.assertIsNone(x11.read_cookie(str(self.path), 0, "box"))


class DisplayTest(unittest.TestCase):
    def test_local_displays(self):
        self.assertEqual(x11.parse_display(":0"), (0, 0))
        self.assertEqual(x11.parse_display(":1.2"), (1, 2))
        self.assertEqual(x11.parse_display("unix:3"), (3, 0))

    def test_other_displays_are_refused(self):
        for display in ("host:0", "wayland-0", ":", ":0 ", ""):
            with self.assertRaises(x11.X11Error, msg=display):
                x11.parse_display(display)


class ScriptTest(unittest.TestCase):
    """The broker runs x11.py as a script under -I, outside the package"""

    def run_script(self, *args):
        return subprocess.run([sys.executable, "-I", x11.__file__, *args],
                              capture_output=True, text=True, timeout=30, cwd="/")

    def test_runs_standalone_and_reports_errors_on_stderr(self):
        result = self.run_script("host:0", os.devnull)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Not a local X11 display", result.stderr)

    def test_usage(self):
        self.assertEqual(self.run_script().returncode, 2)
