# Phantom — OSINT username checker
# ---------------------------------
# Single-stage Debian-slim image bundling Phantom + Playwright Chromium
# + Tesseract. Total ~1.5GB; most of that is the Chromium binary
# Playwright pulls down. The image runs as a non-root user.
#
# Build:   docker build -t phantom .
# Run:     docker run --rm phantom <username>
#
# Persistence: the on-disk response cache lives at /home/phantom/.cache
# inside the container. Mount a host directory to keep it across runs:
#   docker run -v $PWD/.phantom-cache:/home/phantom/.cache/phantom \
#              --rm phantom <username>
#
# Network isolation: the most common reason to dockerize Phantom is to
# route through Tor or a private network. Combine with --network=host
# (or a docker-compose stack with a tor sidecar) and pass --proxy on
# the phantom CLI.
#
# Why slim, not alpine: curl_cffi's prebuilt wheels are glibc-only.
# Alpine would need a from-source build of libcurl + openssl, doubling
# the image size.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System packages:
#   tesseract-ocr           - optional dep enabling --photo-ocr
#   libgl1, libglib2.0-0    - OpenCV runtime requirement
#   ca-certificates         - TLS
#   fontconfig              - Playwright Chromium fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
        ca-certificates \
        fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Phantom runs as a non-root user inside the container.
RUN useradd --create-home --shell /bin/bash phantom

WORKDIR /opt/phantom

# Copy requirements first so the layer caches when only source changes.
COPY requirements.txt /opt/phantom/
RUN pip install --no-cache-dir -r requirements.txt pytesseract

# Playwright needs its Chromium downloaded before the user account
# can use it. PLAYWRIGHT_BROWSERS_PATH must be set BEFORE the install
# so the binaries land in a path readable by the non-root user.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN python -m playwright install --with-deps chromium && \
    chmod -R a+rX /opt/playwright-browsers

# Now copy the rest of the source. Putting this AFTER the install means
# changing a Python file doesn't bust the dependency layer.
COPY --chown=phantom:phantom . /opt/phantom/

# `pip install -e .` makes the `phantom` command available on PATH.
RUN pip install --no-cache-dir -e /opt/phantom/

USER phantom
ENV HOME=/home/phantom
WORKDIR /home/phantom

ENTRYPOINT ["phantom"]
CMD ["--help"]
