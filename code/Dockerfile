FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
RUN apt-get update \
 && apt-get install --no-install-recommends -y bubblewrap=0.8.0-2+deb12u1 git \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir Pillow==13.0.0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/mini-codex
COPY . /opt/mini-codex/
