Clip Desk for Windows

1. Extract the folder to a local path outside OneDrive, Dropbox, or other sync services.
2. Install 64-bit Python 3.12 or newer and FFmpeg, or use the offers in setup.bat.
3. Run setup.bat. It installs pinned Python packages with SHA-256 verification into
   .venv and verifies the pinned TwitchDownloaderCLI archive. Startup at Windows
   sign-in is optional.
4. Run start.bat for http://127.0.0.1:8788/today. For a custom .env port, use the link
   opened by the launcher. No Clip Desk account is required.
5. Add creators you have permission to clip in Watchlist. Record posting and any
   monetization permission under Creator rights. Fresh installs start with an
   empty watchlist and automatic publishing off.
6. Connect your own Twitch, Kick, Ollama, Buffer, Cloudflare R2 and Whop in Setup as
   needed. Read README.md, MEDIA-TOOLS.md and EARNINGS.md for provider requirements.
7. Review the clip, permissions and any bounty brief before allowing automation.

Update using the same Windows account. Keep .env, data and watchlist.json private
and retain them across the update. The worker encrypts recognized saved keys
and Whop tokens on startup. Re-enter provider credentials when moving to a
different Windows account or PC. Check /healthz and Today after starting.
Fresh installs use the current Windows account's LocalAppData for clip data.

Optional sign-in startup can be removed in Task Scheduler under GTA6cliptool.
The app is intended for a single trusted Windows user. See SECURITY.md.
