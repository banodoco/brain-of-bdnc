# Use Python 3.10 slim image for smaller size
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies required by the bot
# - libgl1: Required for OpenCV (opencv-python-headless)
# - libglib2.0-0: Required for OpenCV
# - ffmpeg: Required for moviepy video processing
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire application
COPY . .

# Create data directory for SQLite database
RUN mkdir -p /app/data

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Railway provides the PORT environment variable
# The bot will listen on this port for webhooks (if configured)
EXPOSE 8080

# Run the bot
CMD ["python", "main.py"]



