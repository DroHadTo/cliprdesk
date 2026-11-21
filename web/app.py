"""Public landing page and anonymous Windows release downloads."""
from hashlib import file_digest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse


STATIC = Path(__file__).resolve().parent / "static"
WORKER = STATIC / "GTA6cliptool-worker.zip"
SOURCE = STATIC / "GTA6cliptool-source.zip"
LOCAL = "http://127.0.0.1:8788/creators"
REPOSITORY = "https://github.com/DroHadTo/cliprdesk"
LICENSE = f"{REPOSITORY}/blob/main/LICENSE"
CREATOR = "https://x.com/DroMeta"
WORKFLOW = f"{REPOSITORY}/blob/main/WORKFLOWS.md"
PAGES = (("/", "Overview"), ("/gta6", "GTA 6"), ("/downloads", "Download"), ("/security", "Privacy and rights"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def headers(request: Request, call_next):
    result = await call_next(request)
    result.headers.update({
        "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    })
    return result


def page(title, path, content):
    links = "".join(
        f'<a href="{url}"' + (' aria-current="page"' if path == url else '') + f'>{label}</a>'
        for url, label in PAGES
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="theme-color" content="#10121a">
        <meta name="description" content="Clip Desk is an open-source Windows workspace for capturing, editing and publishing vertical livestream clips.">
        <meta name="author" content="DroMeta">
        <title>{title} | Clip Desk</title><link rel="stylesheet" href="/clipdesk.css?v=4"></head>
        <body class="public-site"><a class="skip-link" href="#content">Skip to content</a>
        <header class="public-head"><div class="windowbar">
        <a href="/" class="brand"><span class="brand-mark" aria-hidden="true"></span>Clip Desk</a>
        <nav class="public-nav" aria-label="Site">{links}</nav></div></header>
        <main class="public-main" id="content">{content}</main>
        <footer class="public-foot"><div><strong>Clip Desk</strong><small>&copy; 2026 <a href="{CREATOR}">DroMeta</a></small></div>
        <nav aria-label="Project"><a href="{REPOSITORY}">GitHub</a><a href="{WORKFLOW}">Workflow guide</a>
        <a href="{LICENSE}">MIT License</a><a href="/security">Privacy and rights</a></nav>
        </footer></body></html>"""
    )


def edit_preview(*, gta=False):
    title = "GTA 6 clip review" if gta else "Clip review"
    caption = "GTA VI" if gta else "A clean getaway"
    return f"""
        <figure class="hero-preview">
          <div class="preview-top"><strong>{title}</strong><span>9:16 export</span></div>
          <div class="preview-stage" aria-hidden="true">
            <div class="preview-media">
              <img src="/preview.webp" width="960" height="640" alt="" fetchpriority="high">
              <div class="preview-caption">{caption}</div>
            </div>
            <div class="preview-timeline">
              <strong>Timeline</strong>
              <div class="preview-scale"><span>00:00</span><span>00:45</span></div>
              <div class="preview-tracks">
                <span class="preview-track footage">Video</span>
                <span class="preview-track reaction">Facecam</span>
                <span class="preview-track subtitles">Captions</span>
              </div>
            </div>
          </div>
          <figcaption>Workflow preview with illustrative footage</figcaption>
        </figure>"""


@app.get("/")
def home():
    return page("Livestream clipping for Windows", "/", f"""
      <section class="public-hero">
        <div class="hero-copy">
        <p class="eyebrow">Livestream clipping for Windows</p>
        <h1><span>Catch the moment.</span><span>Make the cut.</span></h1>
        <p class="hero-description">Turn Twitch and Kick streams into vertical clips with the reaction, the game and the captions in frame.</p>
        <div class="actions"><a class="btn" href="/downloads">Download for Windows</a>
        <a class="btn secondary" href="{REPOSITORY}">View on GitHub <span aria-hidden="true">↗</span></a></div>
        <p class="hero-meta"><a href="{LICENSE}">MIT-licensed software</a>
        <a href="{LOCAL}">Open local workspace</a></p></div>
        {edit_preview()}
      </section>
      <section aria-labelledby="workflow-title">
        <div class="section-heading"><h2 id="workflow-title" class="section-title">Capture, review and publish</h2>
        <a href="{WORKFLOW}">Workflow guide</a></div>
        <div class="feature-grid">
        <article class="card"><h3>Capture around the moment</h3><p>Discover live creators or enable your own Twitch and Kick channels.
        Every monitored stream can buffer and generate clips when live.</p></article>
        <article class="card"><h3>Review the vertical cut</h3><p>Check the framing and on-screen captions, trim the timing and export an edit package for your editor.</p></article>
        <article class="card"><h3>Publish from your workspace</h3><p>Prepare a manual upload or use the dedicated GTA 6 workspace for scoring and optional TikTok automation.</p>
        <p><a href="/gta6">Explore the GTA 6 workspace</a></p></article>
        </div>
      </section>""")


@app.get("/gta6")
def gta6():
    return page("GTA 6", "/gta6", f"""
      <section class="public-hero">
        <div class="hero-copy"><p class="eyebrow">Dedicated workspace</p>
        <h1><span>GTA 6</span><span>Clip Desk</span></h1>
        <p class="hero-description">A dedicated workspace for GTA streams, branded clips, trend research,
        TikTok publishing and eligible Whop rewards.</p>
        <div class="actions"><a class="btn" href="http://127.0.0.1:8788/gta6">Open local GTA 6 workspace</a>
        <a class="btn secondary" href="/downloads">Download for Windows</a></div></div>
        {edit_preview(gta=True)}
      </section>""")


@app.api_route("/clipdesk.css", methods=["GET", "HEAD"])
def theme():
    return FileResponse(STATIC / "clipdesk.css", media_type="text/css", headers={"Cache-Control": "public, max-age=3600"})


@app.api_route("/preview.webp", methods=["GET", "HEAD"])
def preview():
    return FileResponse(STATIC / "preview.webp", media_type="image/webp", headers={"Cache-Control": "public, max-age=3600"})


def archive_hash():
    with WORKER.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


@app.get("/downloads")
def downloads():
    release = (
        f'<p><code>{archive_hash()}</code></p><div class="actions">'
        '<a class="btn" href="/downloads/worker.zip">Download Windows ZIP</a>'
        '<a class="btn secondary" href="/downloads/worker.sha256">SHA-256 checksum</a></div>'
        if WORKER.is_file() else '<p role="alert">The Windows release is currently unavailable.</p>'
    )
    source = '<a href="/downloads/source.zip">Download source ZIP</a>' if SOURCE.is_file() else ""
    return page("Download", "/downloads", f"""
      <h1 class="page-title">Download Clip Desk</h1>
      <section class="panel download-package"><h2>Complete Windows package</h2>
      <p>Free download. Includes the capture worker, facecam tools, captions, editor exports,
      clip scoring, bring-your-own AI providers, research, Buffer and R2 publishing, and Whop integration.</p>{release}</section>
      <section class="panel"><h2>Open-source software</h2>
      <p>Clip Desk’s original code is available under the <a href="{LICENSE}">MIT License</a>. You can use,
      modify and redistribute it with the included copyright and license notice. Third-party components and footage have their own terms.</p>
      <div class="actions"><a class="btn secondary" href="{REPOSITORY}">View on GitHub <span aria-hidden="true">↗</span></a>{source}</div></section>
      <h2>Get started</h2><ol><li>Extract the ZIP to a local folder outside OneDrive or other sync folders.</li>
      <li>Read README.txt and run setup.bat. It installs the Python environment and offers dependency setup.</li>
      <li>Run start.bat to open <a href="{LOCAL}">your local workspace</a>.</li>
      <li>Configure only the providers you use from the Setup tab. Enable auto publishing after you review your clip rights.</li></ol>
      <p>Requires Windows x64, Python 3.12 or newer, and FFmpeg. Provider accounts and optional AI run separately.</p>""")


@app.get("/downloads/worker.sha256")
def checksum():
    if not WORKER.is_file():
        return PlainTextResponse("The Windows release is unavailable.", 404)
    return PlainTextResponse(f"{archive_hash()}  {WORKER.name}\n")


@app.api_route("/downloads/worker.zip", methods=["GET", "HEAD"])
def archive():
    if not WORKER.is_file():
        return PlainTextResponse("The Windows release is unavailable.", 404)
    return FileResponse(WORKER, filename=WORKER.name, media_type="application/zip")


@app.api_route("/downloads/source.zip", methods=["GET", "HEAD"])
def source_archive():
    if not SOURCE.is_file():
        return PlainTextResponse("The source archive is unavailable.", 404)
    return FileResponse(SOURCE, filename=SOURCE.name, media_type="application/zip")


@app.get("/security")
def security():
    return page("Privacy and rights", "/security", """
      <h1 class="page-title">Privacy and creator rights</h1>
      <section class="panel"><h2>Local workspace</h2>
      <p>The public site offers downloads without a Clip Desk account. Your desktop workspace runs on loopback.
      New installations keep the library under the current Windows user's LocalAppData. Windows encrypts saved
      provider keys and Whop tokens for that user. Install outside cloud sync folders.</p></section>
      <section class="panel"><h2>Publishing permissions</h2>
      <p>Use footage you own or have permission to edit and redistribute. Verify permissions for games, music,
      streamer reactions and sponsorships before monetizing. Credit the creator and honor takedown requests.</p>
      <p>Posting and earnings features use the provider accounts you connect. Review each provider's permissions,
      terms and quotas before enabling automation.</p></section>""")


@app.get("/healthz")
def health():
    return {"ok": True, "service": "clip-desk-public", "download_available": WORKER.is_file()}


@app.get("/login")
def old_login():
    return RedirectResponse("/", status_code=303)
