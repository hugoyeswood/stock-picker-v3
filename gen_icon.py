# -*- coding: utf-8 -*-
"""生成安卓 App 图标：深蓝圆角底 + 白色上升折线（涨势）+ 末端红点"""
import os
from PIL import Image, ImageDraw

BG   = (26, 35, 126)     # 深蓝 #1a237e
LINE = (255, 255, 255)   # 白色折线
DOT  = (211, 47, 47)     # 红色末端点 #d32f2f

# 折线经过点（相对坐标 0~1）
PTS = [(0.13, 0.74), (0.28, 0.50), (0.46, 0.63), (0.64, 0.34), (0.80, 0.20)]

def draw_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.20)
    d.rounded_rectangle([0, 0, size-1, size-1], radius=r, fill=BG)
    # 折线
    pts = [(int(x*size), int(y*size)) for x, y in PTS]
    d.line(pts, fill=LINE, width=max(2, int(size*0.045)), joint="curve")
    # 末端红点
    ex, ey = pts[-1]
    rad = int(size*0.045)
    d.ellipse([ex-rad, ey-rad, ex+rad, ey+rad], fill=DOT)
    return img

os.makedirs("icons", exist_ok=True)
draw_icon(512).save("icons/icon-512.png")
draw_icon(192).save("icons/icon-192.png")
draw_icon(180).save("icons/apple-touch-icon.png")
print("图标已生成: icons/icon-512.png, icon-192.png, apple-touch-icon.png")
