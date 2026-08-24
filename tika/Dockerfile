# Tika, plus a JPEG2000 decoder.
#
# Scanned PDFs are OCR'd by rendering each page to a bitmap and handing it to
# Tesseract. Some scanners store those page images as JPEG2000, which PDFBox
# cannot decode on its own — the JPEG2000 ImageIO plugin ships under the JJ2000
# license, which is not Apache-compatible, so Tika is not allowed to bundle it.
# Without it the render throws MissingImageReaderException per page, the image
# is dropped from the bitmap, and OCR reads a blank page: silent text loss, not
# just log noise.
#
# The stock entrypoint already puts /tika-extras/* on the classpath for exactly
# this purpose, so dropping the jars in is the whole fix.
FROM alpine:3.20 AS jars
RUN apk add --no-cache curl
ARG JAI_VERSION=1.4.0
WORKDIR /tika-extras
# jai-imageio-jpeg2000 is the reader; jai-imageio-core carries the ImageIO
# plumbing it registers itself through, and is useless without it. Tika 4 does
# now bundle -core, so that half is a duplicate of a jar already in
# /opt/tika-server/lib — same version, and the classpath simply takes the first.
# Kept anyway: it is the reader that is licence-blocked, and pairing them here
# means an -extras directory that works whatever the base image ships.
RUN base=https://repo1.maven.org/maven2/com/github/jai-imageio \
 && curl -fsSLO "$base/jai-imageio-core/${JAI_VERSION}/jai-imageio-core-${JAI_VERSION}.jar" \
 && curl -fsSLO "$base/jai-imageio-jpeg2000/${JAI_VERSION}/jai-imageio-jpeg2000-${JAI_VERSION}.jar"

# Pinned, not `latest-full`: on 2026-08-22 upstream moved that tag from Tika
# 3.3.1 to 4.0.0, which relaid the image out (see the classpath below) and broke
# CI on a commit that touched nothing but JavaScript. A base image is a
# dependency; this one is now written down like the rest of them.
FROM apache/tika:4.0.0-full

# Versioned and pushed alongside the other two images so that one `meerail.sh`
# install pulls a set that was built and tested together — this image carries no
# meerail code of its own, but it is part of the release.
ARG MEERAIL_VERSION=dev
LABEL org.opencontainers.image.title="meerail-tika" \
      org.opencontainers.image.description="Apache Tika (full, with OCR) plus the JPEG2000 ImageIO jars meerail needs" \
      org.opencontainers.image.version="${MEERAIL_VERSION}" \
      org.opencontainers.image.source="https://github.com/ribalba/meerail" \
      org.opencontainers.image.url="https://meerail.eu" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY --from=jars /tika-extras /tika-extras

# Parser, timeout, pool and logging config — README.md in this directory is what
# each setting is for, because tika-config.json cannot carry its own comments
# (Tika's pipes config merger rejects them, whatever the format documents).
#
# Both files are carried in the image rather than mounted or passed as flags
# because the compose files that use this image are also the ones a user already
# has on disk from an earlier install — a fix that only takes effect when they
# also update their compose file is a fix most people never get.
COPY tika-config.json log4j2.xml /meerail/

# The stock entrypoint, with those two files wired in. Copied rather than
# extended because there is no way to append to an ENTRYPOINT: upstream's is
# `sh -c "exec java -cp ... TikaServerCli -h 0.0.0.0 $0 $@"`, and the `$0 $@`
# tail is what passes the compose `command:` through, so it has to stay last.
#
# The classpath has to match the base image exactly, and it is what changes
# across major versions: through 3.x the server was one fat jar at the root
# (`/tika-server-standard-${TIKA_VERSION}.jar`), 4.x explodes it into
# /opt/tika-server plus its dependencies in /opt/tika-server/lib. Getting it
# wrong is not subtle — the JVM cannot find TikaServerCli, the container exits
# on the spot, and compose reports it as unhealthy. When bumping the FROM
# above, diff this against `docker inspect --format '{{json .Config.Entrypoint}}'`
# on the new base and keep only our two -D/-c additions.
ENTRYPOINT ["/bin/sh", "-c", "exec java -Dlog4j2.configurationFile=/meerail/log4j2.xml -cp \"/opt/tika-server/*:/opt/tika-server/lib/*:/tika-extras/*\" org.apache.tika.server.core.TikaServerCli -c /meerail/tika-config.json -h 0.0.0.0 $0 $@"]
