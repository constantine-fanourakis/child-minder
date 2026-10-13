"""Capture an X11 screen as a PNG over the X11 protocol, without an X library.

Run as a script, as the user who owns the display:

    python3 -I x11.py DISPLAY XAUTHORITY > screen.png

The broker runs it that way. Running it as root instead would let the user
name any file as XAUTHORITY and have root send the cookies in it to a socket
the user controls. It imports only the standard library, so it runs under -I.

capture() connects to the display's Unix socket, authenticates with the
display's MIT-MAGIC-COOKIE-1 from the authority file (or with none, if the
file has no entry for it), reads the root window with GetImage, and encodes
it as an 8-bit RGB PNG. Only pixel formats with 8-bit, byte-aligned red,
green and blue channels in 24 or 32 bits per pixel are supported, which is
what Xorg and Xvfb use at depth 24 and 32.
"""

import os
import re
import socket
import struct
import sys
import zlib

COOKIE_NAME = b"MIT-MAGIC-COOKIE-1"
FAMILY_LOCAL = 256
FAMILY_WILD = 65535
SOCKET_DIR = "/tmp/.X11-unix"
TIMEOUT = 10
GET_IMAGE = 73
Z_PIXMAP = 2
DISPLAY_RE = re.compile(r"^(?:unix)?:(\d+)(?:\.(\d+))?$")
# GetImage errors, by X error code
GET_IMAGE_ERRORS = {
    8: "BadMatch: the root window cannot be read, as in a rootless Xwayland session",
    9: "BadDrawable: the root window does not exist",
}


class X11Error(Exception):
    pass


def parse_display(display: str):
    """Return (display number, screen number) for a local display such as :0.0"""
    match = DISPLAY_RE.match(display)
    if not match:
        raise X11Error(f"Not a local X11 display: {display!r}")
    return int(match.group(1)), int(match.group(2) or 0)


def read_cookie(path: str, number: int, hostname: str):
    """Return the MIT-MAGIC-COOKIE-1 for display number from an authority file.

    An entry for this host is preferred over one for any host (FamilyWild).
    None means the file is missing or has no entry for the display.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    def field(offset):
        (length,) = struct.unpack_from(">H", data, offset)
        end = offset + 2 + length
        if end > len(data):
            raise X11Error("Truncated authority file")
        return data[offset + 2:end], end

    local = wild = None
    offset = 0
    try:
        while offset + 2 <= len(data):
            (family,) = struct.unpack_from(">H", data, offset)
            address, offset = field(offset + 2)
            entry_number, offset = field(offset)
            name, offset = field(offset)
            cookie, offset = field(offset)
            if name != COOKIE_NAME or entry_number not in (str(number).encode(), b""):
                continue
            if family == FAMILY_LOCAL and address == hostname.encode() and local is None:
                local = cookie
            elif family == FAMILY_WILD and wild is None:
                wild = cookie
    except (struct.error, X11Error):
        pass  # Use whatever entries came before the damage
    return local if local is not None else wild


def pad4(length: int) -> int:
    return (4 - length % 4) % 4


class Connection:
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def read(self, count: int) -> bytes:
        chunks = []
        while count:
            chunk = self.sock.recv(min(count, 1 << 20))
            if not chunk:
                raise X11Error("The X server closed the connection")
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)


def connect(number: int, socket_dir: str = SOCKET_DIR) -> socket.socket:
    """Connect to a local display's socket, falling back to its abstract name"""
    path = f"{socket_dir}/X{number}"
    for address in (path, "\0" + path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        try:
            sock.connect(address)
            return sock
        except OSError:
            sock.close()
    raise X11Error(f"Cannot connect to X display :{number}")


def setup(conn: Connection, cookie) -> bytes:
    """Send the connection setup in little-endian order and return the reply body"""
    name, data = (COOKIE_NAME, cookie) if cookie else (b"", b"")
    conn.sock.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0, len(name), len(data))
                      + name + b"\0" * pad4(len(name)) + data + b"\0" * pad4(len(data)))
    status, reason_length, _, _, length = struct.unpack("<BBHHH", conn.read(8))
    body = conn.read(length * 4)
    if status == 1:
        return body
    reason = body[:reason_length] if status == 0 else body
    raise X11Error("The X server refused the connection: "
                   + reason.decode(errors="replace").strip("\0 ").strip())


