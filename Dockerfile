FROM python:3.12-slim

# Не создавать .pyc и писать логи сразу в stdout (удобно для `docker logs`)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hexbot.py .

# Бот работает не от root
RUN useradd -m botuser
USER botuser

CMD ["python", "hexbot.py"]
