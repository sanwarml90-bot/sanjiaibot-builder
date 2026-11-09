FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY runner.py .
# Bot working dirs live under /app/bots (rebuilt from GitHub on each start).
CMD ["python", "runner.py"]
