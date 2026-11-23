# Imagen para los procesos de servidor del pipeline MQTT:
# server/mqtt/subscriber.py, server/mqtt/classifier.py y server/monitor/monitor.py.
# Se construye desde la raíz del repo (ver ../../docker-compose.yml) porque esos
# scripts importan código de analysis/ y leen contracts/ con rutas relativas al repo.
FROM python:3.12-slim

WORKDIR /app

COPY server/mqtt/requirements.txt server/mqtt/requirements.txt
RUN pip install --no-cache-dir -r server/mqtt/requirements.txt

COPY . .

CMD ["python", "server/mqtt/subscriber.py"]
