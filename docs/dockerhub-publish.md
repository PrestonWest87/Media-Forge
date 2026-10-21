# Docker Hub image publishing

## Image identity

- **Docker Hub repository:** [`weasts/media-forge`](https://hub.docker.com/r/weasts/media-forge)
- **Product title:** GPU-Forge
- **Source repository:** [`PrestonWest87/Media-Forge`](https://github.com/PrestonWest87/Media-Forge)
- **Candidate release tags:** `2.0.0` and `latest` (the application reports version 2.0.0 in `app/main.py`).
- **Platform currently targeted:** `linux/amd64`.

## Release build

The selected first-release tags are `2.0.0` and `latest`. The Dockerfile builds FFmpeg 8.1.2 with NVDEC/NVENC using `nv-codec-headers` 13.0.19.1, without `--enable-gpl`, `--enable-nonfree`, `--enable-cuda-nvcc`, `--enable-libnpp`, or FDK AAC. A local build verified the LGPL output, CUDA hardware-acceleration entry, `hevc_nvenc` encoder, and embedded matching source archives/build information. Rebuild and repeat these checks for each release; retain the source archives with the image.

The Dockerfile sets OCI title, description, source, documentation, MIT application-license, vendor, version, and source-revision labels. Third-party and base-image licenses are listed in [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

Use [`dockerhub-description.md`](dockerhub-description.md) as the copy-ready Docker Hub repository summary/readme.

## One-time Docker Hub setup

Create the `media-forge` repository under the `weasts` namespace with the intended visibility, and grant the publishing account write access. The project owner selected a public repository. Authenticate from the release environment with a Docker Hub personal access token that has write permission:

```sh
docker login --username weasts
```

At the password prompt, enter the access token. Do not put the token in command-line arguments, `.env`, source files, build arguments, or chat. A configured Docker credential helper is preferred.

## Build and publish

Once the base/build has been resolved and approved for distribution, run the tests and inspect the staged/public file list before publishing. From the repository root:

```sh
IMAGE=weasts/media-forge
VERSION=2.0.0
REVISION="$(git rev-parse --short HEAD)"

docker buildx build \
  --platform linux/amd64 \
  --sbom=true \
  --provenance=mode=max \
  --build-arg APP_VERSION="$VERSION" \
  --build-arg VCS_REF="$REVISION" \
  --tag "$IMAGE:$VERSION" \
  --tag "$IMAGE:latest" \
  --push \
  .
```

`--sbom=true` and `--provenance=mode=max` publish build metadata alongside the image. Do not pass secrets as build arguments; BuildKit secrets are the appropriate mechanism for any future build-time credentials (none are required by this Dockerfile).

The image carries application/third-party notices at `/usr/share/doc/gpu-forge/` and matching FFmpeg/NV codec-header source archives at `/usr/share/doc/gpu-forge/ffmpeg/sources/`. To extract them from a published image:

```sh
container_id="$(docker create weasts/media-forge:2.0.0)"
docker cp "$container_id":/usr/share/doc/gpu-forge ./gpu-forge-image-notices
docker rm "$container_id"
```

Only move the `latest` tag for an intended release. For a development build, use a distinct tag such as `main-<short-commit>` and do not overwrite a release version.

## Verify the published image

```sh
docker buildx imagetools inspect weasts/media-forge:2.0.0
docker pull weasts/media-forge:2.0.0
docker image inspect weasts/media-forge:2.0.0 \
  --format '{{json .Config.Labels}}'
```

Inspect the tags, manifest platform, OCI labels, and available SBOM/provenance attestations in Docker Hub/Buildx. Test the pulled image with the deployment Compose configuration before pointing production at it.

## Use a published image with Compose

Set this in the local, ignored `.env` file:

```dotenv
GPU_FORGE_IMAGE=weasts/media-forge:2.0.0
```

Then run `docker compose pull` followed by `docker compose up -d`. Keep `.env` local. A Docker CLI push publishes image layers and tags; Docker Hub's repository description/README and visibility are configured separately in Docker Hub. Use the project's short summary and link to the GitHub README when setting that repository metadata.
