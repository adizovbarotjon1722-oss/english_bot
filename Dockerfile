FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py bot.py course.json lesson_links.json lesson_guides.json learning_pathways.json campaigns.json question_banks.json activities.json teacher.html ./
COPY assets ./assets
USER 10001:10001
CMD ["python", "-m", "app"]
