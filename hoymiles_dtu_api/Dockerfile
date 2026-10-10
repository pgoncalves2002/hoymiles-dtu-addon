FROM python:3.12-alpine

RUN pip install --no-cache-dir paho-mqtt==2.1.0

COPY server.py /app/server.py

EXPOSE 8099
CMD ["python3", "-u", "/app/server.py"]
