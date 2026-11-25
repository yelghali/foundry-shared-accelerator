FROM python:3.13-slim
WORKDIR /app
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/main.py .
ENV PORT=8088
EXPOSE 8088
CMD ["python", "main.py"]
