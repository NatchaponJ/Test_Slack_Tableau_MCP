FROM python:3.11-slim

# ติดตั้ง Node.js + npm (มี npx ติดมาด้วย) จำเป็นสำหรับ spawn
# `npx @tableau/mcp-server` ตอน runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ตั้ง default ไว้เผื่อรัน local
ENV PORT=3000
EXPOSE 3000

CMD ["python", "app.py"]
