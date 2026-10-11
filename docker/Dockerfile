# aibrarian API + CPU worker image. Dependencies are baked in at build time;
# nothing is pip-installed when the container starts.
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels ".[flows,media]"

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home aib
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels "aibrarian[flows,media]" \
 && rm -rf /wheels
USER aib
WORKDIR /home/aib
ENV PYTHONUNBUFFERED=1
EXPOSE 8790 9108
ENTRYPOINT ["tini", "--"]
CMD ["aibrarian", "api"]
