FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render PORT ko runtime par inject karta hai.
# bot.py os.environ.get("PORT", "7860") se padhta hai — dono cases handle hain.
EXPOSE 7860

CMD ["python", "bot.py"]
