#!/usr/bin/env python3
"""voiceover.py — AI voiceover (piper TTS). say(text, out_wav)."""
import subprocess, sys
from pathlib import Path
VOICE = Path.home()/"projects/agent-os/assets/voices/en_US-lessac-medium.onnx"
PIPER = Path.home()/"projects/agent-os/.venv/bin/piper"
def say(text, out):
    subprocess.run([str(PIPER),"--model",str(VOICE),"--output_file",out], input=text.encode(), check=True, capture_output=True)
    return out
if __name__=="__main__":
    say(sys.argv[2] if len(sys.argv)>2 else "Hello from agent O S", sys.argv[1] if len(sys.argv)>1 else "/tmp/vo.wav")
    print("voiceover written")
