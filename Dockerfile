FROM python:3.12-slim-bookworm

ARG MOTIONPHOTO2_REF=v2.7.7

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/opt/MotionPhoto2 \
    MOTIONPHOTO2_PYTHON=python \
    MOTIONPHOTO2_SCRIPT=/opt/MotionPhoto2/motionphoto2.py

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git libimage-exiftool-perl ffmpeg \
    && apt-get clean

COPY docker/motionphoto2_gooey_headless.py /tmp/motionphoto2_gooey_headless.py

RUN git clone --depth 1 --branch "${MOTIONPHOTO2_REF}" https://github.com/PetrVys/MotionPhoto2 /opt/MotionPhoto2 \
    && python -c "from pathlib import Path; src=Path('/opt/MotionPhoto2/requirements.txt'); out=Path('/tmp/motionphoto2-requirements-headless.txt'); out.write_text('\\n'.join(line for line in src.read_text().splitlines() if line.strip() and not line.strip().lower().startswith('gooey')) + '\\n', encoding='utf-8')" \
    && pip install --no-cache-dir -r /tmp/motionphoto2-requirements-headless.txt \
    && cp /tmp/motionphoto2_gooey_headless.py /opt/MotionPhoto2/gooey.py \
    && python /opt/MotionPhoto2/motionphoto2.py --help >/tmp/motionphoto2-help.txt

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY src /app/src

CMD ["python", "-m", "livephoto_worker"]
