# Heliotrope backend — production image.
#
# Build from the REPO ROOT (paths below are root-relative):
#   docker build -f backend/Dockerfile -t heliotrope-backend .
# Building with `backend/` as the context will fail on the COPY lines.
FROM python:3.11-slim
WORKDIR /srv

# Production dependency lock. This lock pins runtime deps only; dev tools
# (pytest, hypothesis) come from the `dev` extra in pyproject.toml and must
# stay out of this image: `pip install -e "./backend[dev]"` is for CI/local.
COPY backend/pyproject.toml backend/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY backend/app ./app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HELIOTROPE_ENV=production
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT', '8000') + '/api/v1/health')" || exit 1
# Drop privileges: nothing in the app needs root (execution store writes
# under the workdir, which is re-owned below).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /srv
USER appuser
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
