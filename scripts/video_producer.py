#!/usr/bin/env python3
"""video_producer.py — assemble a narrated slideshow video (ffmpeg). slideshow(images, audio, out)."""
import subprocess
def slideshow(images, audio, out, per=3.7):
    inp=[]; flt=[]
    for i,img in enumerate(images):
        inp += ["-loop","1","-t",str(per),"-i",img]
        flt.append(f"[{i}]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
    n=len(images); inp += ["-i",audio]
    fc=";".join(flt)+";"+"".join(f"[v{i}]" for i in range(n))+f"concat=n={n}:v=1:a=0[v]"
    subprocess.run(["ffmpeg","-y","-loglevel","error",*inp,"-filter_complex",fc,"-map","[v]","-map",f"{n}:a",
                    "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",out], check=True)
    return out
