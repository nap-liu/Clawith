import sys
from PIL import Image

def process_img(infile, outfile):
    img = Image.open(infile).convert("RGB")
    # Resize to make it smaller since logos only need < 128px usually
    img = img.resize((256, 256), Image.Resampling.LANCZOS)
    img.save(outfile, "PNG", optimize=True)

if __name__ == "__main__":
    process_img('/Users/feng/.gemini/antigravity/brain/275cfe12-0bc7-4518-8835-bd373f877a86/media__1776155218726.jpg', 'frontend/public/logo.png')
