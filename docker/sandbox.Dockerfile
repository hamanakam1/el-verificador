# El Verificador sandbox image
#
# The verification sandbox: an isolated, network-free environment in which
# acceptance checks are executed. The plan recorded that no such image
# existed anywhere in the factory — `taskrivet/sandbox:latest` was referenced
# by two files and built by nothing — which meant the "runs the CI pipeline
# inside the hydrated Docker container" claim could not be exercised, in CI
# or anywhere else.
#
# This is the default Python sandbox. It is deliberately small, runs as a
# non-root user with no network, and carries the standard acceptance
# toolchain. Repositories with a different toolchain should publish their own
# image and point `docker_image` at it; this one exists so that the default
# path is real rather than aspirational.
#
# Build:
#   docker build -f docker/sandbox.Dockerfile -t el-verificador/sandbox:dev .
#
# The image is pinned by digest in any deployment that needs reproducibility;
# a floating tag is a development convenience only.

FROM python:3.12-slim

# git is needed because acceptance checks frequently run `git diff`, and the
# verifier itself inspects the repository history.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

# The default acceptance toolchain: unit tests, lint, security scan.
RUN pip install --no-cache-dir \
        pytest==8.3.4 \
        ruff==0.8.4 \
        bandit==1.8.3

# Unprivileged user. The sandbox executes agent-authored code; it must never
# run as root, and it must not be able to write to the image.
RUN useradd --create-home --shell /bin/bash verifier
WORKDIR /workspace
RUN chown verifier:verifier /workspace
USER verifier

# The container is started with `--network none` by the runner; this makes the
# intent explicit for anyone running the image by hand.
LABEL org.opencontainers.image.title="el-verificador-sandbox" \
      org.opencontainers.image.description="Isolated acceptance-check sandbox for El Verificador" \
      org.opencontainers.image.source="https://github.com/hamanakam1/el-verificador"

CMD ["sleep", "infinity"]
