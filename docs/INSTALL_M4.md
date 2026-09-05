# Installing LTX-2 on a Mac mini M4 (16 GB)

> **Target: Mac mini M4, 16 GB**, macOS 14+. The goal is to run the backend that talks to the proxy (port **8198**) and to the dashboard's **Sequence** tab.
>
> **Realistic hardware expectations:** on 16 GB you run **int4 + `--low-ram` + tiling**. Short clips (<= 4 s at 704x416) are fine, anything larger risks OOM. The 16 GB tier is **not officially documented** by the upstream repos — grey area, but usable.

---

## Which route to take?

| Route | What for | Effort | Wired to the dashboard? |
|------|-----------|--------|-------------------|
| **A · Pinokio** | One-click start, pre-wired web panel | 15 min | Yes (port 8198) |
| **B · Manual** | Full control, integrates with your existing proxy | 45 min | Yes (port 8198) |
| **C · bare LTX-2-MLX** | CLI only, to test the pipelines from a shell | 20 min | No (CLI only) |

**Recommendation**: route **A** (Pinokio), to avoid fighting dependencies. If Pinokio refuses, take route **B**.

---

## 0 · Prerequisites (check once)

```bash
# macOS Apple Silicon
uname -m                       # → arm64
sw_vers -productVersion        # → 14.x ou +

# Python 3.11 disponible (dgrauet/ltx-2-mlx exige >= 3.11)
python3.11 --version || brew install python@3.11

# uv (gestionnaire de venv ultra rapide, requis par dgrauet)
which uv || curl -LsSf https://astral.sh/uv/install.sh | sh

# ffmpeg (for preview / extend / assembly)
which ffmpeg || brew install ffmpeg

# Git LFS (the HuggingFace weights are large)
which git-lfs || brew install git-lfs && git lfs install

# Free disk space — you need ~30 GB (weights + venv + cache)
df -h ~ | tail -1 | awk '{print "free:", $4}'
```

⚠️ **With less than 30 GB free**, clear space before installing — a download interrupted at 80% costs you 20 GB to redo.

---

## Route A · Pinokio (the shortest)

1. **Install Pinokio** from https://pinokio.computer/ (the official DMG).
2. Open Pinokio -> **Discover** -> **Download from URL** ->
   ```
   https://github.com/mrbizarro/phosphene
   ```
3. Click **Install**. Pinokio will:
   - clone the repo + LTX-2-MLX `v0.14.8`
   - create an MLX 0.31.1 venv
   - download the **Q4 LTX weights (~28 GB)** from HuggingFace
   - start the server on `127.0.0.1:8198`
4. Wait **20-40 min** (network dependent).
5. Click **Start** — a web tab opens on `http://127.0.0.1:8198`.

✅ **Immediate check on the proxy side**:
```bash
curl -s http://127.0.0.1:8198/status | head -c 200
# must return JSON (queue/history/current)
```

If you see JSON, the dashboard and its **Sequence** tab will talk to this backend automatically.

---

## Route B · Manual install (fine-grained integration)

Use this if Pinokio refuses, or if you want to pin the versions yourself.

### B.1 · Cloner phosphene + LTX-2-MLX

```bash
cd ~                                        # or another project directory
git clone https://github.com/mrbizarro/phosphene.git
cd phosphene

# LTX-2-MLX pinned to the tag phosphene supports
git clone https://github.com/dgrauet/ltx-2-mlx.git
cd ltx-2-mlx
git checkout v0.14.8
cd ..
```

### B.2 · Create the venv (Python 3.11, MLX)

```bash
# dedicated venv, Python 3.11 required
uv venv .venv --python 3.11
source .venv/bin/activate

# Installer LTX-2-MLX et ses extras
cd ltx-2-mlx
uv pip install -e ".[all]"     # MLX 0.31.1, mlx-lm, transformers, gradio, …
cd ..

# Install the phosphene deps (panel + HTTP server)
uv pip install -r requirements.txt    # adjust if the file has another name
```

> If `requirements.txt` does not exist, follow phosphene's `README.md` under *Manual install* — the procedure is listed explicitly there.

### B.3 · Download the Q4 weights (~28 GB)

Automatic on first run, or manual to pre-load:

```bash
# Auto-download through huggingface_hub (triggered by the first run)
# OU manuel :
uv pip install -U huggingface_hub
hf download dgrauet/ltx-2.3-mlx-q4 --local-dir ./models/ltx-2.3-q4
```

> **Hint**: `hf download` (the new CLI) replaces `huggingface-cli download` (deprecated in 2026).

### B.4 · Start the server

```bash
# From ~/phosphene with the venv activated
python -m phosphene.server --port 8198 --low-ram
# (adjust the module: see the repo's scripts/start.sh or pinokio.js)
```

✅ **Check**:
```bash
curl -s http://127.0.0.1:8198/status
```

---

## Route C · bare LTX-2-MLX CLI (testing, not for the dashboard)

If you want to **just test the pipelines** without the web panel:

