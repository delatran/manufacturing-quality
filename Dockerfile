FROM node:24-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY knowledge/glossary.json /build/knowledge/glossary.json
RUN npm run build:production

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 analyst \
    && useradd --uid 10001 --gid analyst --create-home analyst \
    && mkdir -p /runtime /app/.runtime \
    && chown analyst:analyst /runtime /app/.runtime
COPY analyst/ ./analyst/
COPY config/ ./config/
COPY knowledge/ ./knowledge/
COPY data/master_data.csv ./data/master_data.csv
COPY DATA_QUALITY_TEMPLATE.xlsx DATA_QUALITY.xlsx ./
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
USER analyst
EXPOSE 8000
CMD ["python", "-m", "analyst.cloud_runtime"]
