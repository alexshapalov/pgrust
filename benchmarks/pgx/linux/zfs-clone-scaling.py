import os, fcntl, time, sys, shutil, concurrent.futures as cf
FICLONE = 0x40049409
base = "/tank/pgx-bench/ficlone-test"
shutil.rmtree(base, ignore_errors=True)
src = base + "/src"; os.makedirs(src)
# 600 files, mixed sizes like a small template: mostly 8-16 KB, some 100-800 KB
import random; r = random.Random(1)
for i in range(600):
    sz = r.choice([8192, 8192, 16384, 16384, 24576, 131072, 819200])
    with open("%s/f%d" % (src, i), "wb") as f: f.write(os.urandom(sz))
os.sync(); os.system("sudo -n zpool sync tank")
def clone(a, b):
    with open(a, "rb") as s, open(b, "wb") as d:
        fcntl.ioctl(d.fileno(), FICLONE, s.fileno())
def run(threads, tag):
    dst = "%s/%s" % (base, tag); os.makedirs(dst)
    names = os.listdir(src)
    t = time.perf_counter()
    if threads == 1:
        for n in names: clone(src + "/" + n, dst + "/" + n)
    else:
        with cf.ThreadPoolExecutor(threads) as ex:
            list(ex.map(lambda n: clone(src + "/" + n, dst + "/" + n), names))
    return (time.perf_counter() - t) * 1e3
for rep in range(3):
    for th in (1, 2, 4, 8):
        print("threads=%d rep=%d %.1f ms" % (th, rep, run(th, "d%d_%d" % (th, rep))), flush=True)
shutil.rmtree(base, ignore_errors=True)
