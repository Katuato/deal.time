# syntax=docker/dockerfile:1

# The official CPU PyTorch wheel needs glibc. Build its Python runtime separately.
FROM python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS python-runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends libquadmath0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --no-compile --only-binary=:all: -r requirements.txt \
    && python -m pip check

RUN <<'SH'
set -eu
mkdir -p /runtime/lib64
# The slim Python image ships the optional Tk extension without its GUI libraries.
rm -f /usr/local/lib/python3.14/lib-dynload/_tkinter*.so
find /usr/local -type f \( -name python3.14 -o -name '*.so' -o -name '*.so.*' \) \
    -exec ldd '{}' \; > /tmp/linked-libraries
if grep '=> not found' /tmp/linked-libraries; then
    exit 1
fi
awk '$2 == "=>" && $3 ~ /^\/(lib|usr\/lib)\// {print $3}' /tmp/linked-libraries \
    | sort -u \
    | xargs -r cp -L --parents -t /runtime
cp -L /lib64/ld-linux-x86-64.so.2 /runtime/lib64/
rm -rf /usr/local/include /usr/local/lib/pkgconfig /usr/local/share/man
SH

FROM alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
RUN apk add --no-cache ca-certificates ffmpeg tzdata \
    && addgroup -S -g 10001 app \
    && adduser -S -D -u 10001 -G app -h /home/app app \
    && mkdir -p /app/logs /app/voices /home/app/.cache/huggingface \
    && chown -R app:app /app/logs /app/voices /home/app

COPY --from=python-runtime /runtime/ /
COPY --from=python-runtime /usr/local/ /usr/local/

ENV HOME=/home/app \
    HF_HOME=/home/app/.cache/huggingface \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY main.py config.py logging_setup.py alembic.ini ./
COPY migrations/*.py migrations/script.py.mako ./migrations/
COPY migrations/versions/*.py ./migrations/versions/
COPY calendarr/*.py ./calendarr/
COPY database/*.py ./database/
COPY modules/*.py ./modules/
COPY tg_bot/*.py ./tg_bot/
COPY tg_bot/privacy_policy.html ./tg_bot/
COPY docs/index.html docs/styles.css docs/config.js docs/api.js docs/app.js ./miniapp/
COPY docs/assets/logo.png ./miniapp/assets/

USER app
CMD ["python", "main.py"]
