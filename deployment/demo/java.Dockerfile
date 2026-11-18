FROM maven:3.9.11-eclipse-temurin-17 AS build
WORKDIR /src
COPY mall-swarm/ ./
RUN --mount=type=cache,target=/root/.m2 mvn -B -ntp -DskipTests -Ddocker.skip=true \
      -pl mall-admin,mall-auth,mall-portal,mall-gateway,mall-search,mall-monitor -am package \
    && mkdir /out \
    && for service in admin auth portal gateway search monitor; do \
         cp "mall-$service/target/mall-$service-1.0-SNAPSHOT.jar" "/out/$service.jar"; \
       done

FROM eclipse-temurin:17.0.16_8-jre-jammy
WORKDIR /app
COPY --from=build /out/ ./
USER 10001:10001
ENTRYPOINT ["sh", "-c", "exec java -XX:MaxRAMPercentage=70 -jar /app/${MALL_SERVICE}.jar"]
