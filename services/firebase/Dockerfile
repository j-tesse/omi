# Firebase Emulator Suite (Firestore + Auth + UI), replacing Firebase for the
# Omi backend. Debian trixie ships JDK 21, which firebase-tools requires.
FROM node:22-trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Keep in sync with upstream/omi/package.json devDependencies.firebase-tools.
ARG FIREBASE_TOOLS_VERSION=15.22.0
RUN npm install -g --no-fund --no-audit "firebase-tools@${FIREBASE_TOOLS_VERSION}" \
    && npm cache clean --force

# Download the emulator jars/bundles at build time so startup needs no network.
USER node
ENV HOME=/home/node
RUN firebase setup:emulators:firestore && firebase setup:emulators:ui

COPY --chown=node:node firebase.json entrypoint.sh /srv/
# The emulators write *-debug.log files to the working directory.
RUN mkdir -p /home/node/work
WORKDIR /home/node/work
ENTRYPOINT ["/srv/entrypoint.sh"]