```bash
git clone https://github.com/dgrauet/ltx-2-mlx.git
cd ltx-2-mlx
uv sync --all-extras            # installs the `ltx-2-mlx` CLI
```

### Ready-to-run commands (16 GB-safe)

**Text -> video** (the simplest):
```bash
ltx-2-mlx generate \
  --prompt "A neon vortex opening above a bearded man in a flat cap" \
  --distilled \
  --model dgrauet/ltx-2.3-mlx-q4 \
  --low-ram \
  -o test_t2v.mp4
```

**Image -> video (i2v)**:
```bash
ltx-2-mlx generate \
  --prompt "Slow zoom in, neon lights flicker" \
  --image ./peye.jpg \
  --distilled --low-ram \
  --model dgrauet/ltx-2.3-mlx-q4 \
  -o test_i2v.mp4
```

**Frame-to-frame (keyframe interpolation)** :
```bash
ltx-2-mlx keyframe \
  --prompt "Smooth golden vortex transition" \
  --start ./frame_a.jpg --end ./frame_b.jpg \
  --low-ram \
  --model dgrauet/ltx-2.3-mlx-q4 \
  -o test_kf.mp4
```

**Extend (lengthen an existing video)**:
```bash
ltx-2-mlx extend \
  --prompt "Continue the scene, camera pulls back" \
  --video ./source.mp4 \
  --extend-frames 2 \
  --low-ram \
  --model dgrauet/ltx-2.3-mlx-q4 \
  -o test_extend.mp4
```

---

## 16 GB settings (worth memorising)

| Param | 16 GB value | Why |
|-------|--------------|----------|
| `--model` | `dgrauet/ltx-2.3-mlx-q4` | int4 = ~12 GB of weights · the only tier that fits |
| `--low-ram` | **required** | streams the blocks from disk (otherwise OOM) |
| `--distilled` | recommended | 8+4 steps instead of ~30 -> 3x faster |
| `--tile-frames 2` | if > 97 frames | tiles the temporal attention |
| `--tile-spatial M` | if > 704x416 | tiles the spatial attention |
| `--tile-overlap 4` | with tiling | avoids visible seams |
| Resolution | **704x416** | sweet spot, 97 frames (4 s @ 24 fps) |

**Avoid on 16 GB**: `--two-stage` at full resolution, more than 121 frames, non-distilled models.

---

## Pointing the dashboard at the render host

Once the server is up (route A or B), the local proxy you already have (`dynamic_video_generator.proxy` on port **8200**) forwards every `/queue/add`, `/status`, `/upload` and `/outputs` call to `127.0.0.1:8198`. **Nothing to reconfigure on the dashboard side.**

Test bout-en-bout :
```bash
# On the render host: start the proxy + phosphene
./start.sh                               # or the manual commands

# On your main Mac: open the dashboard
open http://127.0.0.1:8200               # or through the auto-discovered Cloudflare tunnel
# -> Sequence tab -> Start the render -> "test" -> ▶ Start the render
```

The `proxy v18` badge must be green in the top right. Otherwise see *Troubleshooting* below.

---

## Honest troubleshooting

| Symptom | Likely cause | Fix |
|----------|----------------|-----|
| `OOM killed` after ~30 s | 16 GB of RAM saturated | Reduce: 704x416 · 97 frames · `--distilled` · `--low-ram` |
| `OutOfMemoryError MPS` | Not enough unified memory | Quit Safari/Chrome (heavy consumers) before starting |
| `/status` returns 404 on 8198 | Wrong port, or the server is not running | `lsof -iTCP:8198 -sTCP:LISTEN` must show the Python process |
| HF download interrupted | Connection dropped | `hf download …` resumes automatically |
| A render takes > 30 min | Normal in int4 on a 16 GB M4 | No fix — that is the price of 16 GB. Reduce the frame count. |
| `mlx.core` import error | venv on the wrong Python | Recreate it: `uv venv .venv --python 3.11` |
| The dashboard does not see the job | Stale proxy | Deploy the latest proxy version |

---

## Costs (worth knowing before you click)

- **Disk**: ~28 GB (Q4 weights) + ~3 GB (venv) + ~2 GB (MLX cache) ≈ **33 GB**
- **Install time**: 20-40 min (network bound)
- **First render**: 5-15 min for 4 s @ 704x416 in distilled int4 (estimate; no official 16 GB M4 benchmark exists)
- **RAM**: ~14 GB peak during a render — **close everything else**

---

## Sources

- LTX-2 officiel : https://github.com/Lightricks/LTX-2
- The MLX port phosphene uses: https://github.com/dgrauet/ltx-2-mlx
- Web panel (recommended on the render host): https://github.com/mrbizarro/phosphene
- Q4 weights (16 GB): https://huggingface.co/dgrauet/ltx-2.3-mlx-q4

> Written **2026-06-24**. If a `dgrauet/ltx-2-mlx` release later than `v0.14.8` breaks phosphene's HTTP API, stay on the pinned tag.
