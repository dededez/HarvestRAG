FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/models
WORKDIR /app
COPY requirements.txt requirements-engine.txt ./
RUN pip install --no-cache-dir -r requirements-engine.txt && python -m playwright install --with-deps chromium
COPY agents agents
COPY config config
COPY fetchers fetchers
COPY pipeline pipeline
COPY schemas schemas
COPY harvest_rag harvest_rag
CMD ["python", "-m", "harvest_rag", "serve", "--host", "0.0.0.0"]
