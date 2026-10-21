# Contributing

Contributions are welcome through GitHub issues and pull requests. For a large change, open an issue first to discuss scope and behavior.

## Before submitting

1. Keep changes focused and consistent with the existing Python/async patterns.
2. Run the CPU-only acceptance suite from the repository root:

   ```sh
   python -m unittest discover -s tests -v
   ```

3. Update documentation when changing routes, settings, deployment, processing behavior, or recovery semantics.
4. Do not include secrets, `.env`, runtime `config/settings.json`, SQLite files, media, logs with identifying paths, or registry credentials.
5. Preserve the mandatory CUDA decode/`-xerror` safety gates and source-recovery behavior when changing FFmpeg or file replacement logic.

The acceptance suite mocks FFmpeg and does not replace real-GPU validation. Hardware-path changes should also be exercised on supported NVIDIA hardware using a non-critical test file.

## Pull requests

Describe the reason for the change, relevant behavior/API impact, tests run, and any deployment or migration notes. Include sanitized screenshots for dashboard changes when useful; see [`docs/images/README.md`](docs/images/README.md) for asset guidance.

By submitting a contribution for inclusion, you agree that the contribution is provided under the repository's MIT License unless a different agreement is made in writing.
