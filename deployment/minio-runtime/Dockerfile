FROM docker.m.daocloud.io/library/eclipse-temurin:17-jre-jammy
COPY minio /usr/local/bin/minio
RUN chmod 755 /usr/local/bin/minio
ENTRYPOINT ["/usr/local/bin/minio"]
