FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/app.py .

# Default environment variables
ENV PORT=9100
ENV LOG_LEVEL=INFO

# Expose metrics port
EXPOSE 9100

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:9100/health', timeout=5).raise_for_status()"

# Run the exporter
CMD ["python", "app.py"]
