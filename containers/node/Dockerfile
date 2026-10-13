# Mologate container node (docs/container-nodes.md). It needs the host's Docker socket and the
# host's /var/lib/mologate-node mounted at the same path. The same image is the helper that applies
# the firewall rules for the sandbox bridge, which is why it ships iptables and tc.
FROM python:3.12-alpine

RUN apk add --no-cache iptables ip6tables iproute2 tini

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mologate_node ./mologate_node

ENV PYTHONUNBUFFERED=1 \
    NODE_HELPER_IMAGE=ghcr.io/pmixay/mologate-node:latest

EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8090/healthz >/dev/null || exit 1

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python", "-m", "mologate_node"]
