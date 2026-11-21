# Security policy

Clip Desk runs on Windows loopback under the current user. The hosted web app provides the information and download pages. It keeps no project accounts and receives no worker heartbeat.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/DroHadTo/cliprdesk/security/advisories/new). Include the affected release or SHA-256, Windows and Python versions, and a description using synthetic data. Keep secrets, personal data, and footage out of the report. The current release is the supported version for security fixes.

## Local data and provider access

- The loopback UI checks the request host, then checks exact origins and a Strict CSRF cookie on writes. The app is intended for a single trusted Windows user. Keep it off LAN interfaces, reverse proxies and public tunnels.
- Windows DPAPI protects saved API keys in `.env` and Whop access and refresh tokens in SQLite under the current Windows account. Existing recognized keys are migrated in place. The app keeps loaded keys out of the environment inherited by media tools. Put the installation outside cloud sync folders and rotate credentials from older plaintext backups. An account or process that can act as you may access your local data. Removing a key in Setup clears the installation's copy. Revoke the key with its provider to invalidate other copies.
- New installs keep media and SQLite under the current user's LocalAppData. Existing libraries keep their original paths to preserve saved edits.
- The local service previews only registered finished MP4s and their render variants. Edit packages may contain source footage and transcripts. Treat exported edit packages as private unless you have distribution rights.
- TikTok automation sends finished files to the configured R2 staging bucket and a delivery URL to Buffer. Use a dedicated bucket with scoped credentials and an independent lifecycle rule. Public delivery links allow anyone with the URL to fetch the file while it is staged.
- The configured model service receives footage frames and scene context for review. Private Ollama can use HTTP on a trusted LAN or VPN. An Ollama bearer token and any external writer API key require HTTPS. Model requests follow no redirects. Keep the raw Ollama port off the public internet and outside public tunnel routes.
- A valid local creator permission gates TikTok uploads. Reward submissions require an additional monetization permission. Attribution follows the saved credit. Local takedowns block new clip access and unsent queue work. Remove clips already sent to Buffer or TikTok from the provider, then reconcile their status.

## Windows downloads

Release ZIPs contain plain source and an allowlisted set of assets. The setup installs pinned, hash-checked Python packages and verifies the pinned TwitchDownloaderCLI archive before extraction. FFmpeg and Python may be installed by Windows Package Manager after confirmation. Verify the exact release filename against its SHA-256 and keep Windows antivirus protection enabled. An SHA-256 from the same website helps verify transfer integrity. Independent signed releases are a separate trust control.

Use the antivirus vendor's quarantine report to investigate any alerts. Only share the flagged install artifact after checking it contains no credentials, footage or personal data. Never post quarantine contents or secrets on a public issue tracker. [Kaspersky file analysis](https://opentip.kaspersky.com/)
