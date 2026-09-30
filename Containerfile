# Engine and card data: the latest EDOPro core, card scripts and databases at build time.
FROM python:3.13-slim AS engine
RUN apt-get update && apt-get install -y --no-install-recommends g++ git ca-certificates curl \
	&& rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY scripts ./scripts
# A new DATA_VERSION (e.g. today's date) refetches the engine, scripts and databases instead of
# reusing the cached layer.
ARG DATA_VERSION=
RUN echo "data $DATA_VERSION" && scripts/fetch-data.sh vendor && scripts/build-engine.sh vendor/ygopro-core build \
	&& rm -rf vendor/CardScripts/.git vendor/BabelCDB/.git

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
COPY --from=engine /app/build/libocgcore.so build/
COPY --from=engine /app/vendor/CardScripts vendor/CardScripts
COPY --from=engine /app/vendor/BabelCDB vendor/BabelCDB
COPY --from=engine /app/vendor/strings.conf vendor/
ENV HOST=0.0.0.0 PORT=8000 STATE_DIR=/data OCGCORE_LIB=/app/build/libocgcore.so YGO_DATA=/app/vendor
VOLUME /data
EXPOSE 8000
CMD ["/app/.venv/bin/ygo-judge"]
