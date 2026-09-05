# Demo renders

What lands here is **output of this pipeline**, nothing else, plus a sidecar
carrying what is actually known about each file.

| File | Resolution | Frames | Duration | Pixel-frames | Mode |
|---|---|---|---|---|---|
| `fleet-at-anchor-frame.jpg` | 768×416 | 1 | — | — | frame |
| `fleet-at-anchor.mp4` | 768×416 | 472 | 19.7 s | 151 M | long |
| `rite-of-the-swords-frame.jpg` | 768×384 | 1 | — | — | frame |
| `rite-of-the-swords.mp4` | 768×384 | 168 | 7.0 s | 50 M | t2v |
| `samurai-blossom-duel-frame.jpg` | 768×416 | 1 | — | — | frame |
| `samurai-blossom-duel.mp4` | 768×416 | 480 | 20.0 s | 153 M | long |

The three clips are LTX-Video 2.3 on the M4, 24 fps, with the synchronised
audio track that model generates. The three `-frame.jpg` stills are single
frames pulled out of those same clips with ffmpeg — real frames of real
renders, not separate generations, and their sidecars say so.

The two 20-second clips were chained in Long mode: a single pass at 150 M
pixel-frames could not run on 16 GB, so the budget readout reports them as
chained rather than flagging them over.

## What the sidecars do not claim

The prompts, the step counts and the render times lived on the render machine,
which is gone. They are absent rather than estimated, so the dashboard shows
"—" for seconds per diffusion step instead of a number nobody measured.
`mode` is the one deduced field, and it says so: at these loads the deduction
follows from the machine's own limits.

## Why the rule is strict

Every figure the dashboard shows is read off these files. A clip from a hosted
service would put someone else's numbers under this project's name — and a
reader can tell. This pipeline renders 704×416, 576×320, 768×416 or 1024×576
at **24 fps** on a 16 GB machine; a 1280×720 clip at 30 fps did not come from
here, and hosted services burn a watermark into the frame. `tests/test_demo_media.py`
checks the signature of whatever is committed here.

## Adding more

With a render backend reachable:

```bash
./scripts/make_demo.sh
```

Four clips at ~35 M pixel-frames each, sidecars written from `ffprobe` plus the
measured wall clock, one still per clip.

## What replay mode does

`dvg --mock` serves whatever is here, with real frames pulled by ffmpeg. When
the directory is empty it falls back to drawn placeholders carrying the render
parameters they stand for, each stamped MOCK — a legible stand-in, never a
picture pretending to be a render.
