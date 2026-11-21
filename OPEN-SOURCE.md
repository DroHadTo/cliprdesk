# Software licensing and creator rights

Clip Desk's original code is available under the [MIT License](LICENSE). Keep the copyright and license notice with copies or substantial portions of the software. Bundled third-party components retain their [own licenses and notices](THIRD-PARTY-NOTICES.md).

The software license does not give permission to republish or monetize someone else's content. Use your own footage or obtain permission that covers editing, redistribution, attribution, and any monetization. Check separately for rights in games, music, brands, and likenesses. Keep permission records with the source and record the required credit in **Creator rights**. Use **Takedowns** to block further use locally and remove already-published material from the relevant provider as required.

## Build the release

Run `python build_worker.py` on a reviewed source checkout. The allowlist builds a Windows ZIP and a source ZIP with per-file SHA-256 manifests. Personal configuration, credentials, watchlists, SQLite databases, clips, logs, and Git history stay out of the downloads. Commit and tag only a verified release. Compare the served SHA-256 values with the tested artifacts after deployment.
