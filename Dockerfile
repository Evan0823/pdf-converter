FROM python:3.12-slim

# 系统依赖：pandoc
RUN apt-get update && apt-get install -y --no-install-recommends pandoc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 项目文件
COPY server.py .
COPY templates/ templates/

# 运行目录
RUN mkdir -p uploads outputs cache

EXPOSE 5000

CMD ["python", "server.py"]
