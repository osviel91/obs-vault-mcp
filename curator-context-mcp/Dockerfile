FROM rclone/rclone:latest

RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY app.py ./app.py

EXPOSE 8000

ENTRYPOINT ["python3", "/app/app.py"]