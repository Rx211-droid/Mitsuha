FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# health check port should match Render's PORT env
ENV PORT=8443

CMD ["python", "main.py"]
