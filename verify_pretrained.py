import sys
sys.path.insert(0, r"D:\Kronos")
from model import Kronos, KronosTokenizer

tok = KronosTokenizer.from_pretrained(r"D:\Kronos\pretrained\Kronos-Tokenizer-base")
print("TOKENIZER OK", flush=True)
model = Kronos.from_pretrained(r"D:\Kronos\pretrained\Kronos-base")
n = sum(p.numel() for p in model.parameters())
print(f"MODEL OK params={n}", flush=True)
print("VERIFY DONE", flush=True)
