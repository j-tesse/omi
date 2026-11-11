# Flutter + Android toolchain for building the Omi app (upstream/omi/app) as an
# Android APK, used by scripts/build_app.sh. Versions follow upstream
# (app/setup.sh, codemagic.yaml, android/app/build.gradle); re-check them when
# bumping the submodule.
FROM eclipse-temurin:21-jdk-noble

ARG FLUTTER_VERSION=3.44.5
ARG ANDROID_CMDLINE_TOOLS=11076708
ARG ANDROID_PLATFORM=36
ARG ANDROID_BUILD_TOOLS=36.0.0
ARG ANDROID_NDK=28.2.13676358
ARG ANDROID_CMAKE=3.22.1
ARG UID=1000
ARG GID=1000

# python3: android/build.gradle runs plugin patch scripts with it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl unzip xz-utils zip file ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

# Build user matching the host user, so the bind-mounted source/caches stay
# owned by them. The SDKs are owned by it too: Flutter writes into its own
# cache, and Gradle may install extra SDK packages a plugin asks for.
RUN (getent group "$GID" || groupadd -g "$GID" builder) \
    && (getent passwd "$UID" && usermod -l builder -d /home/builder -m "$(getent passwd "$UID" | cut -d: -f1)" \
        || useradd -m -u "$UID" -g "$GID" builder) \
    && mkdir -p /opt/android-sdk /opt/flutter && chown "$UID:$GID" /opt/android-sdk /opt/flutter
USER builder
ENV HOME=/home/builder \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    FLUTTER_ROOT=/opt/flutter \
    PUB_CACHE=/home/builder/.pub-cache \
    GRADLE_USER_HOME=/home/builder/.gradle \
    FLUTTER_SUPPRESS_ANALYTICS=true \
    DART_SUPPRESS_ANALYTICS=true
ENV PATH=/opt/flutter/bin:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH

RUN curl -fsSL -o /tmp/tools.zip \
        "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS}_latest.zip" \
    && unzip -q /tmp/tools.zip -d /tmp/tools \
    && mkdir -p /opt/android-sdk/cmdline-tools \
    && mv /tmp/tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest \
    && rm -rf /tmp/tools /tmp/tools.zip \
    && yes | sdkmanager --licenses >/dev/null \
    && sdkmanager --install \
        "platform-tools" \
        "platforms;android-${ANDROID_PLATFORM}" \
        "build-tools;${ANDROID_BUILD_TOOLS}" \
        "ndk;${ANDROID_NDK}" \
        "cmake;${ANDROID_CMAKE}" >/dev/null

RUN git clone --depth 1 --branch "${FLUTTER_VERSION}" https://github.com/flutter/flutter.git /opt/flutter \
    && flutter config --no-analytics --no-cli-animations \
    && flutter config --android-sdk /opt/android-sdk \
    && flutter precache --android \
    && flutter --version

WORKDIR /src/app
