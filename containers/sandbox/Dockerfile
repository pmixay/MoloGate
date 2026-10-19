# Mologate: the base image every chat container starts from (docs/container-nodes.md).
#
# Python, bash, Node.js and the command-line tools agents reach for most, plus Open Terminal
# (MIT, github.com/open-webui/open-terminal): the API Mologate drives the container through. It
# lives in its own virtualenv, so nothing the agent installs with pip can break it, and it listens
# only on a Unix socket the container node mounts, so it works with the network switched off.
FROM python:3.12-slim-bookworm

ARG OPEN_TERMINAL_VERSION=0.14.0
ARG NODE_VERSION=24.21.0

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HOME=/home/user \
    USER=root \
    SHELL=/bin/bash \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONUNBUFFERED=1 \
    PATH=/home/user/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash bash-completion ca-certificates curl wget gnupg \
        git git-lfs gh openssh-client \
        jq ripgrep fd-find tree less nano vim-tiny \
        procps psmisc lsof file iproute2 \
        zip unzip xz-utils bzip2 \
        sqlite3 \
        build-essential pkg-config \
        tini \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

# Node.js LTS from the official binaries (Debian's is several majors behind).
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) arch=x64 ;; \
        arm64) arch=arm64 ;; \
        *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz"; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
        --exclude='*/CHANGELOG.md' --exclude='*/README.md' --exclude='*/LICENSE' --exclude='*/include/*' --exclude='*/share/doc/*'; \
    rm /tmp/node.tar.xz; \
    node --version; npm --version

# Libraries most chat tasks need, so a short task doesn't start with a long install.
RUN pip install \
        uv \
        requests httpx \
        numpy pandas matplotlib \
        pillow openpyxl \
        beautifulsoup4 lxml pyyaml

# Open Terminal, isolated from the user's Python. The OpenAPI document is written at build time so
# the node can describe the tools without starting a container.
RUN python -m venv /opt/open-terminal \
    && /opt/open-terminal/bin/pip install "open-terminal==${OPEN_TERMINAL_VERSION}" \
    && OPEN_TERMINAL_API_KEY=build /opt/open-terminal/bin/python -c "import json, socket; socket.gethostname = lambda: 'sandbox'; from open_terminal.main import app; json.dump(app.openapi(), open('/opt/open-terminal/openapi.json', 'w'))"

COPY mologate-sandbox.sh /usr/local/bin/mologate-sandbox

RUN chmod 755 /usr/local/bin/mologate-sandbox \
    && mkdir -p /home/user /run/mologate \
    && git config --system init.defaultBranch main \
    && git config --system safe.directory '*'

WORKDIR /home/user

LABEL org.opencontainers.image.title="mologate-sandbox" \
      org.opencontainers.image.description="Base image for Mologate chat containers"

# tini reaps the zombies agents leave behind; when Open Terminal exits, the container stops and
# the node starts it again on the next request.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/mologate-sandbox"]
