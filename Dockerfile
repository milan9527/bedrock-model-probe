FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home app
COPY probe ./probe
USER app
EXPOSE 8000
CMD ["uvicorn", "probe.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log"]
