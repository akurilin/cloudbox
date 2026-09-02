# AWS builds this ARM64 container, then snapshots the idle hook listener.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

ARG NODE_VERSION=22.23.2
ARG NODE_ARM64_SHA256=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
ARG PI_VERSION=0.84.4

RUN dnf install -y python3.12 python3.12-pip tar xz gzip bash ca-certificates curl-minimal \
    && dnf clean all
RUN curl --fail --silent --show-error --location \
      "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz" \
      --output /tmp/node.tar.xz \
    && echo "${NODE_ARM64_SHA256}  /tmp/node.tar.xz" | sha256sum --check \
    && tar -xJf /tmp/node.tar.xz --strip-components=1 -C /usr/local \
    && rm /tmp/node.tar.xz
RUN npm install --global --omit=optional "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && npm cache clean --force

WORKDIR /opt/cloudbox
COPY requirements.txt .
RUN python3.12 -m pip install --no-cache-dir --requirement requirements.txt
COPY listener.py supervisor.py startup.sh teardown.sh ./

ENV PYTHONUNBUFFERED=1 PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_VERSION=${PI_VERSION}
EXPOSE 8080
CMD ["python3.12", "listener.py"]
