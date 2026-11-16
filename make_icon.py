"""Генерирует icon.ico (иконка батареи) без сторонних библиотек."""
import struct
import zlib


def png(size: int) -> bytes:
    def px(x, y):
        s = size / 32
        body = (2 * s, 9 * s, 27 * s, 23 * s)          # x0, y0, x1, y1
        t = max(1.0, 2.2 * s)
        x0, y0, x1, y1 = body
        inside = x0 <= x < x1 and y0 <= y < y1
        border = inside and not (x0 + t <= x < x1 - t and y0 + t <= y < y1 - t)
        nub = x1 <= x < x1 + 3 * s and 13 * s <= y < 19 * s
        if border or nub:
            return (240, 240, 240, 255)
        fx0, fy0, fx1, fy1 = x0 + t + s, y0 + t + s, x1 - t - s, y1 - t - s
        if fx0 <= x < fx1 and fy0 <= y < fy1:
            return (76, 194, 74, 255)
        return (0, 0, 0, 0)

    raw = b"".join(b"\0" + b"".join(bytes(px(x + .5, y + .5)) for x in range(size))
                   for y in range(size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    sizes = [16, 24, 32, 48, 64, 256]
    images = [png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open("icon.ico", "wb") as f:
        f.write(header + entries + b"".join(images))


if __name__ == "__main__":
    main()
