FROM python:3.12-slim

WORKDIR /app

COPY web/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY web /app/web
COPY images.png /app/images.png

WORKDIR /app/web

ENV PORT=3005
EXPOSE 3005

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-3005}"]
