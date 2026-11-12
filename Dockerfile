FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lk_bot.py learner.py lk_words.json ./
CMD ["python", "-u", "lk_bot.py"]
