FROM python:3.12-slim-bookworm

ARG MOTIONPHOTO2_VERSION=v2.7.7
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    MOTIONPHOTO2_BIN=/usr/local/bin/motionphoto2

RUN if [ -n "$TARGETARCH" ] && [ "$TARGETARCH" != "amd64" ]; then echo "MotionPhoto2 official Linux release is x86-64; TARGETARCH=$TARGETARCH is not supported by this Dockerfile."; exit 1; fi

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip libimage-exiftool-perl \
    && apt-get clean

RUN curl -fsSL \
        "https://github.com/PetrVys/MotionPhoto2/releases/download/${MOTIONPHOTO2_VERSION}/MotionPhoto2_Linux_${MOTIONPHOTO2_VERSION}.zip" \
        -o /tmp/motionphoto2.zip \
    && unzip /tmp/motionphoto2.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/motionphoto2 \
    && rm /tmp/motionphoto2.zip

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY src /app/src

CMD ["python", "-m", "livephoto_worker"]
