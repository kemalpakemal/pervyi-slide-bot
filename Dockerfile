FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .
# Контейнер работает от root намеренно: Railway Volume монтируется с владельцем root,
# и непривилегированный пользователь не смог бы писать в /data. Если зададите
# RAILWAY_RUN_UID=1000 (и права на Volume) — можно перейти на non-root.
CMD ["python", "bot.py"]
