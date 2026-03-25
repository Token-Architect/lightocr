FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
# libreoffice: 用于 Office 转 PDF
# default-jre: libreoffice 依赖 Java
# fonts-wqy-zenhei: 中文字体支持，防止转换乱码
# libgl1, libglib2.0-0: OpenCV 依赖
RUN apt-get update && apt-get install -y \
    libreoffice \
    default-jre \
    fonts-wqy-zenhei \
    libgl1 \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 预下载 RapidDoc V3 模型 (可选，防止运行时下载超时)
# 如果本地已经有模型，可以取消注释下面这行直接复制进去
# COPY app/models/ /app/app/models/

# 复制应用代码
COPY . .

# 创建输出目录结构
RUN mkdir -p output/images output/debug output/temp app/models

# 暴露端口
EXPOSE 7778

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7778"]
