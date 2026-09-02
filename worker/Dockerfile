# AWS builds this ARM64 container, then snapshots the idle hook listener.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

ARG NODE_VERSION=22.23.2
ARG NODE_ARM64_SHA256=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
ARG PI_VERSION=0.84.4
ARG GIT_VERSION=2.55.0
ARG GIT_SHA256=457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357
ARG GIT_BUILD_FLAGS="NO_TCLTK=YesPlease NO_GETTEXT=YesPlease NO_RUST=YesPlease"
ARG GH_VERSION=2.99.0
ARG GH_ARM64_SHA256=564eff56a61e8caf193efde16937fba879eb62a3a479c9dd6be2001e7647680b
ARG UV_VERSION=0.12.9
ARG UV_ARM64_SHA256=c36fe17937ff6bd16dc42fc13854b5465999fcab2efe0af559381e945e3c6001

# Use the package versions supplied by the base image's Amazon Linux repository.
# hadolint ignore=DL3041
RUN dnf install -y python3.12 python3.12-pip tar xz gzip bash ca-certificates curl-minimal \
      gcc make libcurl-devel zlib-devel openssl-devel expat-devel perl \
    && dnf clean all
# Fail the build if either side of a checksum pipe fails.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Build pinned Git with its supported C implementation; no Rust toolchain is needed.
RUN curl --fail --silent --show-error --location \
      "https://www.kernel.org/pub/software/scm/git/git-${GIT_VERSION}.tar.xz" --output /tmp/git.tar.xz \
    && echo "${GIT_SHA256}  /tmp/git.tar.xz" | sha256sum --check \
    && tar -xJf /tmp/git.tar.xz -C /tmp \
    && make -C "/tmp/git-${GIT_VERSION}" -j2 prefix=/usr/local ${GIT_BUILD_FLAGS} all \
    && make -C "/tmp/git-${GIT_VERSION}" prefix=/usr/local ${GIT_BUILD_FLAGS} install \
    && rm -rf /tmp/git.tar.xz "/tmp/git-${GIT_VERSION}"
# Verify ARM64 archives before exposing tools to the agent.
RUN curl --fail --silent --show-error --location \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz" \
      --output /tmp/gh.tar.gz \
    && echo "${GH_ARM64_SHA256}  /tmp/gh.tar.gz" | sha256sum --check \
    && tar -xzf /tmp/gh.tar.gz -C /tmp \
    && install "/tmp/gh_${GH_VERSION}_linux_arm64/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_arm64" \
    && curl --fail --silent --show-error --location \
      "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-unknown-linux-gnu.tar.gz" \
      --output /tmp/uv.tar.gz \
    && echo "${UV_ARM64_SHA256}  /tmp/uv.tar.gz" | sha256sum --check \
    && tar -xzf /tmp/uv.tar.gz -C /tmp \
    && install /tmp/uv-aarch64-unknown-linux-gnu/uv /tmp/uv-aarch64-unknown-linux-gnu/uvx /usr/local/bin/ \
    && rm -rf /tmp/uv.tar.gz /tmp/uv-aarch64-unknown-linux-gnu
# Fail the AWS build if an installed tool cannot run.
RUN git --version && gh --version && uv --version
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
COPY listener.py supervisor.py github_access.py github_api.py startup.sh teardown.sh ./

ENV PYTHONUNBUFFERED=1 PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_VERSION=${PI_VERSION}
EXPOSE 8080
CMD ["python3.12", "listener.py"]
