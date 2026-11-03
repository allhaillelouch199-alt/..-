FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir aiogram==3.13.1 telethon==1.36.0 APScheduler==3.10.4 aiosqlite==0.20.0 python-dotenv==1.0.1
COPY . .
CMD ["python", "main.py"]
