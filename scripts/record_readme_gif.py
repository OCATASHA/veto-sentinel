"""Record the README GIF from a real dashboard session.

    uv run --with playwright --with imageio-ffmpeg python scripts/record_readme_gif.py

It starts its own replay of the SVB 2023 fixture (real GDELT headlines) on spare ports with a
scratch database and a throwaway login, applies the dashboard's "Banks & credit" focus from the
start (as in docs/EVALUATION.md section 2b), drives the console in headless Google Chrome (the
installed browser, so nothing is downloaded) and writes docs/media/dashboard.gif. Nothing is
staged: every level, score and headline in the GIF is what the replay computed. Run it with the
ML models installed (uv sync --extra ml) to match the documented results.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = "data/historical/gdelt_gkg_svb_2023.jsonl"
BANKS = "banks, bank run, deposits, credit markets, loan defaults, bank capital"  # the dashboard preset
START = "2023-03-10T03:00:00Z"  # Friday 10 March, before the news turns (first HALT evidence 09:25Z)
FREEZE_CSS = ".bg .blob { animation: none !important; }"  # drifting background: noise for a GIF


def start_server(tmp: Path, port: int, pub: int, password: str) -> subprocess.Popen:
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "VETOSENTINEL_LOGIN_USER": "demo",
           "VETOSENTINEL_LOGIN_PASSWORD": password}
    env.pop("VETOSENTINEL_OPERATOR_TOKEN", None)
    cmd = [sys.executable, "-m", "vetosentinel.cli", "replay", "--config", "config/default.toml",
           "--fixture", FIXTURE, "--db", str(tmp / "recording.duckdb"), "--port", str(port),
           "--pub", f"tcp://127.0.0.1:{pub}", "--paused"]
    log = (tmp / "server.log").open("w")
    return subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def wait_healthy(url: str, proc: subprocess.Popen, timeout: float = 300) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise SystemExit("the replay server exited; see its log")
        try:
            if httpx.get(url + "/api/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit("the replay server did not start in time")


async def record(url: str, password: str, api: httpx.Client, args: argparse.Namespace) -> list[tuple[float, bytes]]:
    from playwright.async_api import async_playwright  # pyright: ignore[reportMissingImports]

    frames: list[tuple[float, bytes]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context(viewport={"width": args.width, "height": args.height},
                                        device_scale_factor=1, color_scheme="dark",
                                        bypass_csp=True)  # lets Playwright poll the page
        await ctx.add_init_script("try { localStorage.setItem('vs-theme', 'dark'); "
                                  "localStorage.setItem('vs-expert', '0'); } catch (e) {}")
        r = await ctx.request.post(url + "/api/login", data={"username": "demo", "password": password})
        assert r.ok, await r.text()
        page = await ctx.new_page()
        await page.goto(url + "/dashboard")
        await page.wait_for_function("!/Connecting/.test(document.querySelector('#heroTitle').textContent)")
        await page.add_style_tag(content=FREEZE_CSS)
        await asyncio.sleep(2.5)  # entrance animations settle

        recording = True

        async def capture() -> None:
            nxt = time.monotonic()
            while recording:
                frames.append((time.monotonic(), await page.screenshot(type="png")))
                nxt += 1 / args.fps
                await asyncio.sleep(max(0.0, nxt - time.monotonic()))

        async def post(path: str, body: dict | None = None) -> None:
            res = await asyncio.to_thread(api.post, path, json=body)
            res.raise_for_status()

        async def play_until(state: str, limit: float) -> None:
            """Play at one simulated hour per second; pause as soon as the published level is ``state``."""
            await post("/api/replay-speed", {"speed": 3600})
            await post("/api/replay/play")
            end = time.monotonic() + limit
            while time.monotonic() < end:
                st = (await asyncio.to_thread(api.get, "/api/status")).json()
                if st["state"] == state:
                    break
                await asyncio.sleep(0.15)
            await post("/api/replay/pause")

        task = asyncio.create_task(capture())
        await asyncio.sleep(2.0)  # Overview: calm news before the crisis
        await page.click("#tabbtn-bots")
        await asyncio.sleep(0.6)
        await page.evaluate("window.scrollTo({top: 250, behavior: 'smooth'})")  # heartbeat + order gate
        await asyncio.sleep(1.4)
        # The demo bot loses its heartbeat... (clicked from script so the page doesn't scroll away
        # from the heartbeat monitor at the top)
        await page.evaluate("document.querySelector('button[data-fault=\"silence_5s\"]').click()")
        await asyncio.sleep(8.5)  # ...stops itself after 3 s (the gate closes), then recovers
        await page.click("#tabbtn-overview")
        await asyncio.sleep(0.8)
        await play_until("HALT", 20)  # the bank news turns: Be careful, then Stop
        await asyncio.sleep(3.2)  # the reasons for Stop
        await page.click("#tabbtn-news")
        await asyncio.sleep(2.8)
        await page.click("#tabbtn-timeline")
        await page.click('#rangeSeg button[data-range="72"]')  # the last three days
        await asyncio.sleep(3.2)
        recording = False
        await task
        await browser.close()
    return frames


def encode(frames: list[tuple[float, bytes]], out: Path, args: argparse.Namespace, tmp: Path) -> None:
    import imageio_ffmpeg  # pyright: ignore[reportMissingImports]

    fdir = tmp / "frames"
    fdir.mkdir()
    lines = []
    for i, (t, png) in enumerate(frames):
        name = fdir / f"f{i:05d}.png"
        name.write_bytes(png)
        dur = (frames[i + 1][0] - t) if i + 1 < len(frames) else 1.5  # hold the last frame
        lines += [f"file '{name}'", f"duration {dur:.3f}"]
    lines.append(f"file '{fdir / f'f{len(frames) - 1:05d}.png'}'")  # concat demuxer needs the last file twice
    (tmp / "frames.txt").write_text("\n".join(lines) + "\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = (f"fps={args.fps},scale={args.out_width}:-1:flags=lanczos,split[a][b];"
          "[a]palettegen=max_colors=256:stats_mode=diff[p];"
          "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(tmp / "frames.txt"), "-vf", vf, "-loop", "0", str(out)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Record the README GIF from a real dashboard session.")
    ap.add_argument("--out", default="docs/media/dashboard.gif")
    ap.add_argument("--port", type=int, default=8891)
    ap.add_argument("--pub-port", type=int, default=5591)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--out-width", type=int, default=960)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder (frames, server log)")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="vs-gif-"))
    password = secrets.token_urlsafe(18)
    url = f"http://127.0.0.1:{args.port}"
    proc = start_server(tmp, args.port, args.pub_port, password)
    try:
        wait_healthy(url, proc)
        with httpx.Client(base_url=url, timeout=900) as api:
            api.post("/api/login", json={"username": "demo", "password": password}).raise_for_status()
            api.post("/api/focus", json={"text": BANKS}).raise_for_status()
            print("replaying 27 Feb - 10 Mar 2023 with the Banks & credit focus (a minute or two)...")
            api.post("/api/replay-seek", json={"to": START}).raise_for_status()
            st = api.get("/api/status").json()
            print(f"at {st['as_of']}: {st['state']}, risk {st['risk_score']}")
            frames = asyncio.run(record(url, password, api, args))
        out = ROOT / args.out
        encode(frames, out, args, tmp)
        print(f"{len(frames)} frames -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        if args.keep:
            print(f"scratch folder kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
