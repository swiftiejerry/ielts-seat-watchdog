FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY ocr_server.py .

RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    flask flask-cors ddddocr pillow

ENV OCR_HOST=0.0.0.0
EXPOSE 9898

CMD ["python", "ocr_server.py"]
