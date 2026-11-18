FROM maven:3.9.11-eclipse-temurin-17@sha256:e4a7ace3dc0d645ed97f8d9ad0b0d3f0b14fa8d150138f27f116d7105a639b82 AS build
WORKDIR /src
COPY mall-swarm/ ./
RUN --mount=type=cache,target=/root/.m2 mvn -B -ntp -DskipTests -Ddocker.skip=true \
      -pl mall-admin,mall-auth,mall-portal,mall-gateway,mall-search,mall-monitor -am package \
    && mkdir /out \
    && for service in admin auth portal gateway search monitor; do \
         cp "mall-$service/target/mall-$service-1.0-SNAPSHOT.jar" "/out/$service.jar"; \
       done

FROM eclipse-temurin:17.0.16_8-jre-jammy@sha256:e90fd2b084488c0ffdd22610d44c1579e63b585c7fb83489fd815c0dc5b4e1f7
WORKDIR /app
COPY --from=build /out/ ./
USER 10001:10001
ENTRYPOINT ["sh", "-c", "exec java -XX:ActiveProcessorCount=2 -Xms64m -Xmx384m -jar /app/${MALL_SERVICE}.jar"]
