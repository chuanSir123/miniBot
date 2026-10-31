"""合成一张极简静态页面截图，用作提示词的误报对照。

只依赖标准库：白底 + 一张卡片 + 一条按钮 + 几行占位文字块。目的是提供一张
"明确没有运动内容、也没有物理接触关系"的正常截图，用来检查提示词会不会因为
被要求找茬而开始乱打回。
"""
import struct
import sys
import zlib

W, H = 640, 420
BG = (247, 248, 250)
CARD = (255, 255, 255)
BORDER = (226, 230, 236)
TEXT = (60, 66, 78)
MUTED = (168, 176, 188)
BUTTON = (58, 120, 240)


def canvas():
    return [bytearray(BG * W) for _ in range(H)]


def rect(rows, x0, y0, x1, y1, color):
    for y in range(max(0, y0), min(H, y1)):
        row = rows[y]
        for x in range(max(0, x0), min(W, x1)):
            row[x * 3:x * 3 + 3] = bytes(color)


def round_rect(rows, x0, y0, x1, y1, radius, color):
    rect(rows, x0 + radius, y0, x1 - radius, y1, color)
    rect(rows, x0, y0 + radius, x1, y1 - radius, color)
    for cx, cy in ((x0 + radius, y0 + radius), (x1 - radius, y0 + radius),
                   (x0 + radius, y1 - radius), (x1 - radius, y1 - radius)):
        for y in range(cy - radius, cy + radius):
            for x in range(cx - radius, cx + radius):
                if 0 <= x < W and 0 <= y < H and (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                    rows[y][x * 3:x * 3 + 3] = bytes(color)


def write_png(path, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def main(path):
    rows = canvas()
    round_rect(rows, 96, 56, 544, 364, 16, CARD)
    rect(rows, 97, 56, 543, 58, BORDER)
    rect(rows, 97, 362, 543, 364, BORDER)
    # 标题 + 三行正文
    rect(rows, 132, 96, 380, 118, TEXT)
    for i in range(3):
        y = 140 + i * 22
        rect(rows, 132, y, 508 - (40 if i == 2 else 0), y + 10, MUTED)
    # 按钮
    round_rect(rows, 132, 236, 292, 282, 10, BUTTON)
    rect(rows, 160, 254, 264, 264, CARD)
    # 一个纯装饰的圆形图标
    for y in range(96, 128):
        for x in range(452, 484):
            if (x - 468) ** 2 + (y - 112) ** 2 <= 14 ** 2:
                rows[y][x * 3:x * 3 + 3] = bytes(MUTED)
    write_png(path, rows)
    print("wrote", path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "static_page.png")
