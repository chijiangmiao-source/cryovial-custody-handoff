FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY verify.py ./verify.py

# 一次性验收服务：跑完即退出，退出码即验收结论
ENTRYPOINT ["python", "/app/verify.py"]
