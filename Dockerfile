# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM python:3.12.16-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS runtime
COPY docker/locks/debian.sources /etc/apt/sources.list.d/debian.sources
RUN apt-get update && apt-get install --no-install-recommends -y libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home service \
    && install -d -o 10001 -g 10001 /var/lib/atropos
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001

FROM runtime AS builder
USER root
COPY docker/locks /locks
RUN python -m pip download --no-cache-dir --only-binary=:all: --no-deps --require-hashes -r /locks/build.txt -d /build-tools
RUN --network=none python -m pip install --no-index --no-deps /build-tools/*.whl
WORKDIR /engine
COPY pyproject.toml README.md LICENSE /engine/
COPY atropos /engine/atropos
COPY client /client
RUN python -m pip download --no-cache-dir --prefer-binary --no-deps --no-build-isolation --require-hashes -r /locks/engine.txt -d /sources
RUN --network=none python -m pip wheel --no-index --no-deps --no-build-isolation --wheel-dir /wheels /sources/* /client /engine \
    && python /locks/record-environment.py build engine

FROM runtime AS final
USER root
COPY docker/locks /locks
COPY --from=builder /wheels /wheels
COPY --from=builder /build-record.json /build-record.json
RUN --network=none python -m pip install --no-index --no-deps /wheels/*.whl \
    && python -m pip check \
    && python /locks/record-environment.py installed engine \
    && rm -rf /locks /wheels /build-record.json \
    && python -c "import atropos.rule_server, importlib.util; assert importlib.util.find_spec('atraxis') is None"
ARG VERSION
ARG REVISION
LABEL org.opencontainers.image.title="Atropos" \
      org.opencontainers.image.source="https://github.com/decryptus/atropos" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION
USER 10001:10001
WORKDIR /var/lib/atropos
ENTRYPOINT ["atropos"]
CMD ["rules-serve", "--config", "/etc/atropos/engine.yml", "--port", "8082"]
