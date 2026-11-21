FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
RUN useradd --system --uid 10001 --no-create-home kassa
WORKDIR /app
COPY requirements.txt .
# faster-whisper нужен только процессу бота для голосовых; в бэкенде и тестах он не импортируется.
# huggingface_hub и av закреплены: у faster-whisper 1.1.1 с их новыми версиями не работают скачивание модели и разбор ogg.
RUN pip install --no-cache-dir -r requirements.txt faster-whisper==1.1.1 huggingface_hub==0.27.1 av==13.1.0
COPY kassa_api ./kassa_api
COPY static ./static
RUN chmod -R a+rX /app
USER 10001
EXPOSE 8080 8081
CMD ["python", "-m", "kassa_api"]
