# The default image for docker and kubernetes sandboxes: shells, Python, Node and git for agent
# and script steps. Profiles can name any other image (`image:` in a SandboxProfile).
#
#   docker build -f deploy/sandbox.Dockerfile -t shoal-sandbox deploy/   (from the repository root)

ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl git jq nodejs npm openssh-client unzip \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/sandbox sandbox \
    && install -d -o sandbox -g sandbox /workspace

USER 10001
WORKDIR /workspace
CMD ["sleep", "infinity"]