def parse_setup(body: bytes, screen_number: int) -> dict:
    """Return the root window, its size and its pixel format from a setup reply"""
    (vendor_length, screen_count, format_count, image_byte_order) = struct.unpack_from(
        "<H2xBBB", body, 16)
    if screen_number >= screen_count:
        raise X11Error(f"The display has no screen {screen_number}")
    offset = 32 + vendor_length + pad4(vendor_length)
    formats = {}
    for _ in range(format_count):
        depth, bits_per_pixel, scanline_pad = struct.unpack_from("<BBB", body, offset)
        formats[depth] = (bits_per_pixel, scanline_pad)
        offset += 8

    for index in range(screen_count):
        root, width, height, root_visual, root_depth, depth_count = struct.unpack_from(
            "<I16xHH4x4xI2xBB", body, offset)
        offset += 40
        masks = None
        for _ in range(depth_count):
            _depth, visual_count = struct.unpack_from("<BxH4x", body, offset)
            offset += 8
            for _ in range(visual_count):
                visual_id, red, green, blue = struct.unpack_from("<I4xIII4x", body, offset)
                if visual_id == root_visual:
                    masks = (red, green, blue)
                offset += 24
        if index == screen_number:
            if root_depth not in formats or masks is None:
                raise X11Error("The X server did not describe the root window's format")
            bits_per_pixel, scanline_pad = formats[root_depth]
            return {"root": root, "width": width, "height": height,
                    "bits_per_pixel": bits_per_pixel, "scanline_pad": scanline_pad,
                    "msb_first": image_byte_order == 1, "masks": masks}
    raise X11Error(f"The display has no screen {screen_number}")


def channel_offsets(screen: dict) -> list:
    """Byte offset of red, green and blue within a pixel, or raise X11Error"""
    pixel_bytes = screen["bits_per_pixel"] // 8
    if screen["bits_per_pixel"] not in (24, 32):
        raise X11Error(f"Unsupported pixel size: {screen['bits_per_pixel']} bits")
    offsets = []
    for mask in screen["masks"]:
        shift = (mask & -mask).bit_length() - 1
        if mask == 0 or mask >> shift != 0xFF or shift % 8:
            raise X11Error(f"Unsupported colour mask: {mask:#x}")
        offsets.append(pixel_bytes - 1 - shift // 8 if screen["msb_first"] else shift // 8)
    return offsets


def get_image(conn: Connection, screen: dict) -> bytes:
    """Read the whole root window in ZPixmap format"""
    conn.sock.sendall(struct.pack("<BBHIhhHHI", GET_IMAGE, Z_PIXMAP, 5, screen["root"],
                                  0, 0, screen["width"], screen["height"], 0xFFFFFFFF))
    header = conn.read(32)
    if header[0] == 0:
        raise X11Error("GetImage failed: "
                       + GET_IMAGE_ERRORS.get(header[1], f"X error {header[1]}"))
    (length,) = struct.unpack_from("<I", header, 4)
    return conn.read(length * 4)


def to_rgb_rows(pixels: bytes, screen: dict) -> bytes:
    """Convert ZPixmap data to PNG scanlines: a filter byte, then RGB per row"""
    width, height = screen["width"], screen["height"]
    pixel_bytes = screen["bits_per_pixel"] // 8
    pad_bits = screen["scanline_pad"]
    stride = (width * screen["bits_per_pixel"] + pad_bits - 1) // pad_bits * pad_bits // 8
    if len(pixels) < stride * height:
        raise X11Error("The X server sent less image data than expected")
    red, green, blue = channel_offsets(screen)
    rows = bytearray((width * 3 + 1) * height)
    row_rgb = bytearray(width * 3)
    for y in range(height):
        start = y * stride
        end = start + width * pixel_bytes
        row_rgb[0::3] = pixels[start + red:end:pixel_bytes]
        row_rgb[1::3] = pixels[start + green:end:pixel_bytes]
        row_rgb[2::3] = pixels[start + blue:end:pixel_bytes]
        out = y * (width * 3 + 1)
        rows[out + 1:out + 1 + width * 3] = row_rgb  # Filter byte 0: none
    return bytes(rows)


def encode_png(width: int, height: int, rows: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b""))


def capture(display: str, xauthority: str, socket_dir: str = SOCKET_DIR) -> bytes:
    """Return the root window of display as a PNG, or raise X11Error"""
    number, screen_number = parse_display(display)
    cookie = read_cookie(xauthority, number, socket.gethostname())
    sock = connect(number, socket_dir)
    try:
        conn = Connection(sock)
        screen = parse_setup(setup(conn, cookie), screen_number)
        channel_offsets(screen)  # Refuse an unsupported format before the transfer
        pixels = get_image(conn, screen)
    except socket.timeout:
        raise X11Error("The X server did not answer in time")
    except OSError as e:
        raise X11Error(f"X11 connection failed: {e}")
    finally:
        sock.close()
    return encode_png(screen["width"], screen["height"], to_rgb_rows(pixels, screen))


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("Usage: x11.py DISPLAY XAUTHORITY\n")
        sys.exit(2)
    try:
        png = capture(sys.argv[1], sys.argv[2])
    except (X11Error, struct.error) as e:
        message = str(e) if isinstance(e, X11Error) else "Malformed reply from the X server"
        sys.stderr.write(f"{message}\n")
        sys.exit(1)
    sys.stdout.buffer.write(png)


if __name__ == "__main__":
    main()
