FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8978", "app:app"]
