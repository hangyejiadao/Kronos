# 用 curl（走代理）逐个下载 HF 模型文件，支持断点续传和重试
# huggingface_hub 在这台机器上下载大文件会被重置，curl 更稳
import json
import subprocess
import sys

PROXY = "http://127.0.0.1:7890"
REPOS = [
    ("NeoQuasar/Kronos-base", r"D:\Kronos\pretrained\Kronos-base"),
    ("NeoQuasar/Kronos-Tokenizer-base", r"D:\Kronos\pretrained\Kronos-Tokenizer-base"),
]

def curl_json(url):
    out = subprocess.run(
        ["curl", "-s", "-x", PROXY, url],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout
    return json.loads(out)

def curl_file(url, dest):
    print(f"GET {url} -> {dest}", flush=True)
    r = subprocess.run(
        ["curl", "-L", "-x", PROXY, "-C", "-", "--retry", "5",
         "--retry-all-errors", "--retry-delay", "5",
         "-o", dest, url],
        timeout=3600,
    )
    print(f"curl exit={r.returncode} for {dest}", flush=True)
    if r.returncode != 0:
        raise SystemExit(f"FAILED {url} (exit {r.returncode})")

for repo, outdir in REPOS:
    info = curl_json(f"https://huggingface.co/api/models/{repo}")
    files = [s["rfilename"] for s in info["siblings"]]
    print(f"{repo}: {files}", flush=True)
    import os
    os.makedirs(outdir, exist_ok=True)
    for name in files:
        if name in (".gitattributes", "README.md"):
            continue  # 已有
        dest = os.path.join(outdir, name)
        if os.path.exists(dest):
            print(f"SKIP existing {dest}", flush=True)
            continue
        curl_file(f"https://huggingface.co/{repo}/resolve/main/{name}", dest)

print("ALL DONE", flush=True)
