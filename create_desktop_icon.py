"""Draw an original hanger/play icon using Pillow; no external artwork."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parent / "desktop_assets"
root.mkdir(exist_ok=True)
image = Image.new("RGBA", (512, 512))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((16, 16, 496, 496), radius=112, fill="#20382f")
draw.arc((199, 93, 292, 186), 180, 430, fill="#e4c394", width=22)
draw.line((247, 180, 247, 213), fill="#e4c394", width=22)
draw.line((247, 208, 103, 300, 109, 321, 396, 321, 402, 300, 255, 208), fill="#e4c394", width=22, joint="curve")
draw.rounded_rectangle((268, 281, 440, 453), radius=59, fill="#a8d9b1")
draw.polygon((325, 321, 325, 414, 397, 367), fill="#20382f")
image.save(root / "studio.ico", format="ICO", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
image.save(root / "studio.png")
(root / "studio.svg").write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect x="16" y="16" width="480" height="480" rx="112" fill="#20382f"/><path d="M210 140a38 38 0 1 1 38 38v35L108 304v16h288v-16L252 212" fill="none" stroke="#e4c394" stroke-width="22" stroke-linejoin="round"/><rect x="268" y="281" width="172" height="172" rx="59" fill="#a8d9b1"/><path d="m325 321 72 46-72 47Z" fill="#20382f"/></svg>''', encoding="utf-8")
print("Original desktop icon created")
