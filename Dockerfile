FROM python:3.12-slim

RUN pip install --no-cache-dir pymobiledevice3==11.27.2 \
 && useradd --uid 1000 --create-home app \
 && mkdir /data && chown app:app /data

WORKDIR /app
COPY app.py .
COPY static static

USER app
# pymobiledevice3 caches the developer disk image under $HOME
ENV HOME=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
