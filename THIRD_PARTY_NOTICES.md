# Third-party notices

The root [`LICENSE`](LICENSE) applies to GPU-Forge's repository code and documentation. It does not replace the licenses of third-party packages, browser assets, operating-system packages, or container-image components.

## Direct Python runtime dependencies

The versions below are the minimum constraints in `requirements.txt`, not exact lock pins. The image build resolves compatible current versions. License information was checked against the package metadata/license files available during preparation of this notice; consult the resolved wheels and release SBOM for the exact versions in a particular image.

| Component | Declared requirement | License | Copyright/maintainer | Project |
| --- | --- | --- | --- | --- |
| FastAPI | `fastapi>=0.110.0` | MIT | Sebastián Ramírez | [github.com/fastapi/fastapi](https://github.com/fastapi/fastapi) |
| Uvicorn (standard extras) | `uvicorn[standard]>=0.29.0` | BSD-3-Clause | Encode OSS Ltd. and contributors | [github.com/encode/uvicorn](https://github.com/encode/uvicorn) |
| aiosqlite | `aiosqlite>=0.20.0` | MIT | Amethyst Reese | [github.com/omnilib/aiosqlite](https://github.com/omnilib/aiosqlite) |
| nvidia-ml-py | `nvidia-ml-py>=12.535.133` | BSD | NVIDIA Corporation | [pypi.org/project/nvidia-ml-py](https://pypi.org/project/nvidia-ml-py/) |
| Pydantic | `pydantic>=2.7.0` | MIT | Pydantic Services Inc. and contributors | [github.com/pydantic/pydantic](https://github.com/pydantic/pydantic) |
| python-multipart | `python-multipart>=0.0.9` | Apache-2.0 | Andrew Dunham and contributors | [github.com/Kludex/python-multipart](https://github.com/Kludex/python-multipart) |

`uvicorn[standard]` and the direct packages bring transitive dependencies. Python package wheels retain upstream license files in their `.dist-info` metadata when provided. The declared `>=` ranges are intentionally not a complete version lock; generate and retain a software bill of materials (SBOM) for each released image to identify exact resolved versions and transitive licenses.

## Browser asset

| Asset | Use | License/source |
| --- | --- | --- |
| Tailwind CSS browser CDN (`https://cdn.tailwindcss.com`) | Dashboard styling | MIT; [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) |

The dashboard loads Tailwind from the CDN at browser runtime; it is not vendored into this repository. The dashboard also contains project-specific inline CSS and SVG icons.

## Container and system components

| Component | Use | Attribution/license information |
| --- | --- | --- |
| FFmpeg `n8.1.2` | Media probing, NVDEC decoding, and NVENC encoding | LGPL-2.1-or-later; [FFmpeg source](https://github.com/FFmpeg/FFmpeg/tree/n8.1.2). The image builds shared FFmpeg libraries without `--enable-gpl` or `--enable-nonfree` and includes the matching source archive and build configuration under `/usr/share/doc/gpu-forge/ffmpeg/`. |
| FFmpeg `nv-codec-headers` `n13.0.19.1` | Compile-time NVIDIA NVDEC/NVENC API headers | MIT; [source](https://github.com/FFmpeg/nv-codec-headers/tree/n13.0.19.1). Its matching source archive is included under `/usr/share/doc/gpu-forge/ffmpeg/sources/`. NVIDIA driver libraries are supplied by the host's NVIDIA Container Toolkit, not bundled in this image. |
| `python:3.12-slim-bookworm` | Python runtime files copied into the final image | [Official Python Docker image](https://hub.docker.com/_/python); Python is PSF-licensed and the image includes Debian components with their own licenses. |
| Debian Bookworm packages | Shared libraries installed by the Dockerfile | Each package's license/copyright notices are supplied by its Debian package metadata under `/usr/share/doc` where available. |
| NVIDIA driver/NVML libraries | GPU access supplied by NVIDIA Container Toolkit from the host | NVIDIA components are governed by the applicable NVIDIA license terms; they are not redistributed as source in this repository. |

The FFmpeg build deliberately does not enable `cuda_nvcc`, `libnpp`, `libfdk_aac`, `--enable-gpl`, or `--enable-nonfree`. AAC fallback encoding uses FFmpeg's native `aac` encoder. FFmpeg 8.1.2's configure rules classify `cuda_nvcc`/`libnpp` and certain external libraries as nonfree; this service needs the NVENC/NVDEC API wrappers and CUDA hardware frames, not CUDA-compiled filters. The Docker build checks that `hevc_nvenc`, `aac`, CUDA hardware acceleration, and the LGPL license are present and that GPL/nonfree flags are absent.

The final image combines components under more than one license. The OCI `org.opencontainers.image.licenses` label identifies the repository's MIT-licensed application source; it does not relicense FFmpeg, Python packages, Debian components, or base-image contents. Corresponding FFmpeg and NV codec-header source archives plus exact build information are included in the image to retain source/build provenance.

## Image release records

For Docker Hub releases, the recommended Buildx command enables SBOM and provenance attestations (`--sbom=true --provenance=mode=max`). Preserve the published image's SBOM and review it when changing dependency ranges, base-image tags, or OS packages. The Docker image itself retains upstream package/license files to the extent provided by upstream distributions.
