FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    flask pyyaml curl_cffi gmssl

COPY idp_monitor.py ui.html ./

EXPOSE 9897

CMD ["python", "idp_monitor.py"]
