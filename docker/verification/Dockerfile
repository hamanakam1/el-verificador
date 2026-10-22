# El Verificador — verification sandbox image
#
# The image the acceptance checks run inside. It exists so that the network
# can be switched off at run time: the toolchain the acceptance commands need
# is baked in, so `pytest`, `ruff` and `bandit` never have to be fetched while
# untrusted code is on the machine.
#
# Build:
#   ./scripts/build_sandbox_image.sh
#
# The base image is pinned by tag here and by digest in a release. A tag can
# be moved under you; a digest cannot. The release pipeline resolves the
# digest and records it with the receipt.
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/hamanakam1/el-verificador" \
      org.opencontainers.image.title="El Verificador verification sandbox" \
      org.opencontainers.image.description="Isolated image for El Verificador acceptance checks" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# The acceptance toolchain, installed at build time so the runtime container
# needs no network. `bandit` and `ruff` are the security and lint checks the
# default acceptance contract runs.
RUN pip install --no-cache-dir \
        "pytest>=8.0" \
        "ruff>=0.6" \
        "bandit>=1.7" \
    && rm -rf /root/.cache

# The repository under verification is mounted here.
WORKDIR /workspace

# A non-root uid, so a process in the sandbox is not the operator.
RUN useradd --create-home --uid 10001 verifier \
    && mkdir -p /workspace \
    && chown -R verifier:verifier /workspace
USER verifier

# `sleep infinity` keeps the container alive; the verifier execs into it and
# tears it down when the run ends.
ENTRYPOINT ["sleep", "infinity"]
