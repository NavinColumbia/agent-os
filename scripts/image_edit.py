#!/usr/bin/env python3
"""image_edit.py — image editing/recolor/thumbnail (ImageMagick)."""
import subprocess, sys
def recolor(src,out,modulate="100,120,200"): subprocess.run(["convert",src,"-modulate",modulate,out],check=True); return out
def thumbnail(src,out,size="400x"): subprocess.run(["convert",src,"-thumbnail",size,out],check=True); return out
if __name__=="__main__":
    recolor(sys.argv[1], sys.argv[2]); print("recolored")
