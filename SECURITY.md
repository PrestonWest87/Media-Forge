# Security policy

## Deployment security

- The dashboard/API has no built-in authentication. Bind it to loopback or a trusted network; use an authenticated reverse proxy for remote access.
- The application can rename, replace, restore, and purge files within configured writable media mounts. Grant access only to the libraries and operators that need it.
- The Compose service uses the host PID namespace so NVML process IDs can be inspected. GPU-Forge signals only its registered FFmpeg child processes, but host PID visibility is still a privileged deployment choice.
- The Uvicorn command trusts forwarded headers from all peers. Restrict direct access to the container port to trusted clients/proxies.
- Keep `.env`, runtime settings, database files, media paths, registry credentials, and access tokens out of Git. `.gitignore` excludes local environment and state files; `.dockerignore` excludes local state from image build context.
- Never put registry tokens or other credentials in Docker build arguments, image labels, Compose files, issue reports, or source-control history.

## Reporting a vulnerability

Please do not report sensitive vulnerabilities in a public issue. Use GitHub's private vulnerability reporting for [PrestonWest87/Media-Forge](https://github.com/PrestonWest87/Media-Forge/security/advisories/new) when available. If private reporting is unavailable, contact the repository maintainer through a private channel linked from the GitHub profile and request a secure reporting route.

Include the affected commit/image tag, a concise impact description, and reproducible steps that do not expose private media or credentials. Redact hostnames, IPs, file paths, logs, tokens, and personal data. No response-time SLA is promised; reports will be reviewed as maintainer time permits.

## Supported versions

Security fixes are expected to target the current `main` branch. Published image tags are immutable release snapshots; users should track published updates and rebuild when updated dependencies/base images are released.
