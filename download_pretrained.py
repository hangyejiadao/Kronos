# 下载 Kronos-base 与 Kronos-Tokenizer-base 到 D:\Kronos\pretrained
from huggingface_hub import snapshot_download

for repo in ["NeoQuasar/Kronos-base", "NeoQuasar/Kronos-Tokenizer-base"]:
    name = repo.split("/")[-1]
    print(f"DOWNLOADING {repo} ...", flush=True)
    path = snapshot_download(repo_id=repo, local_dir=f"D:/Kronos/pretrained/{name}")
    print(f"DONE {repo} -> {path}", flush=True)
print("ALL DONE", flush=True)
