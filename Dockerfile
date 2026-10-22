# Aristotle CI runner: one image for GitLab, Jenkins, GitHub, Buildkite, or a plain `docker run`.

FROM python:3.12-slim

# The repository GHCR links the published image to.
LABEL org.opencontainers.image.source="https://github.com/harmonic-ai/aristotle"

# git is required: the file scope comes from the repository index and diff.
# procps provides ps and kill, which Jenkins' docker agent runs in the container to stop a step.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# CI runners check out as one uid and run containers as another; safe.directory opts out of git's ownership check.
RUN git config --system --add safe.directory '*'

# Only what the wheel needs: a COPY layer outlives the rm below, and the published image is public.
COPY pyproject.toml README.md LICENSE /tmp/aristotle/
COPY src /tmp/aristotle/src
RUN pip install --no-cache-dir /tmp/aristotle \
    && rm -rf /tmp/aristotle

# Where CI systems mount the checkout.
WORKDIR /workspace

# CMD, not ENTRYPOINT: CI systems start the job container with their own command (GitLab `sh -c`, Jenkins `cat`).
CMD ["aristotle", "ci"]
