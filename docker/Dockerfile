# aibrarian API + CPU worker image. Dependencies are baked in at build time;
# nothing is pip-installed when the container starts.
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels ".[flows,media]"

FROM python:3.12-slim
# Calibre CLI (calibredb, ebook-meta) for ebook ingest into the Calibre library. Official
# binary build, pinned; ~650 MB unpacked. Keep it close to the calibre version Calibre-Web
# uses (linuxserver universal-calibre mod installs the current release).
ARG CALIBRE_VERSION=9.16.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini curl xz-utils \
      libegl1 libopengl0 libglx0 libxcb-cursor0 libxkbcommon0 libfontconfig1 libnss3 \
 && mkdir -p /opt/calibre \
 && curl -fsSL "https://download.calibre-ebook.com/${CALIBRE_VERSION}/calibre-${CALIBRE_VERSION}-x86_64.txz" \
      | tar -xJ --no-same-owner -C /opt/calibre \
 && ln -s /opt/calibre/calibredb /opt/calibre/ebook-meta /usr/local/bin/ \
 && QT_QPA_PLATFORM=offscreen calibredb --version && QT_QPA_PLATFORM=offscreen ebook-meta --version \
 && apt-get purge -y xz-utils && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home aib
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels "aibrarian[flows,media]" \
 && rm -rf /wheels
USER aib
WORKDIR /home/aib
ENV PYTHONUNBUFFERED=1 QT_QPA_PLATFORM=offscreen \
    CALIBRE_CONFIG_DIRECTORY=/home/aib/.config/calibre
EXPOSE 8790 9108
ENTRYPOINT ["tini", "--"]
CMD ["aibrarian", "api"]
