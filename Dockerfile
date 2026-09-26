FROM python:3.12-slim

WORKDIR /app
COPY . .

EXPOSE 5000

# 依赖随容器启动安装，然后启动应用
CMD ["sh", "-c", "pip install --no-cache-dir -r requirements.txt && python app.py"]
