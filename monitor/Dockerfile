# 控制面容器：配置/面板/推送/去重。数据查询由用户浏览器里的油猴脚本完成。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    flask pyyaml tzdata

COPY ielts_monitor.py ui.html ./

EXPOSE 9899

CMD ["python", "ielts_monitor.py"]
